"""CORS: browser dashboards (the Pages app's "Connect your API" flow) must be
able to call the API cross-origin — but ONLY when the operator opts in with
explicit origins. Default stays locked down (no CORS headers at all), and a
wildcard is never emitted because responses carry tenant data.
"""

from __future__ import annotations

import pytest

APP_ORIGIN = "https://febufenn-cyber.github.io"


@pytest.mark.asyncio
async def test_cors_disabled_by_default(api):
    client, _ctx = api
    r = await client.get("/healthz", headers={"Origin": APP_ORIGIN})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.asyncio
async def test_cors_preflight_and_echo_for_allowed_origin(api_factory):
    async with api_factory(
        cors_origins=f"{APP_ORIGIN}, http://localhost:8080"
    ) as (client, ctx):
        # Preflight for the exact call sf.js makes.
        pre = await client.options(
            "/v1/scenes",
            headers={
                "Origin": APP_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert pre.status_code == 200
        assert pre.headers["access-control-allow-origin"] == APP_ORIGIN
        assert "POST" in pre.headers["access-control-allow-methods"]
        # The actual authenticated request carries the echo too.
        r = await client.post(
            "/v1/scenes",
            json={"filename": "flat.mp4", "content_type": "video/mp4"},
            headers={**ctx["headers"], "Origin": APP_ORIGIN},
        )
        assert r.status_code == 201
        assert r.headers["access-control-allow-origin"] == APP_ORIGIN


@pytest.mark.asyncio
async def test_cors_unknown_origin_gets_no_allowance(api_factory):
    async with api_factory(cors_origins=APP_ORIGIN) as (client, _ctx):
        r = await client.get("/healthz", headers={"Origin": "https://evil.example"})
        # Request still succeeds server-side; the browser simply gets no
        # cross-origin allowance for it.
        assert r.status_code == 200
        assert "access-control-allow-origin" not in r.headers
