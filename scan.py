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
import random
import signal
import psutil
import threading
import time
import traceback
import uuid
import logging
from urllib.parse import unquote_plus
from common import strtobool
from kafka import KafkaProducer
from kafka.errors import KafkaError, KafkaTimeoutError, NoBrokersAvailable

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

# =============================================================================
# Kafka Producer Configuration Constants
# =============================================================================

# Connection keepalive: 5 minutes (300000ms) - prevents idle connection drops
KAFKA_CONNECTIONS_MAX_IDLE_MS = int(os.getenv("KAFKA_CONNECTIONS_MAX_IDLE_MS", "300000"))

# Request timeout: 30 seconds - time to wait for response from broker
KAFKA_REQUEST_TIMEOUT_MS = int(os.getenv("KAFKA_REQUEST_TIMEOUT_MS", "30000"))

# Retry backoff: 500ms base - initial wait time between retries
KAFKA_RETRY_BACKOFF_MS = int(os.getenv("KAFKA_RETRY_BACKOFF_MS", "500"))

# Max retries at producer level (kafka-python internal retries)
KAFKA_RETRIES = int(os.getenv("KAFKA_RETRIES", "3"))

# Metadata max age: 5 minutes - how often to refresh cluster metadata
KAFKA_METADATA_MAX_AGE_MS = int(os.getenv("KAFKA_METADATA_MAX_AGE_MS", "300000"))

# =============================================================================
# Application-level Retry Configuration
# =============================================================================

# Max application-level retry attempts for send operations
KAFKA_MAX_SEND_RETRIES = int(os.getenv("KAFKA_MAX_SEND_RETRIES", "3"))

# Base delay for exponential backoff (seconds)
KAFKA_SEND_RETRY_BASE_DELAY = float(os.getenv("KAFKA_SEND_RETRY_BASE_DELAY", "0.5"))

# Maximum delay cap for exponential backoff (seconds)
KAFKA_SEND_RETRY_MAX_DELAY = float(os.getenv("KAFKA_SEND_RETRY_MAX_DELAY", "5.0"))

# =============================================================================
# Circuit Breaker Configuration
# =============================================================================

# Number of consecutive failures before opening circuit
CIRCUIT_BREAKER_FAILURE_THRESHOLD = int(os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5"))

# Time in seconds before attempting to close circuit (half-open state)
CIRCUIT_BREAKER_RECOVERY_TIMEOUT = int(os.getenv("CIRCUIT_BREAKER_RECOVERY_TIMEOUT", "30"))

# Number of successful probes needed to close circuit
CIRCUIT_BREAKER_SUCCESS_THRESHOLD = int(os.getenv("CIRCUIT_BREAKER_SUCCESS_THRESHOLD", "2"))

# =============================================================================
# Global State - Persists across Lambda invocations
# =============================================================================

# Global Kafka producer - persists across Lambda invocations
kafka_producer = None
kafka_producer_lock = threading.Lock()

# Circuit breaker state
circuit_breaker_state = {
    "state": "CLOSED",  # CLOSED, OPEN, HALF_OPEN
    "failure_count": 0,
    "success_count": 0,
    "last_failure_time": None,
    "last_error": None,
}
circuit_breaker_lock = threading.Lock()

logger = logging.getLogger()

for handler in logger.handlers:
    logger.removeHandler(handler)

stream_handler = logging.StreamHandler()

formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)

logger.addHandler(stream_handler)
logger.setLevel(logging.INFO)

# =============================================================================
# Circuit Breaker Implementation
# =============================================================================

class CircuitBreakerOpen(Exception):
    """Exception raised when circuit breaker is open and rejecting requests."""
    pass


def get_circuit_breaker_state():
    """Get current circuit breaker state (thread-safe)."""
    with circuit_breaker_lock:
        return circuit_breaker_state["state"]


