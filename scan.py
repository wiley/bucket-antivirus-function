# -*- coding: utf-8 -*-
# Upside Travel, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import json
import os
import signal
import psutil
import traceback
import uuid
import logging
from urllib.parse import unquote_plus
from common import strtobool
from kafka import KafkaProducer
from kafka.errors import KafkaError

import boto3

import clamav
from common import AV_DELETE_INFECTED_FILES
from common import AV_PROCESS_ORIGINAL_VERSION_ONLY
from common import AV_SCAN_START_METADATA
from common import REX_KAFKA_BOOTSTRAP_SERVERS
from common import AV_SCAN_START_TOPIC
from common import AV_SIGNATURE_METADATA
from common import AV_STATUS_CLEAN
from common import AV_STATUS_INFECTED
from common import AV_STATUS_METADATA
from common import REX_KAFKA_TOPIC_AVSCAN_RESPONSE
from common import AV_STATUS_PUBLISH_CLEAN
from common import AV_STATUS_PUBLISH_INFECTED
from common import AV_TIMESTAMP_METADATA
from common import AV_EFS_MOUNT_POINT
from common import create_dir
from common import get_timestamp

DEFAULT_SCAN_DIR = "/tmp"
clamd_pid = None

# Global Kafka producer - persists across Lambda invocations
kafka_producer = None

logger = logging.getLogger()

for handler in logger.handlers:
    logger.removeHandler(handler)

stream_handler = logging.StreamHandler()

formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)

logger.addHandler(stream_handler)
logger.setLevel(logging.INFO)

def get_kafka_producer():
    """Get or create a Kafka producer instance that persists across invocations."""
    global kafka_producer

    # Return None if bootstrap servers not configured
    if not REX_KAFKA_BOOTSTRAP_SERVERS:
        return None

    # Create producer if it doesn't exist
    if kafka_producer is None:
        try:
            kafka_producer = KafkaProducer(
                bootstrap_servers=REX_KAFKA_BOOTSTRAP_SERVERS.split(','),
                security_protocol='PLAINTEXT',
                api_version = (3, 5, 1),
                value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                # Add some sensible defaults for Lambda environment
                request_timeout_ms=30000,
                retry_backoff_ms=500,
                max_in_flight_requests_per_connection=1,
                acks='all'
            )
            logging.info("Created new Kafka producer")
        except Exception as e:
            logging.error(f"Failed to create Kafka producer: {e}")
            traceback.print_exc()
            return None
    return kafka_producer


def event_object(event, event_source="s3"):
    s3 = boto3.resource("s3")

    # SNS events are slightly different
    if event_source.upper() == "SNS":
        event = json.loads(event["Records"][0]["Sns"]["Message"])

    if event_source.upper() == "S3-BATCH":
        task = event["tasks"][0]
        bucket_arn = task["s3BucketArn"]
        key_name = task["s3Key"]

        bucket_name = bucket_arn.split(":")[-1]

        key_name = unquote_plus(key_name)

        return s3.Object(bucket_name, key_name)

    # Break down the record
    records = event["Records"]
    if len(records) == 0:
        raise Exception("No records found in event!")
    record = records[0]

    s3_obj = record["s3"]

    # Get the bucket name
    if "bucket" not in s3_obj:
        raise Exception("No bucket found in event!")
    bucket_name = s3_obj["bucket"].get("name", None)

    # Get the key name
    if "object" not in s3_obj:
        raise Exception("No key found in event!")
    key_name = s3_obj["object"].get("key", None)

    if key_name:
        key_name = unquote_plus(key_name)

    # Ensure both bucket and key exist
    if (not bucket_name) or (not key_name):
        raise Exception("Unable to retrieve object from event.\n{}".format(event))

    return s3.Object(bucket_name, key_name)


def verify_s3_object_version(s3, s3_object):
    # validate that we only process the original version of a file, if asked to do so
    # security check to disallow processing of a new (possibly infected) object version
    # while a clean initial version is getting processed
    # downstream services may consume latest version by mistake and get the infected version instead
    bucket_versioning = s3.BucketVersioning(s3_object.bucket_name)
    if bucket_versioning.status == "Enabled":
        bucket = s3.Bucket(s3_object.bucket_name)
        versions = list(bucket.object_versions.filter(Prefix=s3_object.key))
        if len(versions) > 1:
            raise Exception(
                "Detected multiple object versions in %s.%s, aborting processing"
                % (s3_object.bucket_name, s3_object.key)
            )
    else:
        # misconfigured bucket, left with no or suspended versioning
        raise Exception(
            "Object versioning is not enabled in bucket %s" % s3_object.bucket_name
        )


