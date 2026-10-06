#!/usr/bin/env bash
# Adds (or overwrites) a monitored target.
# Usage: ./scripts/add-target.sh <name> <url> [expected-status-code]
#   <name>  lowercase letters, digits and hyphens only (it becomes the target id)
#   e.g.    ./scripts/add-target.sh my-api https://api.example.com/health 200
# Re-adding an existing name overwrites it and resets its failure counter.
set -euo pipefail

NAME="${1:?Usage: $0 <name> <url> [expected-status-code]}"
URL="${2:?Usage: $0 <name> <url> [expected-status-code]}"
EXPECTED="${3:-}"
REGION="${AWS_REGION:-us-east-1}"

if ! [[ "$NAME" =~ ^[a-z0-9-]+$ ]]; then
  echo "Name must be lowercase letters, digits and hyphens only"
  exit 1
fi
if ! [[ "$URL" =~ ^https?://[^\"[:space:]]+$ ]]; then
  echo "URL must start with http:// or https:// and contain no quotes or spaces"
  exit 1
fi

ITEM="$(printf '{"target_id":{"S":"%s"},"name":{"S":"%s"},"url":{"S":"%s"},"enabled":{"BOOL":true},"consecutive_failures":{"N":"0"}' \
  "$NAME" "$NAME" "$URL")"
if [ -n "$EXPECTED" ]; then
  if ! [[ "$EXPECTED" =~ ^[0-9]{3}$ ]]; then
    echo "Expected status must be a 3-digit HTTP code"
    exit 1
  fi
  ITEM="$ITEM,\"expected_status\":{\"N\":\"$EXPECTED\"}"
fi
ITEM="$ITEM}"

aws dynamodb put-item --table-name monitor-targets --item "$ITEM" --region "$REGION"
echo "Added target '$NAME' -> $URL"