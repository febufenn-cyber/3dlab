"""Completion webhooks with HMAC-SHA256 signatures.

POSTs {event, scene} to the customer's webhook_url when a scene reaches a
terminal state. The signature lets integrators verify authenticity:

    X-SceneForge-Signature: sha256=<hexdigest of HMAC(secret, body)>

Delivery is best-effort with 3 retries; failures are logged, never fatal —
the API state remains the source of truth (customers can poll GET /v1/scenes).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def is_url_allowed(url: str) -> bool:
    """SSRF guard: http(s) only, and the host must not resolve to a private,
    loopback, link-local or otherwise non-global address.

    Note the residual risk: DNS may re-resolve between this check and the
    request (TOCTOU). Acceptable for v1 because webhooks carry no privileged
    response back to the caller; revisit with a pinned-IP transport if the
    API host ever gains sensitive internal neighbors.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        return len(infos) > 0
    except (OSError, ValueError):
        return False


async def deliver(url: str, payload: dict, secret: str = "", retries: int = 3) -> bool:
    if not is_url_allowed(url):
        log.warning("webhook %s blocked by SSRF guard (non-global address or bad scheme)", url)
        return False
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "sceneforge-webhook/1"}
    if secret:
        headers["X-SceneForge-Signature"] = sign(secret, body)
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(retries):
            try:
                resp = await client.post(url, content=body, headers=headers)
                if resp.status_code < 300:
                    return True
                log.warning("webhook %s → HTTP %s (attempt %d)", url, resp.status_code, attempt + 1)
            except httpx.HTTPError as e:
                log.warning("webhook %s failed: %s (attempt %d)", url, e, attempt + 1)
            await asyncio.sleep(2 ** attempt)
    return False
