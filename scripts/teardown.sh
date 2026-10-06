#!/usr/bin/env bash
# Deletes every stack this project created, in reverse dependency order.
# Run when you are done, to stop all charges.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

[ -f .env ] && { set -a; source .env; set +a; }
export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
REGION="$AWS_DEFAULT_REGION"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ARTIFACT_BUCKET="serverless-monitor-artifacts-${ACCOUNT_ID}-${REGION}"
SITE_BUCKET="serverless-monitor-dashboard-${ACCOUNT_ID}-${REGION}"

# CloudFormation cannot delete a bucket that still has objects in it
echo ">> Emptying the status page bucket"
aws s3 rm "s3://$SITE_BUCKET" --recursive 2>/dev/null || true

for STACK in monitor-dashboard monitor-api monitor-observability monitor-checker monitor-alerting monitor-storage; do
  echo ">> Deleting $STACK"
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
done

echo ">> Removing the Lambda artifact bucket"
aws s3 rm "s3://$ARTIFACT_BUCKET" --recursive 2>/dev/null || true
aws s3 rb "s3://$ARTIFACT_BUCKET" 2>/dev/null || true

echo ">> Done. CloudFront distributions can take a few minutes to finish deleting."