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

import datetime
import json
import time
import unittest
from unittest.mock import Mock, patch, MagicMock

import boto3
import botocore.session
from botocore.stub import Stubber

from common import AV_SCAN_START_METADATA
from common import AV_SIGNATURE_METADATA
from common import AV_SIGNATURE_OK
from common import AV_STATUS_METADATA
from common import AV_TIMESTAMP_METADATA
from common import get_timestamp
from scan import delete_s3_object
from scan import event_object
from scan import get_local_path_internal
from scan import set_av_metadata
from scan import set_av_tags
from scan import kafka_start_scan
from scan import kafka_scan_results
from scan import verify_s3_object_version
from scan import (
    CircuitBreakerOpen,
    check_circuit_breaker,
    record_circuit_breaker_success,
    record_circuit_breaker_failure,
    reset_circuit_breaker,
    get_circuit_breaker_state,
    calculate_backoff_delay,
    send_with_retry,
    get_kafka_producer,
    recreate_kafka_producer,
    close_kafka_producer,
    circuit_breaker_state,
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
    CIRCUIT_BREAKER_SUCCESS_THRESHOLD,
)


class TestScan(unittest.TestCase):
    def setUp(self):
        # Common data
        self.s3_bucket_name = "test_bucket"
        self.s3_key_name = "test_key"

        # Clients and Resources
        self.s3 = boto3.resource("s3")
        self.s3_client = botocore.session.get_session().create_client("s3")
        self.sns_client = botocore.session.get_session().create_client(
            "sns", region_name="us-west-2"
        )

