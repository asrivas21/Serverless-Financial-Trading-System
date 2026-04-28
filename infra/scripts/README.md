# infra/scripts

Bootstrap scripts for Phase 0. Run in order, **once**, against the AWS account
that will host the pipeline. Each script is idempotent — re-running is safe.

Prerequisites:
- AWS CLI v2 installed and `aws configure` completed against the target account
- The IAM principal you're authenticated as has permissions to manage CloudWatch
  alarms, SNS topics, and IAM roles/users (a personal admin user is fine for
  bootstrap; the deploy user created here is least-privilege)

## Order of operations

### Phase 0 — Account bootstrap (one-time)

```bash
# 1. Cost guardrail — do this FIRST, per README §6.1
export BILLING_ALERT_EMAIL=you@example.com
export BILLING_ALERT_THRESHOLD_USD=5
./infra/scripts/00-billing-alarm.sh

# 2. IAM execution roles for the three Lambdas + GitHub Actions deploy user
./infra/scripts/01-create-iam-roles.sh

# 3. Generate the deploy user's access key (one-time)
aws iam create-access-key --user-name financial-pipeline-ci
# -> Copy AccessKeyId + SecretAccessKey into GitHub repo secrets immediately.
#    They are shown only once.
```

### Phase 1 — Fetcher Lambda + EventBridge

```bash
# Activate the venv so python3.12 + pip resolve correctly
source .venv/bin/activate

# 4. Build and publish the yfinance + requests layer
./infra/scripts/02-build-fetcher-layer.sh
# -> Copy the printed LayerVersionArn into the next step.
export FETCHER_LAYER_ARN=arn:aws:lambda:us-east-1:...:layer:financial-pipeline-yfinance:1

# 5. Create / update the fetcher Lambda function
./infra/scripts/03-deploy-fetcher.sh

# 6. Smoke-test it by invoking once on demand
aws lambda invoke --function-name financial-pipeline-fetcher /tmp/out.json
cat /tmp/out.json | python3 -m json.tool

# 7. Wire the rate(1 minute) EventBridge cron rule
./infra/scripts/04-create-eventbridge-rule.sh

# 8. Watch the live tail until you see ~2 scheduled invocations, then disable
#    the rule until Phase 2 wires Kinesis (avoid burning free-tier invocations
#    on data that goes nowhere).
aws logs tail /aws/lambda/financial-pipeline-fetcher --follow
aws events disable-rule --name financial-pipeline-fetcher-cron
```

## What gets created

| Phase | Resource | Name | Region |
|---|---|---|---|
| 0 | SNS topic | `financial-pipeline-billing-alerts` | us-east-1 |
| 0 | CloudWatch alarm | `financial-pipeline-billing-over-5usd` | us-east-1 |
| 0 | IAM role | `financial-pipeline-fetcher-role` | global |
| 0 | IAM role | `financial-pipeline-processor-role` | global |
| 0 | IAM role | `financial-pipeline-query-role` | global |
| 0 | IAM user | `financial-pipeline-ci` | global |
| 1 | Lambda layer | `financial-pipeline-yfinance` | us-east-1 |
| 1 | Lambda function | `financial-pipeline-fetcher` | us-east-1 |
| 1 | CloudWatch log group | `/aws/lambda/financial-pipeline-fetcher` | us-east-1 |
| 1 | EventBridge rule | `financial-pipeline-fetcher-cron` | us-east-1 |

Policies live in `infra/iam/*.json` and are version-controlled.
