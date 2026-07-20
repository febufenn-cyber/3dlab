"""Observability: structured logs, a tiny Prometheus registry, request tracing.

Zero new dependencies (stdlib only) to respect the 1 GB micro budget — a
hand-rolled metric registry rather than prometheus_client. A single-process
async app, so a module-level singleton registry (the same pattern
prometheus_client uses) is correct and lock-free enough; counters use simple
`+=` which is atomic under CPython's GIL for our purposes.

Exposes:
* ``metrics`` — the singleton registry (Counter/Gauge/Histogram) + ``render()``
  in Prometheus text exposition format.
* ``MetricsMiddleware`` — pure-ASGI: assigns a request id, times each request,
  and records http_requests_total / http_request_duration_seconds / in-flight.
* ``configure_logging`` + ``JsonLogFormatter`` — JSON logs carrying the current
  request id (via a contextvar) so every line is correlatable.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from typing import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# --- request-id context -----------------------------------------------------

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


# --- metric primitives ------------------------------------------------------


def _fmt_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(
        f'{k}="{str(v).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"'
        for k, v in sorted(labels.items())
    )
    return "{" + inner + "}"


class _Counter:
    def __init__(self, name: str, help: str):
        self.name, self.help = name, help
        self._vals: dict[tuple, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        self._vals[key] = self._vals.get(key, 0.0) + amount

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for key, v in self._vals.items():
            out.append(f"{self.name}{_fmt_labels(dict(key))} {v}")
        if not self._vals:
            out.append(f"{self.name} 0")
        return out


class _Gauge:
    def __init__(self, name: str, help: str):
        self.name, self.help = name, help
        self._vals: dict[tuple, float] = {}

    def set(self, value: float, **labels: str) -> None:
        self._vals[tuple(sorted(labels.items()))] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        self._vals[key] = self._vals.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        for key, v in self._vals.items():
            out.append(f"{self.name}{_fmt_labels(dict(key))} {v}")
        if not self._vals:
            out.append(f"{self.name} 0")
        return out


_DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


class _Histogram:
    def __init__(self, name: str, help: str, buckets: Iterable[float] = _DEFAULT_BUCKETS):
        self.name, self.help = name, help
        self.buckets = tuple(buckets)
        self._counts: dict[tuple, list[int]] = {}
        self._sum: dict[tuple, float] = {}
        self._n: dict[tuple, int] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        counts = self._counts.setdefault(key, [0] * len(self.buckets))
        for i, b in enumerate(self.buckets):
            if value <= b:
                counts[i] += 1
        self._sum[key] = self._sum.get(key, 0.0) + value
        self._n[key] = self._n.get(key, 0) + 1

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key, counts in self._counts.items():
            lbls = dict(key)
            # counts[i] already holds "# observations <= buckets[i]" (observe()
            # increments every bucket the value fits), so it is cumulative.
            for i, b in enumerate(self.buckets):
                out.append(f"{self.name}_bucket{_fmt_labels({**lbls, 'le': _num(b)})} {counts[i]}")
            out.append(f"{self.name}_bucket{_fmt_labels({**lbls, 'le': '+Inf'})} {self._n[key]}")
            out.append(f"{self.name}_sum{_fmt_labels(lbls)} {self._sum[key]}")
            out.append(f"{self.name}_count{_fmt_labels(lbls)} {self._n[key]}")
        return out


def _num(b: float) -> str:
    return str(int(b)) if b == int(b) else str(b)


class Metrics:
    """The singleton registry."""

    def __init__(self):
        self.http_requests = _Counter(
            "sceneforge_http_requests_total", "HTTP requests by route/method/status"
        )
        self.http_latency = _Histogram(
            "sceneforge_http_request_duration_seconds", "HTTP request latency",
        )
        self.http_in_flight = _Gauge("sceneforge_http_in_flight", "In-flight HTTP requests")
        self.scenes_by_state = _Gauge(
            "sceneforge_scenes", "Scene count by state (refreshed at scrape)"
        )
        self.reaper_actions = _Counter(
            "sceneforge_reaper_actions_total", "Reaper actions by kind"
        )
        self.webhook_deliveries = _Counter(
            "sceneforge_webhook_deliveries_total", "Webhook delivery attempts by result"
        )
        self.rejections = _Counter(
            "sceneforge_rejections_total", "Requests rejected by an edge control, by kind"
        )
        self.worker_results = _Counter(
            "sceneforge_worker_results_total", "Worker result callbacks by state"
        )

    def render(self) -> str:
        lines: list[str] = []
        for m in (
            self.http_requests, self.http_latency, self.http_in_flight,
            self.scenes_by_state, self.reaper_actions, self.webhook_deliveries,
            self.rejections, self.worker_results,
        ):
            lines.extend(m.render())
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Clear all series — for test isolation (the registry is a singleton)."""
        self.__init__()


metrics = Metrics()


# --- request middleware -----------------------------------------------------


def _route_template(scope: Scope) -> str:
    """The matched route path template (low cardinality) or the raw path.

    Prefer the FastAPI route template so /v1/scenes/{scene_id} doesn't explode
    metric cardinality with one series per scene id."""
    route = scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    return scope.get("path", "unknown")


class MetricsMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Request id: honor an inbound X-Request-ID (for cross-service tracing)
        # else mint one. Bounded length to avoid log/head abuse.
        rid = "-"
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                rid = value.decode("latin-1")[:64]
                break
        if rid == "-":
            rid = uuid.uuid4().hex
        token = request_id_ctx.set(rid)

        method = scope.get("method", "GET")
        start = time.perf_counter()
        status_holder = {"code": 500}
        metrics.http_in_flight.inc()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", rid.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = _route_template(scope)
            status = str(status_holder["code"])
            metrics.http_requests.inc(route=route, method=method, status=status)
            metrics.http_latency.observe(elapsed, route=route)
            metrics.http_in_flight.dec()
            request_id_ctx.reset(token)


# --- structured logging -----------------------------------------------------


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in getattr(record, "extra_fields", {}).items():
            payload[k] = v
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(fmt: str = "json", level: str = "INFO") -> None:
    """Install the root handler. fmt='json' for prod, 'text' for dev."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s")
            if False
            else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    root.addHandler(handler)