#     def test_sns_event_object(self):
#         event = {
#             "Records": [
#                 {
#                     "s3": {
#                         "bucket": {"name": self.s3_bucket_name},
#                         "object": {"key": self.s3_key_name},
#                     }
#                 }
#             ]
#         }
#         sns_event = {"Records": [{"Sns": {"Message": json.dumps(event)}}]}
#         s3_obj = event_object(sns_event, event_source="sns")
#         expected_s3_object = self.s3.Object(self.s3_bucket_name, self.s3_key_name)
#         self.assertEqual(s3_obj, expected_s3_object)

    def test_s3_event_object(self):
        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": self.s3_bucket_name},
                        "object": {"key": self.s3_key_name},
                    }
                }
            ]
        }
        s3_obj = event_object(event)
        expected_s3_object = self.s3.Object(self.s3_bucket_name, self.s3_key_name)
        self.assertEqual(s3_obj, expected_s3_object)

    def test_s3_event_object_missing_bucket(self):
        event = {"Records": [{"s3": {"object": {"key": self.s3_key_name}}}]}
        with self.assertRaises(Exception) as cm:
            event_object(event)
            self.assertEqual(cm.exception.message, "No bucket found in event!")

    def test_s3_event_object_missing_key(self):
        event = {"Records": [{"s3": {"bucket": {"name": self.s3_bucket_name}}}]}
        with self.assertRaises(Exception) as cm:
            event_object(event)
            self.assertEqual(cm.exception.message, "No key found in event!")

    def test_s3_event_object_bucket_key_missing(self):
        event = {"Records": [{"s3": {"bucket": {}, "object": {}}}]}
        with self.assertRaises(Exception) as cm:
            event_object(event)
            self.assertEqual(
                cm.exception.message,
                "Unable to retrieve object from event.\n{}".format(event),
            )

    def test_s3_event_object_no_records(self):
        event = {"Records": []}
        with self.assertRaises(Exception) as cm:
            event_object(event)
            self.assertEqual(cm.exception.message, "No records found in event!")

    def test_verify_s3_object_version(self):
        s3_obj = self.s3.Object(self.s3_bucket_name, self.s3_key_name)

        # Set up responses
        get_bucket_versioning_response = {"Status": "Enabled"}
        get_bucket_versioning_expected_params = {"Bucket": self.s3_bucket_name}
        s3_stubber_resource = Stubber(self.s3.meta.client)
        s3_stubber_resource.add_response(
            "get_bucket_versioning",
            get_bucket_versioning_response,
            get_bucket_versioning_expected_params,
        )
        list_object_versions_response = {
            "Versions": [
                {
                    "ETag": "string",
                    "Size": 123,
                    "StorageClass": "STANDARD",
                    "Key": "string",
                    "VersionId": "string",
                    "IsLatest": True,
                    "LastModified": datetime.datetime(2015, 1, 1),
                    "Owner": {"DisplayName": "string", "ID": "string"},
                }
            ]
        }
        list_object_versions_expected_params = {
            "Bucket": self.s3_bucket_name,
            "Prefix": self.s3_key_name,
        }
        s3_stubber_resource.add_response(
            "list_object_versions",
            list_object_versions_response,
            list_object_versions_expected_params,
        )
        try:
            with s3_stubber_resource:
                verify_s3_object_version(self.s3, s3_obj)
        except Exception as e:
            self.fail("verify_s3_object_version() raised Exception unexpectedly!")
            raise e

    def test_verify_s3_object_versioning_not_enabled(self):
        s3_obj = self.s3.Object(self.s3_bucket_name, self.s3_key_name)

        # Set up responses
        get_bucket_versioning_response = {"Status": "Disabled"}
        get_bucket_versioning_expected_params = {"Bucket": self.s3_bucket_name}
        s3_stubber_resource = Stubber(self.s3.meta.client)
        s3_stubber_resource.add_response(
            "get_bucket_versioning",
            get_bucket_versioning_response,
            get_bucket_versioning_expected_params,
        )
        with self.assertRaises(Exception) as cm:
            with s3_stubber_resource:
                verify_s3_object_version(self.s3, s3_obj)
            self.assertEqual(
                cm.exception.message,
                "Object versioning is not enabled in bucket {}".format(
                    self.s3_bucket_name
                ),
            )

    def test_verify_s3_object_version_multiple_versions(self):
        s3_obj = self.s3.Object(self.s3_bucket_name, self.s3_key_name)

        # Set up responses
        get_bucket_versioning_response = {"Status": "Enabled"}
        get_bucket_versioning_expected_params = {"Bucket": self.s3_bucket_name}
        s3_stubber_resource = Stubber(self.s3.meta.client)
        s3_stubber_resource.add_response(
            "get_bucket_versioning",
            get_bucket_versioning_response,
            get_bucket_versioning_expected_params,
        )
        list_object_versions_response = {
            "Versions": [
                {
                    "ETag": "string",
                    "Size": 123,
                    "StorageClass": "STANDARD",
                    "Key": "string",
                    "VersionId": "string",
                    "IsLatest": True,
                    "LastModified": datetime.datetime(2015, 1, 1),
                    "Owner": {"DisplayName": "string", "ID": "string"},
                },
                {
                    "ETag": "string",
                    "Size": 123,
                    "StorageClass": "STANDARD",
                    "Key": "string",
                    "VersionId": "string",
                    "IsLatest": True,
                    "LastModified": datetime.datetime(2015, 1, 1),
                    "Owner": {"DisplayName": "string", "ID": "string"},
                },
            ]
        }
        list_object_versions_expected_params = {
            "Bucket": self.s3_bucket_name,
            "Prefix": self.s3_key_name,
        }
        s3_stubber_resource.add_response(
            "list_object_versions",
            list_object_versions_response,
            list_object_versions_expected_params,
        )
        with self.assertRaises(Exception) as cm:
            with s3_stubber_resource:
                verify_s3_object_version(self.s3, s3_obj)
            self.assertEqual(
                cm.exception.message,
                "Detected multiple object versions in {}.{}, aborting processing".format(
                    self.s3_bucket_name, self.s3_key_name
                ),
            )

