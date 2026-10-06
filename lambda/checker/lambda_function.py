"""
Serverless uptime checker.

Triggered every minute by EventBridge. For each enabled target in the
monitor-targets table it makes an HTTP request, then:

  * writes the result to the monitor-check-results table (auto-expires via TTL)
  * updates the target's current state (consecutive failures, last status)
  * publishes custom CloudWatch metrics (per-target Up/LatencyMs, plus the
    aggregate TargetsDown, SlowestLatencyMs and Heartbeat used by the alarms)
  * sends an SNS message when a target goes DOWN (after FAILURE_THRESHOLD
    consecutive failures) and when it RECOVERS

Standard library plus boto3 only, so there is nothing to package.
All log lines are single-line JSON so CloudWatch Logs Insights can query them.
"""
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import boto3

TARGETS_TABLE = os.environ["TARGETS_TABLE"]
RESULTS_TABLE = os.environ["RESULTS_TABLE"]
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
NAMESPACE = os.environ.get("METRIC_NAMESPACE", "ServerlessMonitor")
TIMEOUT_SECONDS = float(os.environ.get("CHECK_TIMEOUT_SECONDS", "5"))
FAILURE_THRESHOLD = int(os.environ.get("FAILURE_THRESHOLD", "2"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))

dynamodb = boto3.resource("dynamodb")
targets_table = dynamodb.Table(TARGETS_TABLE)
results_table = dynamodb.Table(RESULTS_TABLE)
cloudwatch = boto3.client("cloudwatch")
sns = boto3.client("sns")


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, default=str))


def scan_targets():
    items = []
    kwargs = {}
    while True:
        resp = targets_table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def check_target(target):
    """Make one HTTP request and classify it as up or down."""
    url = target["url"]
    expected = int(target["expected_status"]) if "expected_status" in target else None
    status_code = None
    error = None
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "serverless-monitor/1.0"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status_code = response.status
    except urllib.error.HTTPError as exc:
        status_code = exc.code
    except Exception as exc:  # DNS failure, refused, timeout, TLS error, ...
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.monotonic() - started) * 1000)

    if error is not None:
        up = False
    elif expected is not None:
        up = status_code == expected
    else:
        up = 200 <= status_code < 400

    return {"up": up, "status_code": status_code, "latency_ms": latency_ms, "error": error}


def send_alert(name, result, down):
    if not ALERT_TOPIC_ARN:
        return
    if down:
        subject = f"[DOWN] {name}"
        detail = result["error"] or f"HTTP {result['status_code']}"
        message = (
            f"{name} has failed {FAILURE_THRESHOLD} checks in a row.\n"
            f"Last result: {detail} after {result['latency_ms']} ms."
        )
    else:
        subject = f"[RECOVERED] {name}"
        message = f"{name} is responding again (HTTP {result['status_code']}, {result['latency_ms']} ms)."
    try:
        sns.publish(TopicArn=ALERT_TOPIC_ARN, Subject=subject[:100], Message=message)
        log("alert_sent", target=name, down=down)
    except Exception as exc:
        log("alert_failed", target=name, error=str(exc))


def record_result(target, result, now):
    target_id = target["target_id"]
    name = target.get("name", target_id)
    prior_failures = int(target.get("consecutive_failures", 0))

    if result["up"]:
        failures = 0
        if prior_failures >= FAILURE_THRESHOLD:
            send_alert(name, result, down=False)
    else:
        failures = prior_failures + 1
        if failures == FAILURE_THRESHOLD:
            send_alert(name, result, down=True)

    item = {
        "target_id": target_id,
        "checked_at": now,
        "up": result["up"],
        "latency_ms": result["latency_ms"],
        "expires_at": now + RETENTION_DAYS * 86400,
    }
    if result["status_code"] is not None:
        item["status_code"] = result["status_code"]
    if result["error"]:
        item["error"] = result["error"][:300]
    results_table.put_item(Item=item)

    targets_table.update_item(
        Key={"target_id": target_id},
        UpdateExpression=(
            "SET consecutive_failures = :f, last_status = :s, "
            "last_latency_ms = :l, last_checked = :t"
        ),
        ExpressionAttributeValues={
            ":f": failures,
            ":s": "UP" if result["up"] else "DOWN",
            ":l": result["latency_ms"],
            ":t": now,
        },
    )

    fields = {
        "target_id": target_id,
        "status_code": result["status_code"],
        "latency_ms": result["latency_ms"],
        "consecutive_failures": failures,
    }
    if result["error"]:
        fields["error"] = result["error"][:300]
    log("check_ok" if result["up"] else "check_failed", **fields)


def publish_metrics(targets, results):
    data = [{"MetricName": "Heartbeat", "Value": 1, "Unit": "Count"}]
    down_count = 0
    healthy_latencies = []

    for target, result in zip(targets, results):
        dims = [{"Name": "TargetId", "Value": target["target_id"]}]
        data.append({"MetricName": "Up", "Dimensions": dims,
                     "Value": 1 if result["up"] else 0, "Unit": "Count"})
        if result["up"]:
            healthy_latencies.append(result["latency_ms"])
            data.append({"MetricName": "LatencyMs", "Dimensions": dims,
                         "Value": result["latency_ms"], "Unit": "Milliseconds"})
        else:
            down_count += 1

    data.append({"MetricName": "TargetsDown", "Value": down_count, "Unit": "Count"})
    if healthy_latencies:
        data.append({"MetricName": "SlowestLatencyMs", "Value": max(healthy_latencies),
                     "Unit": "Milliseconds"})

    for i in range(0, len(data), 100):
        cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=data[i:i + 100])


def handler(event, context):
    targets = [t for t in scan_targets() if t.get("enabled", True)]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(check_target, targets))

    now = int(time.time())
    for target, result in zip(targets, results):
        try:
            record_result(target, result, now)
        except Exception as exc:  # one bad target must not stop the others
            log("record_failed", target_id=target.get("target_id"), error=str(exc))

    publish_metrics(targets, results)

    summary = {"checked": len(results), "down": sum(1 for r in results if not r["up"])}
    log("run_complete", **summary)
    return summary