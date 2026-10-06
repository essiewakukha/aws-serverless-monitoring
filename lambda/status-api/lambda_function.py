"""
Read-only status API behind API Gateway (GET /status).

Returns every enabled target's current state plus its recent check history.
The target URL is deliberately NOT returned: the endpoint is public, and a
status page only needs the display name.
"""
import json
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

TARGETS_TABLE = os.environ["TARGETS_TABLE"]
RESULTS_TABLE = os.environ["RESULTS_TABLE"]
HISTORY_POINTS = int(os.environ.get("HISTORY_POINTS", "60"))

dynamodb = boto3.resource("dynamodb")
targets_table = dynamodb.Table(TARGETS_TABLE)
results_table = dynamodb.Table(RESULTS_TABLE)


def _scan_targets():
    items = []
    kwargs = {}
    while True:
        resp = targets_table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _as_int(value, default=0):
    return int(value) if isinstance(value, (Decimal, int)) else default


def handler(event, context):
    targets = [t for t in _scan_targets() if t.get("enabled", True)]
    targets.sort(key=lambda t: t.get("name", t["target_id"]))

    output = []
    for target in targets:
        resp = results_table.query(
            KeyConditionExpression=Key("target_id").eq(target["target_id"]),
            ScanIndexForward=False,
            Limit=HISTORY_POINTS,
        )
        history = [
            {"t": _as_int(i["checked_at"]), "up": bool(i["up"]), "ms": _as_int(i.get("latency_ms"))}
            for i in reversed(resp.get("Items", []))
        ]
        uptime = None
        if history:
            uptime = round(100 * sum(1 for h in history if h["up"]) / len(history), 2)

        output.append({
            "name": target.get("name", target["target_id"]),
            "status": target.get("last_status", "UNKNOWN"),
            "latency_ms": _as_int(target.get("last_latency_ms")),
            "last_checked": _as_int(target.get("last_checked")),
            "uptime_pct": uptime,
            "history": history,
        })

    body = {
        "all_up": bool(output) and all(t["status"] == "UP" for t in output),
        "targets": output,
    }
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json", "cache-control": "max-age=15"},
        "body": json.dumps(body),
    }