#     def test_sns_start_scan(self):
#         sns_stubber = Stubber(self.sns_client)
#         s3_stubber_resource = Stubber(self.s3.meta.client)
#
#         sns_arn = "some_arn"
#         version_id = "version-id"
#         timestamp = get_timestamp()
#         message = {
#             "bucket": self.s3_bucket_name,
#             "key": self.s3_key_name,
#             "version": version_id,
#             AV_SCAN_START_METADATA: True,
#             AV_TIMESTAMP_METADATA: timestamp,
#         }
#         publish_response = {"MessageId": "message_id"}
#         publish_expected_params = {
#             "TargetArn": sns_arn,
#             "Message": json.dumps({"default": json.dumps(message)}),
#             "MessageStructure": "json",
#         }
#         sns_stubber.add_response("publish", publish_response, publish_expected_params)
#
#         head_object_response = {"VersionId": version_id}
#         head_object_expected_params = {
#             "Bucket": self.s3_bucket_name,
#             "Key": self.s3_key_name,
#         }
#         s3_stubber_resource.add_response(
#             "head_object", head_object_response, head_object_expected_params
#         )
#         with sns_stubber, s3_stubber_resource:
#             s3_obj = self.s3.Object(self.s3_bucket_name, self.s3_key_name)
#             sns_start_scan(self.sns_client, s3_obj, sns_arn, timestamp)

    def test_get_local_path_internal(self):
        s3_obj = self.s3.Object(self.s3_bucket_name, self.s3_key_name)
        s3_stubber_resource = Stubber(self.s3.meta.client)
        content_length = 200
        head_object_response = {
            "ContentType": "content",
            "Metadata": {},
            "ContentLength": content_length,
        }
        head_object_expected_params = {
            "Bucket": self.s3_bucket_name,
            "Key": self.s3_key_name,
        }
        s3_stubber_resource.add_response(
            "head_object", head_object_response, head_object_expected_params
        )

        with s3_stubber_resource:
            file_path = get_local_path_internal(
                s3_obj, "/tmp", "/mnt", content_length - 1
            )

            expected_file_path = "/mnt/test_bucket/test_key"
            self.assertEqual(file_path, expected_file_path)

        with s3_stubber_resource:
            file_path = get_local_path_internal(
                s3_obj, "/tmp", "/mnt", content_length + 1
            )

            expected_file_path = "/tmp/test_bucket/test_key"
            self.assertEqual(file_path, expected_file_path)

        with s3_stubber_resource:
            file_path = get_local_path_internal(s3_obj, "/tmp", None, None)

            expected_file_path = "/tmp/test_bucket/test_key"
            self.assertEqual(file_path, expected_file_path)

    def test_set_av_metadata(self):
        scan_result = "CLEAN"
        scan_signature = AV_SIGNATURE_OK
        timestamp = get_timestamp()

        s3_obj = self.s3.Object(self.s3_bucket_name, self.s3_key_name)
        s3_stubber_resource = Stubber(self.s3.meta.client)

        # First head call is done to get content type and meta data
        head_object_response = {"ContentType": "content", "Metadata": {}}
        head_object_expected_params = {
            "Bucket": self.s3_bucket_name,
            "Key": self.s3_key_name,
        }
        s3_stubber_resource.add_response(
            "head_object", head_object_response, head_object_expected_params
        )

        # Next two calls are done when copy() is called
        head_object_response_2 = {
            "ContentType": "content",
            "Metadata": {},
            "ContentLength": 200,
        }
        head_object_expected_params_2 = {
            "Bucket": self.s3_bucket_name,
            "Key": self.s3_key_name,
        }
        s3_stubber_resource.add_response(
            "head_object", head_object_response_2, head_object_expected_params_2
        )
        copy_object_response = {"VersionId": "version_id"}
        copy_object_expected_params = {
            "Bucket": self.s3_bucket_name,
            "Key": self.s3_key_name,
            "ContentType": "content",
            "CopySource": {"Bucket": self.s3_bucket_name, "Key": self.s3_key_name},
            "Metadata": {
                AV_SIGNATURE_METADATA: scan_signature,
                AV_STATUS_METADATA: scan_result,
                AV_TIMESTAMP_METADATA: timestamp,
            },
            "MetadataDirective": "REPLACE",
        }
        s3_stubber_resource.add_response(
            "copy_object", copy_object_response, copy_object_expected_params
        )

        with s3_stubber_resource:
            set_av_metadata(s3_obj, scan_result, scan_signature, timestamp)

    def test_set_av_tags(self):
        scan_result = "CLEAN"
        scan_signature = AV_SIGNATURE_OK
        timestamp = get_timestamp()
        tag_set = {
            "TagSet": [
                {"Key": AV_SIGNATURE_METADATA, "Value": scan_signature},
                {"Key": AV_STATUS_METADATA, "Value": scan_result},
                {"Key": AV_TIMESTAMP_METADATA, "Value": timestamp},
            ]
        }

        s3_stubber = Stubber(self.s3_client)
        get_object_tagging_response = tag_set
        get_object_tagging_expected_params = {
            "Bucket": self.s3_bucket_name,
            "Key": self.s3_key_name,
        }
        s3_stubber.add_response(
            "get_object_tagging",
            get_object_tagging_response,
            get_object_tagging_expected_params,
        )
        put_object_tagging_response = {}
        put_object_tagging_expected_params = {
            "Bucket": self.s3_bucket_name,
            "Key": self.s3_key_name,
            "Tagging": tag_set,
        }
        s3_stubber.add_response(
            "put_object_tagging",
            put_object_tagging_response,
            put_object_tagging_expected_params,
        )

        with s3_stubber:
            s3_obj = self.s3.Object(self.s3_bucket_name, self.s3_key_name)
            set_av_tags(self.s3_client, s3_obj, scan_result, scan_signature, timestamp)

