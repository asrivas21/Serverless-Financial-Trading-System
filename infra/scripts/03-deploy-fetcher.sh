#!/usr/bin/env bash
# Creates (or updates) the fetcher Lambda function. Idempotent.
# Requires the IAM role from 01 and the layer ARN from 02.
#
# Usage:
#   export FETCHER_LAYER_ARN=arn:aws:lambda:us-east-1:...:layer:financial-pipeline-yfinance:1
#   export FINNHUB_API_KEY=xxxxxxxxxxxxxxxxxxxx   # from https://finnhub.io dashboard
#   # optional: override the default ticker list
#   export TICKERS="AAPL,TSLA,BTC-USD,ETH-USD"
#   # optional (Phase 2+): publish to Kinesis. Unset = fetch-only (Phase 1 mode).
#   export KINESIS_STREAM_NAME="financial-price-events"
#   ./infra/scripts/03-deploy-fetcher.sh

set -euo pipefail

FUNCTION_NAME="financial-pipeline-fetcher"
ROLE_NAME="financial-pipeline-fetcher-role"
RUNTIME="python3.12"
HANDLER="handler.lambda_handler"
TIMEOUT=60
MEMORY=256
DEFAULT_TICKERS="AAPL,TSLA,BTC-USD,ETH-USD"

LAYER_ARN="${FETCHER_LAYER_ARN:?Set FETCHER_LAYER_ARN to the ARN printed by 02-build-fetcher-layer.sh}"
FINNHUB_API_KEY="${FINNHUB_API_KEY:?Set FINNHUB_API_KEY (sign up free at https://finnhub.io)}"
TICKERS="${TICKERS:-$DEFAULT_TICKERS}"
# KINESIS_STREAM_NAME is optional: when unset, the Lambda skips publishing
# (useful for the Phase 1 fetch-only smoke test).
KINESIS_STREAM_NAME="${KINESIS_STREAM_NAME:-}"

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="${WORKSPACE_ROOT}/lambdas/fetcher/build"
ZIP_PATH="${WORKSPACE_ROOT}/lambdas/fetcher/fetcher.zip"

echo "==> Building fetcher zip (handler.py only — runtime deps live in the layer)"
rm -rf "${BUILD_DIR}" "${ZIP_PATH}"
mkdir -p "${BUILD_DIR}"
cp "${WORKSPACE_ROOT}/lambdas/fetcher/handler.py" "${BUILD_DIR}/handler.py"
( cd "${BUILD_DIR}" && zip -q "${ZIP_PATH}" handler.py )
echo "    Fetcher zip: ${ZIP_PATH} ($(du -k "${ZIP_PATH}" | cut -f1) KB)"

ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)

# Use JSON for --environment instead of shorthand: shorthand uses commas to
# separate key=value pairs, which collides with the commas in our TICKERS list.
ENV_JSON=$(printf \
  '{"Variables":{"TICKERS":"%s","FINNHUB_API_KEY":"%s","KINESIS_STREAM_NAME":"%s"}}' \
  "${TICKERS}" "${FINNHUB_API_KEY}" "${KINESIS_STREAM_NAME}")

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
    --memory-size "${MEMORY}" \
    --layers "${LAYER_ARN}" \
    --environment "${ENV_JSON}" >/dev/null
else
  echo "==> Creating function ${FUNCTION_NAME}"
  aws lambda create-function \
    --function-name "${FUNCTION_NAME}" \
    --runtime "${RUNTIME}" \
    --handler "${HANDLER}" \
    --role "${ROLE_ARN}" \
    --zip-file "fileb://${ZIP_PATH}" \
    --timeout "${TIMEOUT}" \
    --memory-size "${MEMORY}" \
    --layers "${LAYER_ARN}" \
    --environment "${ENV_JSON}" >/dev/null
fi

echo "==> Setting CloudWatch Logs retention to 7 days"
LOG_GROUP="/aws/lambda/${FUNCTION_NAME}"
aws logs create-log-group --log-group-name "${LOG_GROUP}" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "${LOG_GROUP}" --retention-in-days 7

echo
echo "==> Done. Smoke-test the function:"
echo "    aws lambda invoke --function-name ${FUNCTION_NAME} /tmp/fetcher-out.json"
echo "    cat /tmp/fetcher-out.json | python3 -m json.tool"
