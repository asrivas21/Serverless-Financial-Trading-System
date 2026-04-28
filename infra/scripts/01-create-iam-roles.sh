#!/usr/bin/env bash
# Provisions IAM execution roles for the three Lambdas plus a programmatic
# deploy user for GitHub Actions. Idempotent: re-running creates missing
# resources and updates inline policies in place.
#
# Resource ARNs in the policy JSON files reference the *intended* names of
# the Kinesis stream, DynamoDB table, S3 bucket, and SQS DLQ. They will be
# created in Phases 2/3; the policies are valid before those resources exist.
#
# Usage:
#   ./infra/scripts/01-create-iam-roles.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IAM_DIR="$(cd "${SCRIPT_DIR}/../iam" && pwd)"
TRUST="file://${IAM_DIR}/lambda-trust-policy.json"

create_or_update_role() {
  local role_name="$1"
  local policy_file="$2"
  local policy_name="$3"

  if aws iam get-role --role-name "${role_name}" >/dev/null 2>&1; then
    echo "    Role ${role_name} already exists; skipping create."
  else
    echo "==> Creating role ${role_name}"
    aws iam create-role \
      --role-name "${role_name}" \
      --assume-role-policy-document "${TRUST}" >/dev/null
  fi

  echo "==> Putting inline policy ${policy_name} on ${role_name}"
  aws iam put-role-policy \
    --role-name "${role_name}" \
    --policy-name "${policy_name}" \
    --policy-document "file://${policy_file}"
}

create_or_update_role \
  "financial-pipeline-fetcher-role" \
  "${IAM_DIR}/fetcher-policy.json" \
  "fetcher-inline"

create_or_update_role \
  "financial-pipeline-processor-role" \
  "${IAM_DIR}/processor-policy.json" \
  "processor-inline"

create_or_update_role \
  "financial-pipeline-query-role" \
  "${IAM_DIR}/query-policy.json" \
  "query-inline"

# ---- GitHub Actions deploy user ----
DEPLOY_USER="financial-pipeline-ci"
if aws iam get-user --user-name "${DEPLOY_USER}" >/dev/null 2>&1; then
  echo "    User ${DEPLOY_USER} already exists; skipping create."
else
  echo "==> Creating IAM user ${DEPLOY_USER} (programmatic only, no console)"
  aws iam create-user --user-name "${DEPLOY_USER}" >/dev/null
fi

echo "==> Attaching deploy policy to ${DEPLOY_USER}"
aws iam put-user-policy \
  --user-name "${DEPLOY_USER}" \
  --policy-name "github-actions-deploy" \
  --policy-document "file://${IAM_DIR}/github-actions-deploy-policy.json"

echo
echo "==> Done."
echo
echo "Next: generate an access key for ${DEPLOY_USER} and store it as GitHub Secrets."
echo "    aws iam create-access-key --user-name ${DEPLOY_USER}"
echo "    Then in GitHub: Settings -> Secrets and variables -> Actions -> New repository secret"
echo "      AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION=us-east-1"
echo
echo "Rotate the key (or delete and recreate) periodically. Never commit it."