#     def test_sns_scan_results(self):
#         sns_stubber = Stubber(self.sns_client)
#         s3_stubber_resource = Stubber(self.s3.meta.client)
#
#         sns_arn = "some_arn"
#         version_id = "version-id"
#         scan_result = "CLEAN"
#         scan_signature = AV_SIGNATURE_OK
#         timestamp = get_timestamp()
#         message = {
#             "bucket": self.s3_bucket_name,
#             "key": self.s3_key_name,
#             "version": version_id,
#             AV_SIGNATURE_METADATA: scan_signature,
#             AV_STATUS_METADATA: scan_result,
#             AV_TIMESTAMP_METADATA: timestamp,
#         }
#         publish_response = {"MessageId": "message_id"}
#         publish_expected_params = {
#             "TargetArn": sns_arn,
#             "Message": json.dumps({"default": json.dumps(message)}),
#             "MessageAttributes": {
#                 "av-status": {"DataType": "String", "StringValue": scan_result},
#                 "av-signature": {"DataType": "String", "StringValue": scan_signature},
#             },
#             "MessageStructure": "json",
#         }
#         sns_stubber.add_response("publish", publish_response, publish_expected_params)
#
#         head_object_response = {"VersionId": version_id}
#         head_object_expected_params = {
#             "Bucket": self.s3_bucket_name,
#             "Key": self.s3_key_name,
#         }
#         s3_stubber_resource.add_response(
#             "head_object", head_object_response, head_object_expected_params
#         )
#         with sns_stubber, s3_stubber_resource:
#             s3_obj = self.s3.Object(self.s3_bucket_name, self.s3_key_name)
#             sns_scan_results(
#                 self.sns_client, s3_obj, sns_arn, scan_result, scan_signature, timestamp
#             )

    def test_delete_s3_object(self):
        s3_stubber = Stubber(self.s3.meta.client)
        delete_object_response = {}
        delete_object_expected_params = {
            "Bucket": self.s3_bucket_name,
            "Key": self.s3_key_name,
        }
        s3_stubber.add_response(
            "delete_object", delete_object_response, delete_object_expected_params
        )

        with s3_stubber:
            s3_obj = self.s3.Object(self.s3_bucket_name, self.s3_key_name)
            delete_s3_object(s3_obj)

    def test_delete_s3_object_exception(self):
        s3_stubber = Stubber(self.s3.meta.client)

        with self.assertRaises(Exception) as cm:
            with s3_stubber:
                s3_obj = self.s3.Object(self.s3_bucket_name, self.s3_key_name)
                delete_s3_object(s3_obj)
            self.assertEqual(
                cm.exception.message,
                "Failed to delete infected file: {}.{}".format(
                    self.s3_bucket_name, self.s3_key_name
                ),
            )


