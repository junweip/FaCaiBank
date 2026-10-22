"""Per-endpoint latency metrics for the balance and transfer targets.

The ALB only reports latency for the whole load balancer, so the API records
each request's duration per endpoint and publishes raw values to CloudWatch
once a minute. CloudWatch can then compute p95 for ``Endpoint=balance`` and
``Endpoint=transfer``, which is what the slide-12 alarms watch.
"""

import logging
import threading
import time
from collections import Counter, defaultdict

import boto3

from . import config


logger = logging.getLogger(__name__)
MAX_VALUES_PER_DATUM = 150
MAX_DATA_PER_CALL = 1000
# Each endpoint dimension is a billable custom metric, so only the endpoints the
# alarms and dashboard use are tracked individually; the rest roll up to "other".
TRACKED_ENDPOINTS = {"balance", "transfer", "transactions", "deposit", "withdrawal", "profile"}


class LatencyRecorder:
    def __init__(self, namespace: str, environment: str, region: str, enabled: bool, interval: float = 60.0):
        self.namespace = namespace
        self.environment = environment
        self.region = region
        self.enabled = enabled
        self.interval = interval
        self._lock = threading.Lock()
        self._latencies: dict[str, Counter] = defaultdict(Counter)
        self._requests: Counter = Counter()
        self._errors: Counter = Counter()
        self._thread: threading.Thread | None = None
        self._client = None

    def record(self, endpoint: str, millis: float, status_code: int) -> None:
        if not self.enabled or endpoint == "health":
            return
        endpoint = endpoint if endpoint in TRACKED_ENDPOINTS else "other"
        with self._lock:
            self._latencies[endpoint][max(1, round(millis))] += 1
            self._requests[endpoint] += 1
            if status_code >= 500:
                self._errors[endpoint] += 1

    def drain(self) -> list[dict]:
        with self._lock:
            latencies, self._latencies = self._latencies, defaultdict(Counter)
            requests, self._requests = self._requests, Counter()
            errors, self._errors = self._errors, Counter()
        data = []
        for endpoint, histogram in latencies.items():
            dimensions = [
                {"Name": "Environment", "Value": self.environment},
                {"Name": "Endpoint", "Value": endpoint},
            ]
            values = sorted(histogram.items())
            for start in range(0, len(values), MAX_VALUES_PER_DATUM):
                chunk = values[start : start + MAX_VALUES_PER_DATUM]
                data.append(
                    {
                        "MetricName": "Latency",
                        "Dimensions": dimensions,
                        "Values": [float(value) for value, _ in chunk],
                        "Counts": [float(count) for _, count in chunk],
                        "Unit": "Milliseconds",
                    }
                )
            data.append(
                {"MetricName": "Requests", "Dimensions": dimensions, "Value": requests[endpoint], "Unit": "Count"}
            )
            data.append(
                {"MetricName": "Errors5xx", "Dimensions": dimensions, "Value": errors[endpoint], "Unit": "Count"}
            )
        return data

    def flush(self) -> None:
        data = self.drain()
        if not data:
            return
        if self._client is None:
            self._client = boto3.client("cloudwatch", region_name=self.region)
        for start in range(0, len(data), MAX_DATA_PER_CALL):
            try:
                self._client.put_metric_data(Namespace=self.namespace, MetricData=data[start : start + MAX_DATA_PER_CALL])
            except Exception:
                logger.warning("Could not publish API latency metrics", exc_info=True)

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return

        def loop():
            while True:
                time.sleep(self.interval)
                self.flush()

        self._thread = threading.Thread(target=loop, name="latency-metrics", daemon=True)
        self._thread.start()


recorder = LatencyRecorder(
    namespace=config.settings.metrics_namespace,
    environment=config.settings.environment,
    region=config.settings.aws_region,
    enabled=config.settings.metrics_enabled,
)