def check_circuit_breaker():
    """
    Check if circuit breaker allows the request to proceed.

    Returns:
        bool: True if request can proceed, raises CircuitBreakerOpen otherwise

    Raises:
        CircuitBreakerOpen: If circuit is open and not ready for probe
    """
    with circuit_breaker_lock:
        state = circuit_breaker_state["state"]

        if state == "CLOSED":
            return True

        if state == "OPEN":
            # Check if recovery timeout has elapsed
            if circuit_breaker_state["last_failure_time"]:
                elapsed = time.time() - circuit_breaker_state["last_failure_time"]
                if elapsed >= CIRCUIT_BREAKER_RECOVERY_TIMEOUT:
                    # Transition to half-open state
                    circuit_breaker_state["state"] = "HALF_OPEN"
                    circuit_breaker_state["success_count"] = 0
                    logger.info(
                        f"Circuit breaker transitioning to HALF_OPEN after {elapsed:.1f}s recovery period"
                    )
                    return True

            logger.warning(
                f"Circuit breaker is OPEN - rejecting Kafka request. "
                f"Last error: {circuit_breaker_state['last_error']}"
            )
            raise CircuitBreakerOpen(
                f"Circuit breaker is open due to {circuit_breaker_state['failure_count']} "
                f"consecutive failures. Last error: {circuit_breaker_state['last_error']}"
            )

        if state == "HALF_OPEN":
            # Allow probe request through
            return True

        return True


def record_circuit_breaker_success():
    """Record a successful Kafka operation."""
    with circuit_breaker_lock:
        if circuit_breaker_state["state"] == "HALF_OPEN":
            circuit_breaker_state["success_count"] += 1
            if circuit_breaker_state["success_count"] >= CIRCUIT_BREAKER_SUCCESS_THRESHOLD:
                circuit_breaker_state["state"] = "CLOSED"
                circuit_breaker_state["failure_count"] = 0
                circuit_breaker_state["success_count"] = 0
                circuit_breaker_state["last_error"] = None
                logger.info("Circuit breaker CLOSED after successful probe requests")
        elif circuit_breaker_state["state"] == "CLOSED":
            # Reset failure count on success
            circuit_breaker_state["failure_count"] = 0


def record_circuit_breaker_failure(error):
    """Record a failed Kafka operation."""
    with circuit_breaker_lock:
        circuit_breaker_state["failure_count"] += 1
        circuit_breaker_state["last_failure_time"] = time.time()
        circuit_breaker_state["last_error"] = str(error)[:200]  # Truncate long errors

        if circuit_breaker_state["state"] == "HALF_OPEN":
            # Failed during probe - reopen circuit
            circuit_breaker_state["state"] = "OPEN"
            circuit_breaker_state["success_count"] = 0
            logger.warning(
                f"Circuit breaker reopened due to probe failure: {error}"
            )
        elif circuit_breaker_state["state"] == "CLOSED":
            if circuit_breaker_state["failure_count"] >= CIRCUIT_BREAKER_FAILURE_THRESHOLD:
                circuit_breaker_state["state"] = "OPEN"
                logger.error(
                    f"Circuit breaker OPENED after {circuit_breaker_state['failure_count']} "
                    f"consecutive failures. Last error: {error}"
                )


def reset_circuit_breaker():
    """Reset circuit breaker to initial state (for testing/recovery)."""
    with circuit_breaker_lock:
        circuit_breaker_state["state"] = "CLOSED"
        circuit_breaker_state["failure_count"] = 0
        circuit_breaker_state["success_count"] = 0
        circuit_breaker_state["last_failure_time"] = None
        circuit_breaker_state["last_error"] = None


# =============================================================================
# Kafka Producer Management
# =============================================================================

