"""Edge security controls: request-body cap + per-key rate/quota limiting.

Added after the P2 red-team pass (see SECURITY.md). Two independent controls:

* ``BodyLimitMiddleware`` — the critical one. FastAPI buffers the *entire*
  request body while resolving a Pydantic body param, and it does so BEFORE
  the auth dependency runs — so a single anonymous ~300 MB POST to /v1/scenes
  would OOM-kill the 320 MB micro container regardless of the bogus key. This
  ASGI middleware rejects oversized requests (Content-Length check + a
  streamed byte counter for chunked bodies with no length) with 413 before a
  handler ever sees them. The dev-only local-upload path is exempted (it has
  its own larger cap and only exists on the local-storage backend).

* ``RateLimiter`` — a per-key token bucket (in-process). Correct for the
  single-uvicorn-worker micro profile; on a multi-worker A1 deployment move
  this to a shared Redis counter (documented in SECURITY.md). Paired with a
  per-key cap on outstanding non-terminal scenes enforced in the route.
"""

from __future__ import annotations

import time
from collections import defaultdict

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_EXEMPT_PREFIXES = ("/v1/_local-upload/",)  # dev-only, local-storage-gated, self-capped


class BodyLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` before buffering them."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Fast path: an honest Content-Length over the cap → 413 without reading
        # a single body byte (defeats the large-Content-Length attack cheaply).
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await _reject_413(send)
                        return
                except ValueError:
                    await _reject_413(send)
                    return

        # Robust path (also covers chunked bodies with no Content-Length):
        # buffer incoming chunks up to the cap, then either 413 (before the app
        # ever runs) or replay the buffered messages to the app. Buffering is
        # bounded by max_bytes, so this cannot itself be a memory-DoS.
        buffered: list[Message] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                buffered.append(message)
                if message["type"] == "http.disconnect":
                    break
                continue
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                await _reject_413(send)
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        replay = iter(buffered)

        async def replay_receive() -> Message:
            try:
                return next(replay)
            except StopIteration:
                return await receive()  # any trailing disconnect

        await self.app(scope, replay_receive, send)


async def _reject_413(send: Send) -> None:
    try:
        from .observability import metrics

        metrics.rejections.inc(kind="body_too_large")
    except Exception:
        pass
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send(
        {"type": "http.response.body", "body": b'{"detail":"Request body too large"}'}
    )


class RateLimiter:
    """Per-identity token bucket. `capacity` tokens, refilled at `rate`/sec.

    In-process only — fine for the single-worker micro profile. `allow`
    returns False when the bucket is empty. Buckets are created lazily and
    pruned opportunistically so idle keys don't leak memory.
    """

    def __init__(self, capacity: int, refill_per_sec: float, clock=time.monotonic):
        self.capacity = capacity
        self.refill = refill_per_sec
        self.clock = clock
        self._tokens: dict[str, float] = defaultdict(lambda: float(capacity))
        self._last: dict[str, float] = {}

    def allow(self, identity: str) -> bool:
        now = self.clock()
        last = self._last.get(identity, now)
        # Refill.
        self._tokens[identity] = min(
            self.capacity, self._tokens[identity] + (now - last) * self.refill
        )
        self._last[identity] = now
        if self._tokens[identity] >= 1.0:
            self._tokens[identity] -= 1.0
            self._maybe_prune(now)
            return True
        return False

    def _maybe_prune(self, now: float) -> None:
        # Drop full, idle buckets to bound memory under key churn.
        if len(self._tokens) <= 4096:
            return
        stale = [
            k for k, t in self._last.items()
            if now - t > 3600 and self._tokens.get(k, 0) >= self.capacity
        ]
        for k in stale:
            self._tokens.pop(k, None)
            self._last.pop(k, None)
