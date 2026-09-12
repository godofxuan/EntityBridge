"""Bounded, process-local HTTP counters without request text or identities."""
import math
import threading
import time
from collections import Counter, defaultdict, deque


class RequestMetrics:
    def __init__(self, sample_limit=1000, route_limit=128):
        self.started = time.monotonic()
        self.sample_limit = sample_limit
        self.route_limit = route_limit
        self._lock = threading.Lock()
        self._errors = 0
        self._counts = Counter()
        self._samples = defaultdict(lambda: deque(maxlen=sample_limit))

    def record(self, method, route, status, seconds):
        with self._lock:
            self._errors += int(status >= 500)
            key = (method, route, status)
            if key not in self._counts and len(self._counts) >= self.route_limit:
                key = ("OTHER", "overflow", 0)
            self._counts[key] += 1
            self._samples[key].append(max(0.0, seconds * 1000))

    def snapshot(self):
        with self._lock:
            rows = []
            for (method, route, status), count in sorted(self._counts.items()):
                samples = sorted(self._samples[(method, route, status)])
                percentile = lambda q, values=samples: values[max(0, math.ceil(q * len(values)) - 1)]
                rows.append({"method": method, "route": route, "status": status, "requests": count,
                             "latency_sample_count": len(samples), "p50_ms": percentile(.5),
                             "p95_ms": percentile(.95)})
            return {"scope": "one API process since startup; bounded recent latency samples",
                    "uptime_seconds": time.monotonic() - self.started, "requests": sum(self._counts.values()),
                    "errors_5xx": self._errors,
                    "sample_limit_per_series": self.sample_limit, "series": rows}