def create_kafka_producer():
    """
    Create a new Kafka producer with optimized settings for Lambda environment.

    Configuration includes:
    - Connection keepalive to maintain persistent connections
    - Appropriate timeouts for Lambda execution context
    - Retry settings for transient failures

    Returns:
        KafkaProducer: Configured producer instance or None if creation fails
    """
    try:
        producer = KafkaProducer(
            bootstrap_servers=REX_KAFKA_BOOTSTRAP_SERVERS.split(','),
            security_protocol='PLAINTEXT',
            api_version=(3, 5, 1),
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),

            # Connection keepalive settings - prevents idle connection drops
            connections_max_idle_ms=KAFKA_CONNECTIONS_MAX_IDLE_MS,

            # Request timeout - time to wait for broker response
            request_timeout_ms=KAFKA_REQUEST_TIMEOUT_MS,

            # Retry settings at kafka-python level
            retries=KAFKA_RETRIES,
            retry_backoff_ms=KAFKA_RETRY_BACKOFF_MS,

            # Metadata refresh interval - keeps cluster info current
            metadata_max_age_ms=KAFKA_METADATA_MAX_AGE_MS,

            # Delivery guarantees
            acks='all',

            # Single in-flight request for ordering guarantees
            max_in_flight_requests_per_connection=1,

            # Linger time - small batch window for efficiency
            linger_ms=10,
        )
        logger.info(
            f"Created new Kafka producer with bootstrap_servers={REX_KAFKA_BOOTSTRAP_SERVERS}, "
            f"connections_max_idle_ms={KAFKA_CONNECTIONS_MAX_IDLE_MS}, "
            f"request_timeout_ms={KAFKA_REQUEST_TIMEOUT_MS}"
        )
        return producer
    except NoBrokersAvailable as e:
        logger.error(
            f"Failed to create Kafka producer - no brokers available: {e}. "
            f"Bootstrap servers: {REX_KAFKA_BOOTSTRAP_SERVERS}"
        )
        raise
    except Exception as e:
        logger.error(f"Failed to create Kafka producer: {e}")
        traceback.print_exc()
        raise


def close_kafka_producer():
    """Close the global Kafka producer and reset state."""
    global kafka_producer
    with kafka_producer_lock:
        if kafka_producer is not None:
            try:
                kafka_producer.close(timeout=5)
                logger.info("Closed Kafka producer")
            except Exception as e:
                logger.warning(f"Error closing Kafka producer: {e}")
            finally:
                kafka_producer = None


def get_kafka_producer():
    """
    Get or create a Kafka producer instance that persists across invocations.

    This function implements connection pooling by maintaining a global producer
    instance. The producer is created lazily on first use and reused for
    subsequent invocations.

    Returns:
        KafkaProducer: Configured producer instance or None if Kafka is not configured
    """
    global kafka_producer

    # Return None if bootstrap servers not configured
    if not REX_KAFKA_BOOTSTRAP_SERVERS:
        return None

    creation_error = None
    with kafka_producer_lock:
        # Create producer if it doesn't exist
        if kafka_producer is None:
            try:
                kafka_producer = create_kafka_producer()
            except Exception as e:
                logger.error(f"Failed to create Kafka producer: {e}")
                creation_error = e
        producer = kafka_producer

    # Record failure outside kafka_producer_lock to avoid lock-order inversion
    # with circuit_breaker_lock (get_kafka_status acquires circuit_breaker_lock
    # then kafka_producer_lock, so we must never do the reverse).
    if creation_error is not None:
        record_circuit_breaker_failure(creation_error)
        return None

    return producer


def recreate_kafka_producer():
    """
    Force recreation of the Kafka producer.

    Used when connection issues are detected to establish a fresh connection.
    """
    global kafka_producer
    creation_error = None
    producer = None
    with kafka_producer_lock:
        if kafka_producer is not None:
            try:
                kafka_producer.close(timeout=5)
            except Exception as e:
                logger.warning(f"Error closing old Kafka producer during recreation: {e}")
            kafka_producer = None

        try:
            kafka_producer = create_kafka_producer()
            logger.info("Successfully recreated Kafka producer")
            producer = kafka_producer
        except Exception as e:
            logger.error(f"Failed to recreate Kafka producer: {e}")
            creation_error = e

    # Record failure outside kafka_producer_lock to avoid lock-order inversion
    # with circuit_breaker_lock (get_kafka_status acquires circuit_breaker_lock
    # then kafka_producer_lock, so we must never do the reverse).
    if creation_error is not None:
        record_circuit_breaker_failure(creation_error)
        return None

    return producer


# =============================================================================
# Retry Logic with Exponential Backoff
# =============================================================================

