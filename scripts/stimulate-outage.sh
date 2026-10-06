#!/usr/bin/env bash
# Breaks a monitored target on purpose and measures how long the monitor takes to alert.
#   ./scripts/simulate-outage.sh start   adds a target that always fails, times detection
#   ./scripts/simulate-outage.sh stop    fixes it, times recovery, then removes it
# Detection time is measured to the moment the checker raises the DOWN state
# (the same moment it publishes the SNS alert). Note when the email lands too.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="${AWS_REGION:-us-east-1}"
TARGET="simulated-outage"
KEY="{\"target_id\":{\"S\":\"$TARGET\"}}"
THRESHOLD=2
TIMEOUT_SECONDS=300

field() { # field <attribute-path>
  aws dynamodb get-item --table-name monitor-targets --key "$KEY" \
    --query "Item.$1" --output text --region "$REGION" 2>/dev/null || echo "None"
}

case "${1:-}" in
  start)
    # Connection refused inside the Lambda: fails fast and deterministically
    "$DIR/add-target.sh" "$TARGET" "http://127.0.0.1:9/" > /dev/null
    START=$(date +%s)
    echo ">> Outage started at $(date +%H:%M:%S). Waiting for $THRESHOLD consecutive failed checks..."
    while true; do
      FAILURES="$(field consecutive_failures.N)"
      ELAPSED=$(( $(date +%s) - START ))
      echo "   ${ELAPSED}s elapsed, consecutive failures: ${FAILURES}"
      if [[ "$FAILURES" =~ ^[0-9]+$ ]] && [ "$FAILURES" -ge "$THRESHOLD" ]; then
        echo ">> DOWN alert raised after ${ELAPSED}s. Check your email, then run: $0 stop"
        break
      fi
      if [ "$ELAPSED" -ge "$TIMEOUT_SECONDS" ]; then
        echo ">> Gave up after ${TIMEOUT_SECONDS}s - check the checker logs"
        exit 1
      fi
      sleep 10
    done
    ;;
  stop)
    "$DIR/add-target.sh" "$TARGET" "https://aws.amazon.com" > /dev/null
    START=$(date +%s)
    echo ">> Target repaired at $(date +%H:%M:%S). Waiting for the next healthy check..."
    while true; do
      STATUS="$(field last_status.S)"
      ELAPSED=$(( $(date +%s) - START ))
      echo "   ${ELAPSED}s elapsed, status: ${STATUS}"
      if [ "$STATUS" = "UP" ]; then
        echo ">> Recovered after ${ELAPSED}s"
        break
      fi
      if [ "$ELAPSED" -ge "$TIMEOUT_SECONDS" ]; then
        echo ">> Gave up after ${TIMEOUT_SECONDS}s"
        exit 1
      fi
      sleep 10
    done
    aws dynamodb delete-item --table-name monitor-targets --key "$KEY" --region "$REGION"
    echo ">> Removed the simulated target"
    ;;
  *)
    echo "Usage: $0 start|stop"
    exit 1
    ;;
esac