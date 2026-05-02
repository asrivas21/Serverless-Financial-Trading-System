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

# 4. Build and publish the requests layer (originally yfinance; renamed lazily)
./infra/scripts/02-build-fetcher-layer.sh
# -> Copy the printed LayerVersionArn into the next step.
export FETCHER_LAYER_ARN=arn:aws:lambda:us-east-1:...:layer:financial-pipeline-yfinance:1

# 5. Create / update the fetcher Lambda function. Requires FINNHUB_API_KEY
#    (sign up free at https://finnhub.io). KINESIS_STREAM_NAME is also read
#    once Phase 2 step 6 has run.
export FINNHUB_API_KEY=xxxxxxxxxxxxxxxxxxxx
./infra/scripts/03-deploy-fetcher.sh

# 6. Smoke-test it by invoking once on demand
aws lambda invoke --function-name financial-pipeline-fetcher /tmp/out.json
cat /tmp/out.json | python3 -m json.tool

# 7. (Deferred until Phase 2 wires Kinesis) Wire the rate(1 minute)
#    EventBridge cron rule. Running this with no Kinesis stream just burns
#    free-tier invocations on data that goes nowhere — defer.
./infra/scripts/04-create-eventbridge-rule.sh
```

### Phase 2 — Kinesis stream + Processor Lambda

```bash
# 8. Provision the Kinesis stream the fetcher publishes to.
#    COST: ~$0.36/day per shard while the stream is up — tear down between
#    sessions via 99-teardown.sh.
./infra/scripts/05-create-kinesis-stream.sh
export KINESIS_STREAM_NAME=financial-price-events

# 9. Redeploy the fetcher (now publishes to Kinesis on each invocation).
./infra/scripts/03-deploy-fetcher.sh

# 10. Verify records actually land in the stream:
aws lambda invoke --function-name financial-pipeline-fetcher /tmp/out.json
SHARD_ITER=$(aws kinesis get-shard-iterator \
  --stream-name financial-price-events \
  --shard-id shardId-000000000000 \
  --shard-iterator-type TRIM_HORIZON \
  --query 'ShardIterator' --output text)
aws kinesis get-records --shard-iterator "${SHARD_ITER}" --limit 10

# 11. Deploy the processor Lambda (no layer needed — stdlib + boto3 only)
./infra/scripts/06-deploy-processor.sh

# 12. Create the SQS DLQ and wire Kinesis -> processor event source mapping
./infra/scripts/07-create-event-source-mapping.sh

# 13. End-to-end smoke: invoke fetcher, then watch the processor logs
aws lambda invoke --function-name financial-pipeline-fetcher /tmp/out.json
sleep 3
aws logs tail /aws/lambda/financial-pipeline-processor --since 1m

# At end of session, delete the Kinesis stream + event source mapping:
./infra/scripts/99-teardown.sh
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
| 2 | Kinesis stream | `financial-price-events` | us-east-1 |
| 2 | Lambda function | `financial-pipeline-processor` | us-east-1 |
| 2 | SQS queue (DLQ) | `financial-pipeline-processor-dlq` | us-east-1 |
| 2 | Event source mapping | (Kinesis → processor) | us-east-1 |

Policies live in `infra/iam/*.json` and are version-controlled.
