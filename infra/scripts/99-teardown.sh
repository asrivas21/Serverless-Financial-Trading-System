#!/usr/bin/env bash
# Deletes paid resources to keep monthly cost near zero between dev sessions.
# Safe to run at the end of any working session; recreate via 05+ scripts next time.
#
# Currently tears down:
#   - Kinesis stream `financial-price-events`         (~$0.36/day if left up)
#
# Future phases will extend this with:
#   - DynamoDB table   (on-demand: $0 at rest, only pay per request)
#   - SQS DLQ          (free tier covers it)
#
# Lambda functions, IAM roles, log groups, and the EventBridge rule are NOT
# torn down — they cost nothing at rest and recreating them is friction.
#
# Usage:
#   ./infra/scripts/99-teardown.sh

set -euo pipefail

STREAM_NAME="financial-price-events"
PROCESSOR_NAME="financial-pipeline-processor"

# ---- 1. Event source mapping (orphans if its stream is deleted out from
#         under it; Lambda console then shows a permanent "PROBLEM" state)
MAPPING_UUID=$(aws lambda list-event-source-mappings \
  --function-name "${PROCESSOR_NAME}" \
  --query 'EventSourceMappings[0].UUID' --output text 2>/dev/null || echo "None")
if [ "${MAPPING_UUID}" != "None" ] && [ -n "${MAPPING_UUID}" ]; then
  echo "==> Deleting event source mapping ${MAPPING_UUID}"
  aws lambda delete-event-source-mapping --uuid "${MAPPING_UUID}" >/dev/null
fi

# ---- 2. Kinesis stream (paid: ~$0.36/day per shard) ----
if aws kinesis describe-stream-summary --stream-name "${STREAM_NAME}" >/dev/null 2>&1; then
  echo "==> Deleting Kinesis stream ${STREAM_NAME}"
  aws kinesis delete-stream --stream-name "${STREAM_NAME}" --enforce-consumer-deletion
  echo "==> Waiting for delete to complete"
  aws kinesis wait stream-not-exists --stream-name "${STREAM_NAME}"
  echo "    Deleted."
else
  echo "    Stream ${STREAM_NAME} not present; nothing to delete."
fi

echo
echo "==> Teardown complete. Recreate with:"
echo "    ./infra/scripts/05-create-kinesis-stream.sh"
echo "    ./infra/scripts/07-create-event-source-mapping.sh"
