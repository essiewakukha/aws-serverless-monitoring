"""
Unit tests for the checker Lambda. AWS clients are mocked, so these run
offline: python3 -m unittest discover -s tests -v
"""
import importlib
import os
import sys
import unittest
import urllib.error
from decimal import Decimal
from unittest import mock

os.environ.update({
    "TARGETS_TABLE": "monitor-targets",
    "RESULTS_TABLE": "monitor-check-results",
    "ALERT_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:serverless-monitor-alerts",
    "AWS_DEFAULT_REGION": "us-east-1",
    "FAILURE_THRESHOLD": "2",
})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda", "checker"))

with mock.patch("boto3.resource"), mock.patch("boto3.client"):
    checker = importlib.import_module("lambda_function")


def fake_response(status):
    response = mock.MagicMock()
    response.status = status
    context_manager = mock.MagicMock()
    context_manager.__enter__.return_value = response
    return context_manager


class CheckTargetTests(unittest.TestCase):
    def test_200_is_up(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response(200)):
            result = checker.check_target({"target_id": "a", "url": "https://example.com"})
        self.assertTrue(result["up"])
        self.assertEqual(result["status_code"], 200)

    def test_http_error_5xx_is_down(self):
        err = urllib.error.HTTPError("https://example.com", 503, "unavailable", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            result = checker.check_target({"target_id": "a", "url": "https://example.com"})
        self.assertFalse(result["up"])
        self.assertEqual(result["status_code"], 503)

    def test_connection_error_is_down_with_message(self):
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")):
            result = checker.check_target({"target_id": "a", "url": "http://127.0.0.1:9/"})
        self.assertFalse(result["up"])
        self.assertIn("ConnectionRefusedError", result["error"])

    def test_expected_status_mismatch_is_down(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response(200)):
            result = checker.check_target(
                {"target_id": "a", "url": "https://example.com", "expected_status": Decimal(204)})
        self.assertFalse(result["up"])


class AlertingTests(unittest.TestCase):
    def setUp(self):
        checker.sns = mock.MagicMock()
        checker.targets_table = mock.MagicMock()
        checker.results_table = mock.MagicMock()

    def _record(self, prior_failures, up):
        target = {"target_id": "web", "name": "Web", "consecutive_failures": Decimal(prior_failures)}
        result = {"up": up, "status_code": 200 if up else 503, "latency_ms": 120, "error": None}
        checker.record_result(target, result, now=1000)

    def test_first_failure_does_not_alert(self):
        self._record(prior_failures=0, up=False)
        checker.sns.publish.assert_not_called()

    def test_alert_fires_exactly_at_threshold(self):
        self._record(prior_failures=1, up=False)
        checker.sns.publish.assert_called_once()
        self.assertIn("[DOWN]", checker.sns.publish.call_args.kwargs["Subject"])

    def test_no_repeat_alert_after_threshold(self):
        self._record(prior_failures=2, up=False)
        checker.sns.publish.assert_not_called()

    def test_recovery_alert_after_outage(self):
        self._record(prior_failures=2, up=True)
        checker.sns.publish.assert_called_once()
        self.assertIn("[RECOVERED]", checker.sns.publish.call_args.kwargs["Subject"])
        values = checker.targets_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        self.assertEqual(values[":f"], 0)

    def test_blip_that_recovers_before_threshold_is_silent(self):
        self._record(prior_failures=1, up=True)
        checker.sns.publish.assert_not_called()


class MetricsTests(unittest.TestCase):
    def setUp(self):
        checker.cloudwatch = mock.MagicMock()

    def test_aggregate_metrics(self):
        targets = [{"target_id": "a"}, {"target_id": "b"}, {"target_id": "c"}]
        results = [
            {"up": True, "latency_ms": 100, "status_code": 200, "error": None},
            {"up": True, "latency_ms": 450, "status_code": 200, "error": None},
            {"up": False, "latency_ms": 5000, "status_code": None, "error": "timeout"},
        ]
        checker.publish_metrics(targets, results)
        data = checker.cloudwatch.put_metric_data.call_args.kwargs["MetricData"]
        by_name = {m["MetricName"]: m["Value"] for m in data if "Dimensions" not in m}
        self.assertEqual(by_name["TargetsDown"], 1)
        self.assertEqual(by_name["SlowestLatencyMs"], 450)  # failed check excluded
        self.assertEqual(by_name["Heartbeat"], 1)


if __name__ == "__main__":
    unittest.main()