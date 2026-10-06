#!/usr/bin/env bash
# Deploys every stack in dependency order, capturing outputs and feeding them
# into the next stack, so nothing needs to be copied by hand between steps.
# Run from anywhere:  ./scripts/deploy.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

[ -f .env ] || { echo "Missing .env - run: cp .env.example .env and fill it in"; exit 1; }
set -a
source .env
set +a

ALERT_EMAIL="${ALERT_EMAIL:?Set ALERT_EMAIL in .env}"
export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
REGION="$AWS_DEFAULT_REGION"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ARTIFACT_BUCKET="serverless-monitor-artifacts-${ACCOUNT_ID}-${REGION}"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

out() { # out <stack-name> <output-key>
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

echo ">> 0/7 Artifact bucket for Lambda packages ($ARTIFACT_BUCKET)"
aws s3 mb "s3://$ARTIFACT_BUCKET" --region "$REGION" 2>/dev/null || true

echo ">> 1/7 Storage (DynamoDB tables)"
aws cloudformation deploy \
  --template-file cloudformation/storage.yaml \
  --stack-name monitor-storage

TARGETS_TABLE="$(out monitor-storage TargetsTableName)"
TARGETS_ARN="$(out monitor-storage TargetsTableArn)"
RESULTS_TABLE="$(out monitor-storage ResultsTableName)"
RESULTS_ARN="$(out monitor-storage ResultsTableArn)"

echo ">> 2/7 Alerting (SNS topic + alarms) - confirm the subscription email AWS sends you"
aws cloudformation deploy \
  --template-file cloudformation/alerting.yaml \
  --stack-name monitor-alerting \
  --parameter-overrides AlertEmail="$ALERT_EMAIL"

TOPIC_ARN="$(out monitor-alerting AlertTopicArn)"

echo ">> 3/7 Checker Lambda + EventBridge schedule"
aws cloudformation package \
  --template-file cloudformation/checker.yaml \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --output-template-file "$BUILD_DIR/checker.yaml" > /dev/null
aws cloudformation deploy \
  --template-file "$BUILD_DIR/checker.yaml" \
  --stack-name monitor-checker \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides TargetsTableName="$TARGETS_TABLE" TargetsTableArn="$TARGETS_ARN" \
    ResultsTableName="$RESULTS_TABLE" ResultsTableArn="$RESULTS_ARN" AlertTopicArn="$TOPIC_ARN"

CHECKER_NAME="$(out monitor-checker CheckerFunctionName)"

echo ">> 4/7 Observability (dashboard, error alarm, Logs Insights queries)"
aws cloudformation deploy \
  --template-file cloudformation/observability.yaml \
  --stack-name monitor-observability \
  --parameter-overrides CheckerFunctionName="$CHECKER_NAME" AlertTopicArn="$TOPIC_ARN"

echo ">> 5/7 Status API"
aws cloudformation package \
  --template-file cloudformation/api.yaml \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --output-template-file "$BUILD_DIR/api.yaml" > /dev/null
aws cloudformation deploy \
  --template-file "$BUILD_DIR/api.yaml" \
  --stack-name monitor-api \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides TargetsTableName="$TARGETS_TABLE" TargetsTableArn="$TARGETS_ARN" \
    ResultsTableName="$RESULTS_TABLE" ResultsTableArn="$RESULTS_ARN"

API_URL="$(out monitor-api ApiUrl)"

echo ">> 6/7 Status page (S3 + CloudFront)"
aws cloudformation deploy \
  --template-file cloudformation/dashboard.yaml \
  --stack-name monitor-dashboard

SITE_BUCKET="$(out monitor-dashboard BucketName)"
SITE_URL="$(out monitor-dashboard DashboardUrl)"

mkdir -p "$BUILD_DIR/site"
cp dashboard/index.html "$BUILD_DIR/site/index.html"
printf 'window.MONITOR_API_URL = "%s";\n' "$API_URL" > "$BUILD_DIR/site/config.js"
aws s3 sync "$BUILD_DIR/site" "s3://$SITE_BUCKET" --delete

echo ">> 7/7 Seeding default targets"
"$DIR/scripts/add-target.sh" github https://github.com
"$DIR/scripts/add-target.sh" aws-homepage https://aws.amazon.com
if [ -n "${DR_APP_URL:-}" ]; then
  "$DIR/scripts/add-target.sh" dr-project-app "$DR_APP_URL"
fi

echo ""
echo "Done."
echo "  Status page:     $SITE_URL"
echo "  Status API:      $API_URL"
echo "  Alert topic:     $TOPIC_ARN"
echo "  Next: confirm the SNS subscription email, then run ./scripts/simulate-outage.sh start"