def calculate_backoff_delay(attempt, base_delay=None, max_delay=None):
    """
    Calculate exponential backoff delay with jitter.

    Args:
        attempt: Current attempt number (0-indexed)
        base_delay: Base delay in seconds (default from env)
        max_delay: Maximum delay cap in seconds (default from env)

    Returns:
        float: Delay in seconds before next retry
    """
    if base_delay is None:
        base_delay = KAFKA_SEND_RETRY_BASE_DELAY
    if max_delay is None:
        max_delay = KAFKA_SEND_RETRY_MAX_DELAY

    # Exponential backoff: base * 2^attempt
    delay = base_delay * (2 ** attempt)

    # Add jitter (±25% randomization)
    jitter = delay * 0.25 * (2 * random.random() - 1)
    delay = delay + jitter

    # Cap at maximum delay
    return min(delay, max_delay)


def send_with_retry(topic, message, key=None, headers=None, max_retries=None):
    """
    Send a message to Kafka with retry logic and exponential backoff.

    This function handles transient failures by retrying with exponential backoff.
    It also integrates with the circuit breaker to prevent hammering a failing
    Kafka cluster.

    The global Kafka producer is fetched at the start of each attempt so that
    any producer recreated during a previous retry is automatically picked up,
    and callers always use the latest producer instance.

    Args:
        topic: Target Kafka topic
        message: Message value (will be serialized by producer)
        key: Optional message key
        headers: Optional message headers
        max_retries: Maximum number of retry attempts (default from env)

    Returns:
        bool: True if message was sent successfully, False otherwise

    Raises:
        CircuitBreakerOpen: If circuit breaker is open
    """
    if max_retries is None:
        max_retries = KAFKA_MAX_SEND_RETRIES

    # Check circuit breaker before attempting
    check_circuit_breaker()  # Raises CircuitBreakerOpen if open

    last_error = None
    for attempt in range(max_retries + 1):
        # Fetch the current global producer on each attempt so that any producer
        # recreated due to a connection error in a previous retry is used here,
        # and any caller that holds a reference to an older instance stays consistent.
        producer = get_kafka_producer()
        if producer is None:
            last_error = RuntimeError("No Kafka producer available")
            logger.error("No Kafka producer available, cannot send Kafka message")
            # Treat missing producer as retryable to allow backoff/retry for
            # transient producer-creation failures.
            continue

        try:
            # Send message
            future = producer.send(topic, key=key, value=message, headers=headers)

            # Wait for confirmation with timeout
            # The timeout here is shorter than request_timeout_ms to allow for retries
            record_metadata = future.get(timeout=KAFKA_REQUEST_TIMEOUT_MS / 1000)

            # Flush to ensure delivery
            producer.flush(timeout=5)

            # Record success with circuit breaker
            record_circuit_breaker_success()

            logger.info(
                f"Successfully sent message to topic={topic}, "
                f"partition={record_metadata.partition}, "
                f"offset={record_metadata.offset}"
            )
            return True

        except KafkaTimeoutError as e:
            last_error = e
            logger.warning(
                f"Kafka send timeout (attempt {attempt + 1}/{max_retries + 1}): "
                f"topic={topic}, error={e}"
            )
        except KafkaError as e:
            last_error = e
            error_str = str(e)

            # Check for connection reset errors (errno 104)
            is_connection_error = (
                "Connection reset by peer" in error_str or
                "errno=104" in error_str or
                "[Errno 104]" in error_str or
                "ConnectionError" in type(e).__name__
            )

            logger.warning(
                f"Kafka send error (attempt {attempt + 1}/{max_retries + 1}): "
                f"topic={topic}, error_type={type(e).__name__}, error={e}, "
                f"is_connection_error={is_connection_error}"
            )

            # If connection error, recreate the producer so the next attempt
            # (which fetches the global via get_kafka_producer) uses a fresh connection
            if is_connection_error and attempt < max_retries:
                logger.info("Attempting to recreate Kafka producer due to connection error")
                recreate_kafka_producer()

        except Exception as e:
            last_error = e
            logger.error(
                f"Unexpected error sending Kafka message (attempt {attempt + 1}/{max_retries + 1}): "
                f"topic={topic}, error_type={type(e).__name__}, error={e}"
            )

        # Don't sleep after the last attempt
        if attempt < max_retries:
            delay = calculate_backoff_delay(attempt)
            logger.info(f"Retrying Kafka send in {delay:.2f}s...")
            time.sleep(delay)

    # All retries exhausted
    logger.error(
        f"Failed to send Kafka message after {max_retries + 1} attempts. "
        f"topic={topic}, last_error={last_error}"
    )
    record_circuit_breaker_failure(last_error)
    return False