def get_local_path(s3_object):
    # leave padding of 2 sizes of a file to support scanning archives (clamav would unarchive before scan)
    free_bytes = psutil.disk_usage(DEFAULT_SCAN_DIR).free
    efs_threshold = free_bytes - (3 * s3_object.content_length)

    return get_local_path_internal(
        s3_object,
        DEFAULT_SCAN_DIR,
        AV_EFS_MOUNT_POINT,
        efs_threshold,
    )


def get_local_path_internal(s3_object, local_prefix, efs_prefix, efs_threshold):
    if efs_prefix and s3_object.content_length > efs_threshold:
        prefix = efs_prefix
    else:
        prefix = local_prefix
    return os.path.join(prefix, s3_object.bucket_name, s3_object.key)


def delete_s3_object(s3_object):
    try:
        s3_object.delete()
    except Exception:
        raise Exception(
            "Failed to delete infected file: %s.%s"
            % (s3_object.bucket_name, s3_object.key)
        )
    else:
        print("Infected file deleted: %s.%s" % (s3_object.bucket_name, s3_object.key))


def set_av_metadata(s3_object, scan_result, scan_signature, timestamp):
    content_type = s3_object.content_type
    metadata = s3_object.metadata
    metadata[AV_SIGNATURE_METADATA] = scan_signature
    metadata[AV_STATUS_METADATA] = scan_result
    metadata[AV_TIMESTAMP_METADATA] = timestamp
    s3_object.copy(
        {"Bucket": s3_object.bucket_name, "Key": s3_object.key},
        ExtraArgs={
            "ContentType": content_type,
            "Metadata": metadata,
            "MetadataDirective": "REPLACE",
        },
    )


def set_av_tags(s3_client, s3_object, scan_result, scan_signature, timestamp):
    curr_tags = s3_client.get_object_tagging(
        Bucket=s3_object.bucket_name, Key=s3_object.key
    )["TagSet"]
    new_tags = copy.copy(curr_tags)
    for tag in curr_tags:
        if tag["Key"] in [
            AV_SIGNATURE_METADATA,
            AV_STATUS_METADATA,
            AV_TIMESTAMP_METADATA,
        ]:
            new_tags.remove(tag)
    new_tags.append({"Key": AV_SIGNATURE_METADATA, "Value": scan_signature})
    new_tags.append({"Key": AV_STATUS_METADATA, "Value": scan_result})
    new_tags.append({"Key": AV_TIMESTAMP_METADATA, "Value": timestamp})
    s3_client.put_object_tagging(
        Bucket=s3_object.bucket_name, Key=s3_object.key, Tagging={"TagSet": new_tags}
    )


def kafka_start_scan(producer, s3_object, scan_start_topic, timestamp):
    message = {
        "bucket": s3_object.bucket_name,
        "key": s3_object.key,
        "version": s3_object.version_id,
        AV_SCAN_START_METADATA: True,
        AV_TIMESTAMP_METADATA: timestamp,
    }
    try:
        producer.send(scan_start_topic, message)
        producer.flush()
    except KafkaError as e:
        logging.error(f"Failed to send Kafka start scan message: {e}")


def kafka_scan_results(
    producer, s3_object, scan_result, scan_signature, timestamp
):
    # Don't publish if scan_result is CLEAN and CLEAN results should not be published
    if scan_result == AV_STATUS_CLEAN and not str_to_bool(AV_STATUS_PUBLISH_CLEAN):
        return
    # Don't publish if scan_result is INFECTED and INFECTED results should not be published
    if scan_result == AV_STATUS_INFECTED and not str_to_bool(
        AV_STATUS_PUBLISH_INFECTED
    ):
        return
    message_key = str(uuid.uuid4()).encode('utf-8')
    headers = [
        ('bucket', s3_object.bucket_name.encode('utf-8')),
        ('transactionId', message_key)
    ]
    message = {
        "key": s3_object.key,
        "version": s3_object.version_id,
        AV_SIGNATURE_METADATA: scan_signature,
        AV_STATUS_METADATA: scan_result,
        AV_TIMESTAMP_METADATA: get_timestamp(),
    }
    try:
        logging.info(f"Sending message to topic {REX_KAFKA_TOPIC_AVSCAN_RESPONSE} with key={message_key} and headers={headers} and value= {message}")
        producer.send(REX_KAFKA_TOPIC_AVSCAN_RESPONSE, key=message_key, value=message, headers=headers)
        producer.flush()
    except KafkaError as e:
        logging.error(f"Failed to send Kafka scan results message: {e}")