class TestCircuitBreaker(unittest.TestCase):
    """Tests for circuit breaker functionality."""

    def setUp(self):
        """Reset circuit breaker state before each test."""
        reset_circuit_breaker()

    def tearDown(self):
        """Clean up circuit breaker state after each test."""
        reset_circuit_breaker()

    def test_initial_state_is_closed(self):
        """Circuit breaker should start in CLOSED state."""
        self.assertEqual(get_circuit_breaker_state(), "CLOSED")

    def test_check_circuit_breaker_allows_when_closed(self):
        """Requests should be allowed when circuit is CLOSED."""
        self.assertTrue(check_circuit_breaker())

    def test_circuit_opens_after_failure_threshold(self):
        """Circuit should open after reaching failure threshold."""
        for i in range(CIRCUIT_BREAKER_FAILURE_THRESHOLD):
            record_circuit_breaker_failure(Exception(f"Test error {i}"))
        
        self.assertEqual(get_circuit_breaker_state(), "OPEN")

    def test_circuit_rejects_when_open(self):
        """Requests should be rejected when circuit is OPEN."""
        # Open the circuit
        for i in range(CIRCUIT_BREAKER_FAILURE_THRESHOLD):
            record_circuit_breaker_failure(Exception(f"Test error {i}"))
        
        with self.assertRaises(CircuitBreakerOpen):
            check_circuit_breaker()

    def test_success_resets_failure_count(self):
        """Success should reset the failure count."""
        # Record some failures but not enough to open circuit
        for i in range(CIRCUIT_BREAKER_FAILURE_THRESHOLD - 1):
            record_circuit_breaker_failure(Exception(f"Test error {i}"))
        
        # Record success
        record_circuit_breaker_success()
        
        # Circuit should still be closed and failure count reset
        self.assertEqual(get_circuit_breaker_state(), "CLOSED")
        self.assertEqual(circuit_breaker_state["failure_count"], 0)

    def test_circuit_transitions_to_half_open_after_recovery_timeout(self):
        """Circuit should transition to HALF_OPEN after recovery timeout."""
        # Open the circuit
        for i in range(CIRCUIT_BREAKER_FAILURE_THRESHOLD):
            record_circuit_breaker_failure(Exception(f"Test error {i}"))
        
        # Simulate time passing
        circuit_breaker_state["last_failure_time"] = time.time() - CIRCUIT_BREAKER_RECOVERY_TIMEOUT - 1
        
        # Check should transition to HALF_OPEN and allow request
        self.assertTrue(check_circuit_breaker())
        self.assertEqual(get_circuit_breaker_state(), "HALF_OPEN")

    def test_circuit_closes_after_success_threshold_in_half_open(self):
        """Circuit should close after success threshold is met in HALF_OPEN state."""
        # Put circuit in HALF_OPEN state
        circuit_breaker_state["state"] = "HALF_OPEN"
        circuit_breaker_state["success_count"] = 0
        
        # Record enough successes
        for i in range(CIRCUIT_BREAKER_SUCCESS_THRESHOLD):
            record_circuit_breaker_success()
        
        self.assertEqual(get_circuit_breaker_state(), "CLOSED")

    def test_circuit_reopens_on_failure_in_half_open(self):
        """Circuit should reopen on failure in HALF_OPEN state."""
        # Put circuit in HALF_OPEN state
        circuit_breaker_state["state"] = "HALF_OPEN"
        
        # Record failure
        record_circuit_breaker_failure(Exception("Probe failure"))
        
        self.assertEqual(get_circuit_breaker_state(), "OPEN")

    def test_reset_circuit_breaker(self):
        """Reset should return circuit to initial state."""
        # Open the circuit
        for i in range(CIRCUIT_BREAKER_FAILURE_THRESHOLD):
            record_circuit_breaker_failure(Exception(f"Test error {i}"))
        
        self.assertEqual(get_circuit_breaker_state(), "OPEN")
        
        # Reset
        reset_circuit_breaker()
        
        self.assertEqual(get_circuit_breaker_state(), "CLOSED")
        self.assertEqual(circuit_breaker_state["failure_count"], 0)
        self.assertEqual(circuit_breaker_state["success_count"], 0)
        self.assertIsNone(circuit_breaker_state["last_error"])