def event_object(event, event_source="s3"):

    # SNS events are slightly different
    if event_source.upper() == "SNS":
        event = json.loads(event["Records"][0]["Sns"]["Message"])

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

    # Create and return the object
    s3 = boto3.resource("s3")
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
    """
    Publish scan start event to Kafka.

    Uses retry logic with exponential backoff and circuit breaker protection.

    Args:
        producer: KafkaProducer instance
        s3_object: S3 object being scanned
        scan_start_topic: Kafka topic for scan start events
        timestamp: Scan start timestamp

    Returns:
        bool: True if message was sent successfully, False otherwise
    """
    message = {
        "bucket": s3_object.bucket_name,
        "key": s3_object.key,
        "version": s3_object.version_id,
        AV_SCAN_START_METADATA: True,
        AV_TIMESTAMP_METADATA: timestamp,
    }

    try:
        success = send_with_retry(
            topic=scan_start_topic,
            message=message
        )
        if not success:
            logger.error(
                f"Failed to send scan start message for s3://{s3_object.bucket_name}/{s3_object.key} "
                f"after all retries"
            )
        return success
    except CircuitBreakerOpen as e:
        logger.warning(
            f"Circuit breaker open - skipping scan start message for "
            f"s3://{s3_object.bucket_name}/{s3_object.key}: {e}"
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error sending scan start message for "
            f"s3://{s3_object.bucket_name}/{s3_object.key}: {e}"
        )
        return False


def kafka_scan_results(
    s3_object, scan_result, scan_signature, timestamp
):
    """
    Publish scan results to Kafka.

    Uses retry logic with exponential backoff and circuit breaker protection.
    Results are only published based on configuration (AV_STATUS_PUBLISH_CLEAN,
    AV_STATUS_PUBLISH_INFECTED).

    Args:
        s3_object: S3 object that was scanned
        scan_result: Scan result (CLEAN or INFECTED)
        scan_signature: Virus signature if infected, OK otherwise
        timestamp: Scan completion timestamp

    Returns:
        bool: True if message was sent successfully or skipped per config,
              False if send failed
    """
    # Don't publish if scan_result is CLEAN and CLEAN results should not be published
    if scan_result == AV_STATUS_CLEAN and not str_to_bool(AV_STATUS_PUBLISH_CLEAN):
        logger.debug(f"Skipping CLEAN result publish for s3://{s3_object.bucket_name}/{s3_object.key}")
        return True

    # Don't publish if scan_result is INFECTED and INFECTED results should not be published
    if scan_result == AV_STATUS_INFECTED and not str_to_bool(AV_STATUS_PUBLISH_INFECTED):
        logger.debug(f"Skipping INFECTED result publish for s3://{s3_object.bucket_name}/{s3_object.key}")
        return True

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
        logger.info(
            f"Sending scan results to topic={REX_KAFKA_TOPIC_AVSCAN_RESPONSE}, "
            f"key={message_key}, scan_result={scan_result}, "
            f"s3_object=s3://{s3_object.bucket_name}/{s3_object.key}"
        )

        success = send_with_retry(
            topic=REX_KAFKA_TOPIC_AVSCAN_RESPONSE,
            message=message,
            key=message_key,
            headers=headers
        )

        if not success:
            logger.error(
                f"Failed to send scan results for s3://{s3_object.bucket_name}/{s3_object.key} "
                f"after all retries. scan_result={scan_result}"
            )
        return success

    except CircuitBreakerOpen as e:
        logger.warning(
            f"Circuit breaker open - skipping scan results publish for "
            f"s3://{s3_object.bucket_name}/{s3_object.key}: {e}"
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error sending scan results for "
            f"s3://{s3_object.bucket_name}/{s3_object.key}: {e}"
        )
        return False