def kill_process_by_pid(pid):
    # Check if process is running on PID
    try:
        os.kill(clamd_pid, 0)
    except OSError:
        return

    print("Killing the process by PID %s" % clamd_pid)

    try:
        os.kill(clamd_pid, signal.SIGTERM)
    except OSError:
        os.kill(clamd_pid, signal.SIGKILL)


def lambda_handler(event, context):
    global clamd_pid

    s3 = boto3.resource("s3")
    s3_client = boto3.client("s3")

    # Get the persistent Kafka producer
    kafka_producer = get_kafka_producer()

    # Get some environment variables
    ENV = os.getenv("ENV", "")
    EVENT_SOURCE = os.getenv("EVENT_SOURCE", "S3")

    if not clamav.is_clamd_running():
        if clamd_pid is not None:
            kill_process_by_pid(clamd_pid)

        clamd_pid = clamav.start_clamd_daemon()
        print("Clamd PID: %s" % clamd_pid)

    start_time = get_timestamp()
    print("Script starting at %s\n" % (start_time))
    s3_object = event_object(event, event_source=EVENT_SOURCE)

    print(
        "Scanning s3://%s ...\n" % (os.path.join(s3_object.bucket_name, s3_object.key))
    )

    if str_to_bool(AV_PROCESS_ORIGINAL_VERSION_ONLY):
        verify_s3_object_version(s3, s3_object)

    # Publish the start time of the scan
    if kafka_producer and AV_SCAN_START_TOPIC not in [None, ""]:
        start_scan_time = get_timestamp()
        kafka_start_scan(kafka_producer, s3_object, AV_SCAN_START_TOPIC, start_scan_time)

    file_path = get_local_path(s3_object)
    try:
        create_dir(os.path.dirname(file_path))
        s3_object.download_file(file_path)

        scan_result, scan_signature = clamav.scan_file(file_path)
        print(
            "Scan of s3://%s resulted in %s\n"
            % (os.path.join(s3_object.bucket_name, s3_object.key), scan_result)
        )

        result_time = get_timestamp()
        # Set the properties on the object with the scan results
        if "AV_UPDATE_METADATA" in os.environ:
            set_av_metadata(s3_object, scan_result, scan_signature, result_time)
        set_av_tags(s3_client, s3_object, scan_result, scan_signature, result_time)

        # Publish the scan results
        if kafka_producer and REX_KAFKA_TOPIC_AVSCAN_RESPONSE not in [None, ""]:
            kafka_scan_results(
                kafka_producer,
                s3_object,
                scan_result,
                scan_signature,
                result_time,
            )

        if str_to_bool(AV_DELETE_INFECTED_FILES) and scan_result == AV_STATUS_INFECTED:
            delete_s3_object(s3_object)
        stop_scan_time = get_timestamp()
        print("Script finished at %s\n" % stop_scan_time)
    finally:
        # Delete downloaded file to free up room on re-usable lambda function container
        try:
            os.remove(file_path)
        except OSError:
            pass

        # Don't close Kafka producer - let it persist for future invocations
        # It will be reused by subsequent Lambda invocations for better performance

    if EVENT_SOURCE.upper() == "S3-BATCH":
        return {
            "invocationSchemaVersion": event["invocationSchemaVersion"],
            "treatMissingKeysAs": "PermanentFailure",
            "invocationId": event["invocationId"],
            "results": [
                {
                    "taskId": event["tasks"][0]["taskId"],
                    "resultCode": "Succeeded",
                    "resultString": scan_result,
                }
            ],
        }


def str_to_bool(s):
    return bool(strtobool(str(s)))
