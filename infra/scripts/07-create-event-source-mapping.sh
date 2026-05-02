#!/usr/bin/env bash
# Creates the SQS dead-letter queue for the processor and wires the Kinesis
# stream to the processor Lambda via an event source mapping.
#
# Idempotent: re-running confirms the DLQ exists and updates the existing
# mapping rather than creating a duplicate.
#
# Mapping config (per F-01 spec):
#   - BatchSize 10
#   - MaximumBatchingWindowInSeconds 1   (low end-to-end latency)
#   - BisectBatchOnFunctionError true     (isolate poison records)
#   - MaximumRetryAttempts 3              (don't infinitely retry)
#   - StartingPosition LATEST             (don't reprocess test data)
#   - FunctionResponseTypes ReportBatchItemFailures (per-record DLQ routing)
#   - OnFailure -> SQS DLQ
#
# Usage:
#   ./infra/scripts/07-create-event-source-mapping.sh

set -euo pipefail

FUNCTION_NAME="financial-pipeline-processor"
STREAM_NAME="financial-price-events"
DLQ_NAME="financial-pipeline-processor-dlq"

# ---- 1. SQS DLQ ----
if aws sqs get-queue-url --queue-name "${DLQ_NAME}" >/dev/null 2>&1; then
  echo "    DLQ ${DLQ_NAME} already exists; skipping create."
else
  echo "==> Creating SQS DLQ ${DLQ_NAME}"
  aws sqs create-queue \
    --queue-name "${DLQ_NAME}" \
    --attributes 'MessageRetentionPeriod=1209600' >/dev/null
fi

DLQ_URL=$(aws sqs get-queue-url --queue-name "${DLQ_NAME}" --query 'QueueUrl' --output text)
DLQ_ARN=$(aws sqs get-queue-attributes \
  --queue-url "${DLQ_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)
echo "    DLQ ARN: ${DLQ_ARN}"

# ---- 2. Resolve stream + function ARNs ----
STREAM_ARN=$(aws kinesis describe-stream-summary \
  --stream-name "${STREAM_NAME}" \
  --query 'StreamDescriptionSummary.StreamARN' --output text)
FUNCTION_ARN=$(aws lambda get-function \
  --function-name "${FUNCTION_NAME}" \
  --query 'Configuration.FunctionArn' --output text)

# ---- 3. Event source mapping ----
EXISTING_UUID=$(aws lambda list-event-source-mappings \
  --function-name "${FUNCTION_NAME}" \
  --event-source-arn "${STREAM_ARN}" \
  --query 'EventSourceMappings[0].UUID' --output text 2>/dev/null || echo "None")

DEST_CONFIG=$(printf '{"OnFailure":{"Destination":"%s"}}' "${DLQ_ARN}")

if [ "${EXISTING_UUID}" != "None" ] && [ -n "${EXISTING_UUID}" ]; then
  echo "==> Updating existing event source mapping ${EXISTING_UUID}"
  aws lambda update-event-source-mapping \
    --uuid "${EXISTING_UUID}" \
    --batch-size 10 \
    --maximum-batching-window-in-seconds 1 \
    --bisect-batch-on-function-error \
    --maximum-retry-attempts 3 \
    --function-response-types ReportBatchItemFailures \
    --destination-config "${DEST_CONFIG}" >/dev/null
else
  echo "==> Creating event source mapping (Kinesis -> ${FUNCTION_NAME})"
  aws lambda create-event-source-mapping \
    --function-name "${FUNCTION_NAME}" \
    --event-source-arn "${STREAM_ARN}" \
    --starting-position LATEST \
    --batch-size 10 \
    --maximum-batching-window-in-seconds 1 \
    --bisect-batch-on-function-error \
    --maximum-retry-attempts 3 \
    --function-response-types ReportBatchItemFailures \
    --destination-config "${DEST_CONFIG}" >/dev/null
fi

echo
echo "==> Done. Verify with:"
echo "    aws lambda list-event-source-mappings --function-name ${FUNCTION_NAME}"
echo
echo "==> End-to-end smoke test:"
echo "    aws lambda invoke --function-name financial-pipeline-fetcher /tmp/out.json"
echo "    sleep 3"
echo "    aws logs tail /aws/lambda/${FUNCTION_NAME} --since 1m"
