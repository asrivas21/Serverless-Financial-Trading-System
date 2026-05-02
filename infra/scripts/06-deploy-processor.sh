#!/usr/bin/env bash
# Creates (or updates) the processor Lambda function. Idempotent.
# Requires the IAM role from 01.
#
# No layer needed for the processor: it only uses stdlib + boto3 (provided
# by the Lambda runtime). Phase 3 will add boto3 calls to DynamoDB and S3
# but those don't add dependencies.
#
# Usage:
#   ./infra/scripts/06-deploy-processor.sh

set -euo pipefail

FUNCTION_NAME="financial-pipeline-processor"
ROLE_NAME="financial-pipeline-processor-role"
RUNTIME="python3.12"
HANDLER="handler.lambda_handler"
TIMEOUT=60
MEMORY=256

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="${WORKSPACE_ROOT}/lambdas/processor/build"
ZIP_PATH="${WORKSPACE_ROOT}/lambdas/processor/processor.zip"

echo "==> Building processor zip (handler.py only)"
rm -rf "${BUILD_DIR}" "${ZIP_PATH}"
mkdir -p "${BUILD_DIR}"
cp "${WORKSPACE_ROOT}/lambdas/processor/handler.py" "${BUILD_DIR}/handler.py"
( cd "${BUILD_DIR}" && zip -q "${ZIP_PATH}" handler.py )
echo "    Processor zip: ${ZIP_PATH} ($(du -k "${ZIP_PATH}" | cut -f1) KB)"

ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)

if aws lambda get-function --function-name "${FUNCTION_NAME}" >/dev/null 2>&1; then
  echo "==> Updating function ${FUNCTION_NAME}"
  aws lambda update-function-code \
    --function-name "${FUNCTION_NAME}" \
    --zip-file "fileb://${ZIP_PATH}" >/dev/null
  aws lambda wait function-updated --function-name "${FUNCTION_NAME}"
  aws lambda update-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --runtime "${RUNTIME}" \
    --handler "${HANDLER}" \
    --timeout "${TIMEOUT}" \
    --memory-size "${MEMORY}" >/dev/null
else
  echo "==> Creating function ${FUNCTION_NAME}"
  aws lambda create-function \
    --function-name "${FUNCTION_NAME}" \
    --runtime "${RUNTIME}" \
    --handler "${HANDLER}" \
    --role "${ROLE_ARN}" \
    --zip-file "fileb://${ZIP_PATH}" \
    --timeout "${TIMEOUT}" \
    --memory-size "${MEMORY}" >/dev/null
fi

echo "==> Setting CloudWatch Logs retention to 7 days"
LOG_GROUP="/aws/lambda/${FUNCTION_NAME}"
aws logs create-log-group --log-group-name "${LOG_GROUP}" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "${LOG_GROUP}" --retention-in-days 7

echo
echo "==> Done. Next: ./infra/scripts/07-create-event-source-mapping.sh"
echo "    (creates the SQS DLQ and wires Kinesis -> processor)"