class TestExponentialBackoff(unittest.TestCase):
    """Tests for exponential backoff calculation."""

    def test_backoff_increases_exponentially(self):
        """Backoff delay should increase exponentially with attempts."""
        base_delay = 0.5
        delays = [calculate_backoff_delay(i, base_delay=base_delay, max_delay=100) 
                  for i in range(5)]
        
        # Delays should generally increase (accounting for jitter)
        # Check that later delays are in expected ranges
        self.assertGreater(delays[2], base_delay)  # 3rd attempt should be > base
        self.assertGreater(delays[4], delays[0])  # 5th should be > 1st

    def test_backoff_respects_max_delay(self):
        """Backoff should be capped at max_delay."""
        max_delay = 2.0
        delay = calculate_backoff_delay(10, base_delay=0.5, max_delay=max_delay)
        self.assertLessEqual(delay, max_delay * 1.25)  # Allow for jitter

    def test_backoff_has_jitter(self):
        """Backoff should include jitter (randomization)."""
        # Generate multiple delays for same attempt
        delays = [calculate_backoff_delay(2, base_delay=1.0, max_delay=100) 
                  for _ in range(10)]
        
        # Not all delays should be exactly the same due to jitter
        unique_delays = set(round(d, 6) for d in delays)
        self.assertGreater(len(unique_delays), 1)


