# Migration from SNS to MSK/Kafka

This document describes the changes made to migrate from SNS publishing to MSK (Amazon Managed Streaming for Apache Kafka).

## Changes Made

### Dependencies
- Added `kafka-python` library to `requirements.txt`

### Configuration Variables
The following environment variables have been updated in `common.py`:

**Old SNS Configuration:**
- `AV_SCAN_START_SNS_ARN` → `AV_SCAN_START_TOPIC`
- `AV_STATUS_SNS_ARN` → `REX_KAFKA_TOPIC_AVSCAN_RESPONSE`
- `AV_STATUS_SNS_PUBLISH_CLEAN` → `AV_STATUS_PUBLISH_CLEAN`
- `AV_STATUS_SNS_PUBLISH_INFECTED` → `AV_STATUS_PUBLISH_INFECTED`

**New Kafka Configuration:**
- `REX_KAFKA_BOOTSTRAP_SERVERS` - Comma-separated list of Kafka bootstrap servers
- `AV_SCAN_START_TOPIC` - Topic name for scan start notifications
- `REX_KAFKA_TOPIC_AVSCAN_RESPONSE` - Topic name for scan results

### Code Changes

1. **Replaced SNS functions with Kafka equivalents:**
   - `sns_start_scan()` → `kafka_start_scan()`
   - `sns_scan_results()` → `kafka_scan_results()`

2. **Updated lambda_handler:**
   - Creates a global Kafka producer that persists across Lambda invocations
   - Includes health checks and automatic reconnection for stale connections
   - Optimized for Lambda environment with appropriate timeouts and settings
   - Added error handling for Kafka connection issues

## Environment Configuration

Set the following environment variables:

```bash
# Required: Kafka bootstrap servers (comma-separated)
REX_KAFKA_BOOTSTRAP_SERVERS=kafka-broker1:9092,kafka-broker2:9092,kafka-broker3:9092

# Optional: Custom topic names
AV_SCAN_START_TOPIC=av-scan-start
REX_KAFKA_TOPIC_AVSCAN_RESPONSE=av-scan-results

# Optional: Control what results to publish (defaults to True)
AV_STATUS_PUBLISH_CLEAN=True
AV_STATUS_PUBLISH_INFECTED=True
```

## Message Format

Messages are sent as JSON to the specified Kafka topics:

### Scan Start Message
```json
{
  "bucket": "my-bucket",
  "key": "path/to/file.txt",
  "version": "version-id",
  "av-scan-start": true,
  "av-timestamp": "2024/01/15 10:30:00 UTC"
}
```

### Scan Results Message
```json
{
  "bucket": "my-bucket",
  "key": "path/to/file.txt",
  "version": "version-id",
  "av-signature": "OK",
  "av-status": "CLEAN",
  "av-timestamp": "2024/01/15 10:30:05 UTC"
}
```

## Lambda Performance Optimization

The Kafka producer is designed to persist across Lambda invocations for optimal performance:

### How It Works
- **Global Producer**: The Kafka producer is created in global scope, outside the handler function
- **Container Reuse**: When AWS reuses Lambda containers, the producer connection is maintained
- **Health Checks**: Each invocation checks if the existing connection is still healthy
- **Auto-Reconnect**: If the connection is stale, a new producer is automatically created
- **No Cleanup**: The producer is not closed at the end of invocations to allow reuse

### Performance Benefits
- **Faster Cold Starts**: Subsequent invocations skip connection setup time
- **Reduced Latency**: No SSL handshake or authentication overhead on warm starts
- **Lower Resource Usage**: Fewer network connections and memory allocations
- **Better Throughput**: Connection pooling improves message publishing performance

### Configuration
The producer includes Lambda-optimized settings:
```python
KafkaProducer(
    bootstrap_servers=REX_KAFKA_BOOTSTRAP_SERVERS.split(','),
    request_timeout_ms=30000,      # 30 second timeout
    retry_backoff_ms=500,          # Fast retry for Lambda environment
    max_in_flight_requests_per_connection=1,  # Ensure message ordering
    acks='all'                     # Wait for all replicas (durability)
)
```
