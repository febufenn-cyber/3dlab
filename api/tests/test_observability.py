"""Ops-layer tests: metrics registry, /metrics gate, /readyz, request tracing,
structured logs."""

import json
import logging

import pytest

from sceneforge_api.observability import (
    JsonLogFormatter,
    Metrics,
    request_id_ctx,
)

pytestmark = pytest.mark.asyncio


# --- metric primitives (unit) -----------------------------------------------


def test_counter_and_labels():
    m = Metrics()
    m.http_requests.inc(route="/a", method="GET", status="200")
    m.http_requests.inc(route="/a", method="GET", status="200")
    m.http_requests.inc(route="/a", method="GET", status="500")
    text = "\n".join(m.http_requests.render())
    assert 'sceneforge_http_requests_total{method="GET",route="/a",status="200"} 2.0' in text
    assert 'status="500"} 1.0' in text


def test_histogram_cumulative_buckets():
    m = Metrics()
    for v in (0.02, 0.2, 3.0):
        m.http_latency.observe(v, route="/a")
    text = "\n".join(m.http_latency.render())
    # le=0.025 catches the 0.02; le=0.25 catches 0.02+0.2; +Inf catches all 3.
    assert '_bucket{le="0.025",route="/a"} 1' in text
    assert '_bucket{le="0.25",route="/a"} 2' in text
    assert '_bucket{le="+Inf",route="/a"} 3' in text
    assert '_count{route="/a"} 3' in text
    assert '_sum{route="/a"} 3.22' in text


def test_label_escaping():
    m = Metrics()
    m.rejections.inc(kind='a"b\\c')
    text = "\n".join(m.rejections.render())
    assert r'kind="a\"b\\c"' in text  # quotes and backslashes escaped


def test_reset_clears():
    m = Metrics()
    m.rejections.inc(kind="x")
    m.reset()
    assert "sceneforge_rejections_total 0" in "\n".join(m.rejections.render())


# --- structured logging -----------------------------------------------------


def test_json_log_formatter_includes_request_id():
    token = request_id_ctx.set("req-123")
    try:
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        line = JsonLogFormatter().format(rec)
        doc = json.loads(line)
        assert doc["msg"] == "hello world"
        assert doc["request_id"] == "req-123"
        assert doc["level"] == "INFO"
    finally:
        request_id_ctx.reset(token)


# --- endpoints (integration) ------------------------------------------------


async def test_healthz_liveness(api):
    client, _ = api
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_readyz_reports_db_up(api):
    client, _ = api
    r = await client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["components"]["database"] is True


async def test_readyz_503_when_db_down(api, monkeypatch):
    client, ctx = api

    def boom(*a, **k):
        raise ConnectionError("db unreachable")

    # Simulate the DB being unreachable at check time.
    monkeypatch.setattr(ctx["database"], "sessionmaker", boom)
    r = await client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["components"]["database"] is False


async def test_metrics_404_without_token(api):
    """Metrics are disabled by default so tenant/scene counts aren't exposed."""
    client, _ = api
    r = await client.get("/metrics")
    assert r.status_code == 404


async def test_request_id_header_present(api):
    client, _ = api
    r = await client.get("/healthz")
    assert "x-request-id" in {k.lower() for k in r.headers}


async def test_metrics_gated_and_scene_gauges(api_factory):
    async with api_factory(metrics_token="s3cret") as (client, ctx):
        # Wrong token → 401.
        assert (await client.get("/metrics", headers={"Authorization": "Bearer nope"})).status_code == 401
        # Generate some traffic to populate metrics + scenes.
        for _ in range(3):
            await client.post("/v1/scenes", json={"filename": "f.mp4"}, headers=ctx["headers"])
        r = await client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200
        body = r.text
        assert "sceneforge_http_requests_total" in body
        # scene-state gauge refreshed at scrape: 3 awaiting_upload.
        assert 'sceneforge_scenes{state="awaiting_upload"} 3.0' in body
        # the POSTs were counted.
        assert 'route="/v1/scenes"' in body


async def test_rejection_metric_recorded(api_factory):
    async with api_factory(metrics_token="t", create_rate_capacity=2,
                           create_rate_per_sec=0.001, max_outstanding_scenes=1000) as (client, ctx):
        for _ in range(5):
            await client.post("/v1/scenes", json={"filename": "f.mp4"}, headers=ctx["headers"])
        r = await client.get("/metrics", headers={"Authorization": "Bearer t"})
        assert 'sceneforge_rejections_total{kind="rate_limit"}' in r.text
