#!/usr/bin/env bash
# Creates a CloudWatch billing alarm on the AWS/Billing EstimatedCharges metric.
# Billing metrics are only published in us-east-1 regardless of where workloads run.
# Idempotent: re-running updates the alarm and SNS topic in place.
#
# Usage:
#   BILLING_ALERT_EMAIL=you@example.com BILLING_ALERT_THRESHOLD_USD=5 \
#     ./infra/scripts/00-billing-alarm.sh

set -euo pipefail

: "${BILLING_ALERT_EMAIL:?Set BILLING_ALERT_EMAIL to the address that should receive alerts}"
THRESHOLD="${BILLING_ALERT_THRESHOLD_USD:-5}"
TOPIC_NAME="financial-pipeline-billing-alerts"
ALARM_NAME="financial-pipeline-billing-over-${THRESHOLD}usd"
REGION="us-east-1"

echo "==> Ensuring billing alerts are receivable (must also be enabled in Billing console once)"
echo "    https://console.aws.amazon.com/billing/home#/preferences"

echo "==> Creating SNS topic ${TOPIC_NAME} in ${REGION}"
TOPIC_ARN=$(aws sns create-topic \
  --name "${TOPIC_NAME}" \
  --region "${REGION}" \
  --query TopicArn --output text)
echo "    Topic ARN: ${TOPIC_ARN}"

echo "==> Subscribing ${BILLING_ALERT_EMAIL} (confirm via the email AWS sends)"
aws sns subscribe \
  --topic-arn "${TOPIC_ARN}" \
  --protocol email \
  --notification-endpoint "${BILLING_ALERT_EMAIL}" \
  --region "${REGION}" >/dev/null

echo "==> Creating CloudWatch alarm ${ALARM_NAME} at \$${THRESHOLD}"
aws cloudwatch put-metric-alarm \
  --alarm-name "${ALARM_NAME}" \
  --alarm-description "Alerts when estimated AWS charges exceed \$${THRESHOLD}." \
  --namespace "AWS/Billing" \
  --metric-name "EstimatedCharges" \
  --dimensions Name=Currency,Value=USD \
  --statistic Maximum \
  --period 21600 \
  --evaluation-periods 1 \
  --threshold "${THRESHOLD}" \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "${TOPIC_ARN}" \
  --region "${REGION}"

echo "==> Done. Confirm the SNS subscription email to start receiving alerts."
