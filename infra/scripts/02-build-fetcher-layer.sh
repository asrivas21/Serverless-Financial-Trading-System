#!/usr/bin/env bash
# Builds and publishes a Lambda layer containing yfinance + requests.
# boto3/botocore/s3transfer/jmespath are stripped — the Lambda runtime
# already provides them.
#
# Idempotent: re-running publishes a new layer version. The new ARN is
# printed at the end and must be passed to 03-deploy-fetcher.sh via
# FETCHER_LAYER_ARN.
#
# Usage:
#   ./infra/scripts/02-build-fetcher-layer.sh

set -euo pipefail

LAYER_NAME="financial-pipeline-yfinance"
PYTHON_VERSION="3.12"
ARCH="x86_64"
PLATFORM="manylinux2014_x86_64"

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="${WORKSPACE_ROOT}/layers/yfinance"
ZIP_PATH="${WORKSPACE_ROOT}/layers/yfinance-layer.zip"

echo "==> Cleaning ${BUILD_DIR} and ${ZIP_PATH}"
rm -rf "${BUILD_DIR}" "${ZIP_PATH}"
mkdir -p "${BUILD_DIR}/python"

echo "==> Installing runtime deps for Lambda (${PLATFORM}, python ${PYTHON_VERSION})"
python3.12 -m pip install \
  --platform "${PLATFORM}" \
  --target "${BUILD_DIR}/python" \
  --implementation cp \
  --python-version "${PYTHON_VERSION}" \
  --only-binary=:all: \
  --upgrade \
  -r "${WORKSPACE_ROOT}/lambdas/requirements.txt"

echo "==> Removing boto3/botocore/s3transfer/jmespath (provided by Lambda runtime)"
( cd "${BUILD_DIR}/python" && \
  rm -rf boto3 boto3-* botocore botocore-* s3transfer s3transfer-* jmespath jmespath-* )

echo "==> Pruning bytecode and test artifacts"
find "${BUILD_DIR}/python" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "${BUILD_DIR}/python" -name 'tests' -type d -prune -exec rm -rf {} +

echo "==> Zipping layer"
( cd "${BUILD_DIR}" && zip -qr "${ZIP_PATH}" python )
SIZE_MB=$(du -m "${ZIP_PATH}" | cut -f1)
echo "    Layer zip: ${ZIP_PATH} (${SIZE_MB} MB)"

if [ "${SIZE_MB}" -gt 70 ]; then
  echo "    NOTE: zipped layer >70MB. Lambda's hard limit is 50MB for direct upload;"
  echo "    layers larger than that must be uploaded via S3. Adjust the script if hit."
fi

echo "==> Publishing layer ${LAYER_NAME}"
LAYER_ARN=$(aws lambda publish-layer-version \
  --layer-name "${LAYER_NAME}" \
  --description "yfinance + requests for the fetcher Lambda" \
  --zip-file "fileb://${ZIP_PATH}" \
  --compatible-runtimes "python${PYTHON_VERSION}" \
  --compatible-architectures "${ARCH}" \
  --query 'LayerVersionArn' \
  --output text)

echo
echo "Published layer ARN:"
echo "  ${LAYER_ARN}"
echo
echo "Export it for the deploy script:"
echo "  export FETCHER_LAYER_ARN=\"${LAYER_ARN}\""
