#!/usr/bin/env bash
# Provisions the Kinesis Data Stream that the fetcher publishes to and the
# processor consumes from. Idempotent: re-running confirms the stream is
# ACTIVE; it does not re-create on top of an existing one.
#
# Cost note: 1 provisioned shard is ~$0.015/hour (~$11/month if left up).
# Use ./infra/scripts/99-teardown.sh to delete it at end of session.
#
# Usage:
#   ./infra/scripts/05-create-kinesis-stream.sh

set -euo pipefail

STREAM_NAME="financial-price-events"
SHARD_COUNT=1

if aws kinesis describe-stream-summary --stream-name "${STREAM_NAME}" >/dev/null 2>&1; then
  echo "    Stream ${STREAM_NAME} already exists; skipping create."
else
  echo "==> Creating Kinesis stream ${STREAM_NAME} (${SHARD_COUNT} shard, PROVISIONED)"
  aws kinesis create-stream \
    --stream-name "${STREAM_NAME}" \
    --shard-count "${SHARD_COUNT}"
fi

echo "==> Waiting for stream to become ACTIVE (typically 30-60s)"
aws kinesis wait stream-exists --stream-name "${STREAM_NAME}"

STREAM_ARN=$(aws kinesis describe-stream-summary \
  --stream-name "${STREAM_NAME}" \
  --query 'StreamDescriptionSummary.StreamARN' --output text)
RETENTION=$(aws kinesis describe-stream-summary \
  --stream-name "${STREAM_NAME}" \
  --query 'StreamDescriptionSummary.RetentionPeriodHours' --output text)

echo
echo "Stream ARN:        ${STREAM_ARN}"
echo "Retention (hours): ${RETENTION}   (default 24 — fine for this project)"
echo
echo "Export the name for the fetcher redeploy:"
echo "  export KINESIS_STREAM_NAME=\"${STREAM_NAME}\""
echo
echo "Tear down at end of session to avoid \$0.36/day shard cost:"
echo "  ./infra/scripts/99-teardown.sh"
