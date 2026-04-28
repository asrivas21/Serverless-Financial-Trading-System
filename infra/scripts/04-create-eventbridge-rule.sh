#!/usr/bin/env bash
# Creates an EventBridge cron rule that fires every minute and targets the
# fetcher Lambda. Idempotent. Safe to disable/enable via aws events
# disable-rule / enable-rule to control invocation costs.
#
# Usage:
#   ./infra/scripts/04-create-eventbridge-rule.sh

set -euo pipefail

RULE_NAME="financial-pipeline-fetcher-cron"
FUNCTION_NAME="financial-pipeline-fetcher"
SCHEDULE="rate(1 minute)"
STATEMENT_ID="AllowExecutionFromEventBridge"

echo "==> Creating EventBridge rule ${RULE_NAME} (${SCHEDULE})"
RULE_ARN=$(aws events put-rule \
  --name "${RULE_NAME}" \
  --schedule-expression "${SCHEDULE}" \
  --description "Triggers the financial-pipeline fetcher Lambda once per minute" \
  --state ENABLED \
  --query 'RuleArn' --output text)
echo "    Rule ARN: ${RULE_ARN}"

FUNCTION_ARN=$(aws lambda get-function \
  --function-name "${FUNCTION_NAME}" \
  --query 'Configuration.FunctionArn' --output text)

echo "==> Adding ${FUNCTION_NAME} as the rule target"
aws events put-targets \
  --rule "${RULE_NAME}" \
  --targets "Id=1,Arn=${FUNCTION_ARN}" >/dev/null

echo "==> Granting EventBridge permission to invoke the Lambda"
aws lambda remove-permission \
  --function-name "${FUNCTION_NAME}" \
  --statement-id "${STATEMENT_ID}" 2>/dev/null || true
aws lambda add-permission \
  --function-name "${FUNCTION_NAME}" \
  --statement-id "${STATEMENT_ID}" \
  --action "lambda:InvokeFunction" \
  --principal "events.amazonaws.com" \
  --source-arn "${RULE_ARN}" >/dev/null

echo
echo "==> Done. Tail logs to verify minute-by-minute invocations:"
echo "    aws logs tail /aws/lambda/${FUNCTION_NAME} --follow"
echo
echo "==> Pause invocations (recommended until Phase 2 wires Kinesis):"
echo "    aws events disable-rule --name ${RULE_NAME}"
echo "==> Resume:"
echo "    aws events enable-rule --name ${RULE_NAME}"