class TestSendWithRetry(unittest.TestCase):
    """Tests for send_with_retry functionality."""

    def setUp(self):
        """Reset circuit breaker and create mock producer."""
        reset_circuit_breaker()
        self.mock_producer = Mock()

    def tearDown(self):
        """Clean up after tests."""
        reset_circuit_breaker()

    def test_successful_send(self):
        """Successful send should return True and record success."""
        # Setup mock
        mock_future = Mock()
        mock_metadata = Mock()
        mock_metadata.partition = 0
        mock_metadata.offset = 100
        mock_future.get.return_value = mock_metadata
        self.mock_producer.send.return_value = mock_future

        result = send_with_retry(
            self.mock_producer, 
            "test-topic", 
            {"test": "message"},
            max_retries=3
        )

        self.assertTrue(result)
        self.mock_producer.send.assert_called_once()
        self.mock_producer.flush.assert_called()

    def test_retries_on_timeout(self):
        """Should retry on KafkaTimeoutError."""
        from kafka.errors import KafkaTimeoutError
        
        # First two calls fail, third succeeds
        mock_future_fail = Mock()
        mock_future_fail.get.side_effect = KafkaTimeoutError("Timeout")
        
        mock_future_success = Mock()
        mock_metadata = Mock()
        mock_metadata.partition = 0
        mock_metadata.offset = 100
        mock_future_success.get.return_value = mock_metadata
        
        self.mock_producer.send.side_effect = [
            mock_future_fail, 
            mock_future_fail, 
            mock_future_success
        ]

        with patch('scan.time.sleep'):  # Skip actual sleep
            result = send_with_retry(
                self.mock_producer,
                "test-topic",
                {"test": "message"},
                max_retries=3
            )

        self.assertTrue(result)
        self.assertEqual(self.mock_producer.send.call_count, 3)

    def test_fails_after_max_retries(self):
        """Should return False after exhausting retries."""
        from kafka.errors import KafkaTimeoutError
        
        mock_future = Mock()
        mock_future.get.side_effect = KafkaTimeoutError("Timeout")
        self.mock_producer.send.return_value = mock_future

        with patch('scan.time.sleep'):  # Skip actual sleep
            result = send_with_retry(
                self.mock_producer,
                "test-topic",
                {"test": "message"},
                max_retries=2
            )

        self.assertFalse(result)
        self.assertEqual(self.mock_producer.send.call_count, 3)  # Initial + 2 retries

    def test_circuit_breaker_blocks_send(self):
        """Should raise CircuitBreakerOpen when circuit is open."""
        # Open the circuit
        for i in range(CIRCUIT_BREAKER_FAILURE_THRESHOLD):
            record_circuit_breaker_failure(Exception(f"Test error {i}"))

        with self.assertRaises(CircuitBreakerOpen):
            send_with_retry(
                self.mock_producer,
                "test-topic",
                {"test": "message"}
            )

        # Producer should not have been called
        self.mock_producer.send.assert_not_called()

    def test_records_failure_with_circuit_breaker(self):
        """Failed sends should be recorded with circuit breaker."""
        from kafka.errors import KafkaError
        
        mock_future = Mock()
        mock_future.get.side_effect = KafkaError("Send failed")
        self.mock_producer.send.return_value = mock_future

        initial_failures = circuit_breaker_state["failure_count"]

        with patch('scan.time.sleep'):
            result = send_with_retry(
                self.mock_producer,
                "test-topic",
                {"test": "message"},
                max_retries=0
            )

        self.assertFalse(result)
        self.assertGreater(circuit_breaker_state["failure_count"], initial_failures)