def kill_process_by_pid(pid):
    # Check if process is running on PID
    try:
        os.kill(pid, 0)
    except OSError:
        return

    print("Killing the process by PID %s" % pid)

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        os.kill(pid, signal.SIGKILL)


def get_kafka_status():
    """
    Get current Kafka connection and circuit breaker status for logging/monitoring.

    Returns:
        dict: Status information including circuit breaker state and producer status
    """
    global kafka_producer
    with circuit_breaker_lock:
        cb_state = {
            "circuit_state": circuit_breaker_state["state"],
            "failure_count": circuit_breaker_state["failure_count"],
            "success_count": circuit_breaker_state["success_count"],
            "last_error": circuit_breaker_state["last_error"],
        }

    with kafka_producer_lock:
        producer_initialized = kafka_producer is not None

    return {
        "producer_initialized": producer_initialized,
        "bootstrap_servers": REX_KAFKA_BOOTSTRAP_SERVERS,
        "response_topic": REX_KAFKA_TOPIC_AVSCAN_RESPONSE,
        "circuit_breaker": cb_state,
    }


def lambda_handler(event, context):
    """
    Lambda handler for S3 antivirus scanning.

    This handler:
    1. Downloads S3 objects triggered by S3/SNS events
    2. Scans them for viruses using ClamAV
    3. Updates S3 object metadata/tags with scan results
    4. Publishes results to Kafka (if configured)

    The Kafka producer is initialized globally and reused across invocations
    to maintain persistent connections to MSK brokers.
    """
    global clamd_pid

    s3 = boto3.resource("s3")
    s3_client = boto3.client("s3")

    # Track Kafka publishing success for final status
    kafka_publish_success = True

    # Get the persistent Kafka producer (reused across invocations)
    producer = get_kafka_producer()

    # Log Kafka status at start of invocation
    if producer:
        status = get_kafka_status()
        logger.info(
            f"Kafka status at invocation start: circuit_breaker={status['circuit_breaker']['circuit_state']}, "
            f"producer_initialized={status['producer_initialized']}"
        )

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

    # Publish the start time of the scan (non-blocking - failures don't stop scan)
    if producer and AV_SCAN_START_TOPIC not in [None, ""]:
        start_scan_time = get_timestamp()
        start_success = kafka_start_scan(producer, s3_object, AV_SCAN_START_TOPIC, start_scan_time)
        if not start_success:
            kafka_publish_success = False
            logger.warning("Failed to publish scan start event, continuing with scan")

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

        # Publish the scan results (non-blocking - failures don't fail the Lambda)
        if producer and REX_KAFKA_TOPIC_AVSCAN_RESPONSE not in [None, ""]:
            results_success = kafka_scan_results(
                s3_object,
                scan_result,
                scan_signature,
                result_time,
            )
            if not results_success:
                kafka_publish_success = False
                logger.warning("Failed to publish scan results event")

        if str_to_bool(AV_DELETE_INFECTED_FILES) and scan_result == AV_STATUS_INFECTED:
            delete_s3_object(s3_object)

        stop_scan_time = get_timestamp()
        print("Script finished at %s\n" % stop_scan_time)

        # Log final Kafka status
        if producer:
            final_status = get_kafka_status()
            logger.info(
                f"Kafka status at invocation end: circuit_breaker={final_status['circuit_breaker']['circuit_state']}, "
                f"kafka_publish_success={kafka_publish_success}"
            )

        # Return success - scan completed even if Kafka publishing failed
        # This ensures Lambda doesn't retry unnecessarily when MSK is down
        return {
            "statusCode": 200,
            "body": {
                "bucket": s3_object.bucket_name,
                "key": s3_object.key,
                "scan_result": scan_result,
                "kafka_publish_success": kafka_publish_success,
            }
        }

    finally:
        # Delete downloaded file to free up room on re-usable lambda function container
        try:
            os.remove(file_path)
        except OSError:
            pass

        # Don't close Kafka producer - let it persist for future invocations
        # It will be reused by subsequent Lambda invocations for better performance


def str_to_bool(s):
    return bool(strtobool(str(s)))