class TestKafkaProducerManagement(unittest.TestCase):
    """Tests for Kafka producer lifecycle management."""

    def setUp(self):
        """Reset global state before each test."""
        reset_circuit_breaker()
        close_kafka_producer()

    def tearDown(self):
        """Clean up after tests."""
        reset_circuit_breaker()
        close_kafka_producer()

    @patch('scan.REX_KAFKA_BOOTSTRAP_SERVERS', None)
    def test_get_producer_returns_none_when_not_configured(self):
        """Should return None when bootstrap servers not configured."""
        result = get_kafka_producer()
        self.assertIsNone(result)

    @patch('scan.REX_KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
    @patch('scan.KafkaProducer')
    def test_get_producer_creates_new_producer(self, mock_kafka_producer_class):
        """Should create a new producer when none exists."""
        mock_producer = Mock()
        mock_kafka_producer_class.return_value = mock_producer

        result = get_kafka_producer()

        self.assertEqual(result, mock_producer)
        mock_kafka_producer_class.assert_called_once()

    @patch('scan.REX_KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
    @patch('scan.KafkaProducer')
    def test_get_producer_reuses_existing_producer(self, mock_kafka_producer_class):
        """Should reuse existing producer on subsequent calls."""
        mock_producer = Mock()
        mock_kafka_producer_class.return_value = mock_producer

        result1 = get_kafka_producer()
        result2 = get_kafka_producer()

        self.assertEqual(result1, result2)
        mock_kafka_producer_class.assert_called_once()  # Only created once

    @patch('scan.REX_KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
    @patch('scan.KafkaProducer')
    def test_recreate_producer_closes_old_and_creates_new(self, mock_kafka_producer_class):
        """Should close old producer and create new one."""
        mock_producer1 = Mock()
        mock_producer2 = Mock()
        mock_kafka_producer_class.side_effect = [mock_producer1, mock_producer2]

        # Create initial producer
        result1 = get_kafka_producer()
        self.assertEqual(result1, mock_producer1)

        # Recreate
        result2 = recreate_kafka_producer()
        
        self.assertEqual(result2, mock_producer2)
        mock_producer1.close.assert_called_once()
        self.assertEqual(mock_kafka_producer_class.call_count, 2)


class TestKafkaScanFunctions(unittest.TestCase):
    """Tests for kafka_start_scan and kafka_scan_results with retry logic."""

    def setUp(self):
        """Reset circuit breaker before each test."""
        reset_circuit_breaker()
        self.mock_producer = Mock()
        self.mock_s3_object = Mock()
        self.mock_s3_object.bucket_name = "test-bucket"
        self.mock_s3_object.key = "test-key"
        self.mock_s3_object.version_id = "v1"

    def tearDown(self):
        """Clean up after tests."""
        reset_circuit_breaker()

    @patch('scan.send_with_retry')
    def test_kafka_start_scan_success(self, mock_send):
        """kafka_start_scan should return True on success."""
        mock_send.return_value = True

        result = kafka_start_scan(
            self.mock_producer,
            self.mock_s3_object,
            "scan-start-topic",
            "2024-01-01 00:00:00"
        )

        self.assertTrue(result)
        mock_send.assert_called_once()

    @patch('scan.send_with_retry')
    def test_kafka_start_scan_failure(self, mock_send):
        """kafka_start_scan should return False on failure."""
        mock_send.return_value = False

        result = kafka_start_scan(
            self.mock_producer,
            self.mock_s3_object,
            "scan-start-topic",
            "2024-01-01 00:00:00"
        )

        self.assertFalse(result)

    @patch('scan.send_with_retry')
    def test_kafka_start_scan_circuit_breaker_open(self, mock_send):
        """kafka_start_scan should handle circuit breaker open gracefully."""
        mock_send.side_effect = CircuitBreakerOpen("Circuit open")

        result = kafka_start_scan(
            self.mock_producer,
            self.mock_s3_object,
            "scan-start-topic",
            "2024-01-01 00:00:00"
        )

        self.assertFalse(result)

    @patch('scan.send_with_retry')
    @patch('scan.AV_STATUS_PUBLISH_CLEAN', 'True')
    def test_kafka_scan_results_success(self, mock_send):
        """kafka_scan_results should return True on success."""
        mock_send.return_value = True

        result = kafka_scan_results(
            self.mock_producer,
            self.mock_s3_object,
            "CLEAN",
            "OK",
            "2024-01-01 00:00:00"
        )

        self.assertTrue(result)
        mock_send.assert_called_once()

    @patch('scan.send_with_retry')
    @patch('scan.AV_STATUS_PUBLISH_CLEAN', 'False')
    def test_kafka_scan_results_skips_clean_when_disabled(self, mock_send):
        """kafka_scan_results should skip publishing when disabled for CLEAN."""
        result = kafka_scan_results(
            self.mock_producer,
            self.mock_s3_object,
            "CLEAN",
            "OK",
            "2024-01-01 00:00:00"
        )

        self.assertTrue(result)  # Returns True (skipped successfully)
        mock_send.assert_not_called()

    @patch('scan.send_with_retry')
    @patch('scan.AV_STATUS_PUBLISH_INFECTED', 'False')
    def test_kafka_scan_results_skips_infected_when_disabled(self, mock_send):
        """kafka_scan_results should skip publishing when disabled for INFECTED."""
        result = kafka_scan_results(
            self.mock_producer,
            self.mock_s3_object,
            "INFECTED",
            "Virus.Test",
            "2024-01-01 00:00:00"
        )

        self.assertTrue(result)  # Returns True (skipped successfully)
        mock_send.assert_not_called()
