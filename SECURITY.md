# Security

## Reporting

Email the maintainer (see repo owner) with steps to reproduce. No bug-bounty
program at this stage; please allow reasonable time to remediate before
disclosure.

## Threat model

Public $0-infra deployment. Adversaries considered: anonymous internet
attackers, malicious/leaked API-key holders (multi-tenant), malicious
`webhook_url` owners, malicious uploaded video content, and hostile scene
assets rendered inside third-party real-estate sites' browsers.

## P2 red-team pass (2026-07-20)

An adversarial review fanned out across six attack surfaces (authz/tenancy,
SSRF, DoS/limits, injection/traversal, client XSS/supply-chain,
secrets/crypto/config); every candidate finding was refutation-verified with a
runnable POC. **4 issues were confirmed and fixed; 13 were refuted.**

### Fixed

| # | Sev | Finding | Fix | Test |
| --- | --- | --- | --- | --- |
| 1 | **Critical** | No request-body cap — FastAPI buffers the whole body *before* auth runs, so one anonymous ~300 MB POST OOM-kills the 320 MB micro container (CWE-770). | `BodyLimitMiddleware` (security.py): Content-Length fast-path 413 + bounded buffer-and-replay for chunked bodies. Proven to reject the 300 MB attack **reading zero body bytes**. Cap `SCENEFORGE_MAX_REQUEST_BODY_KB` (default 4096); dev local-upload path exempt. | `test_security.py::test_oversized_body_*`, `test_body_limit_middleware_streamed_chunks` |
| 2 | High | No rate limit / per-key quota on scene creation — a leaked key floods unbounded rows + presigned URLs, and abandoned `awaiting_upload` rows were never reaped (CWE-770). | Per-key token-bucket rate limit + per-key cap on outstanding non-terminal scenes (both in `security.py`/route); reaper now expires `awaiting_upload` past presign expiry (`upload_abandoned`). | `test_create_rate_limited`, `test_outstanding_scene_cap`, `test_reaper_expires_abandoned_uploads` |
| 3 | Medium | Reaper loaded every stuck scene with an unbounded `SELECT().all()` — a large stale backlog OOMs the reaper tick (which runs in the API process) (CWE-770). | Both reaper queries bounded with `.limit(reaper_batch)` (default 500); backlog drains across ticks with O(batch) memory. | `test_reaper_batches_queries` |
| 4 | Low | `content_type` accepted unvalidated into the presigned PUT — a tenant could have their upload stored/served as `text/html` (CWE-79-adjacent). | Server allowlists safe video container types; anything else coerced to `application/octet-stream`. | `test_hostile_content_type_coerced`, `test_allowed_content_type_preserved` |

### Refuted (verified NOT exploitable on the shipped deploy)

- **SSRF via webhook DNS-rebinding** — the TOCTOU code gap is real (and documented, `DECISIONS.md` D26): `is_url_allowed` resolves once, httpx re-resolves. But three independent verifiers proved it inert on the shipped profile: the SSRF is **blind** (delivery result is only logged, never surfaced to any endpoint), httpx `follow_redirects` defaults off (no redirect vector), it's **POST-only** so Oracle IMDS (read-only GET, IMDSv2 needs a token) yields nothing, and Valkey's cross-protocol guard **aborts** an inline HTTP POST (POC: `SET`/`CONFIG` smuggled in a webhook body never executed). Accepted risk; see below.
- **`/_local-upload` traversal / unauth write** — that route only registers under `SCENEFORGE_STORAGE=local` (dev); the production default is `s3`, so it's absent.
- **`_result` cross-tenant / customer escalation** — gated by `is_worker` (D25); customer keys get 403.
- **Empty `webhook_secret` → unsigned webhooks** — accurate but the receiver, not us, decides to require signatures; no SceneForge inbound trusts it.
- **Viewer poster CSS-injection / `viewer_html.py` breakout** — values are server-controlled (scene_id regex-gated `scn_[a-z0-9]{8,32}`, filenames fixed); no attacker-reachable payload.
- (…and others: unsalted-but-high-entropy key hashing, missing CORS on a pure API, etc. — all defense-in-depth, no reachable exploit.)

## Accepted risks (documented, with a plan)

- **Webhook SSRF DNS-rebinding TOCTOU** — inert today (above). If the API host
  ever gains an *unauthenticated internal HTTP neighbor*, this becomes live;
  the fix at that point is a pinned-IP httpx transport (resolve once → verify
  the IP is global → connect to that exact IP with the original Host header).
  Tracked so it isn't forgotten when the topology changes.
- **In-process rate limiter** — correct for the single-worker micro profile.
  A multi-worker A1 deployment needs a shared counter (Redis/Valkey
  `INCR`+`EXPIRE`); documented in `docs/DEPLOY_E2_MICRO.md`'s upgrade notes.

## Standing controls (pre-existing)

- API keys: 256-bit random (`secrets.token_urlsafe(32)`), stored as SHA-256
  hashes; plaintext shown once. Worker vs customer roles; `_result` worker-only.
- Tenant isolation: cross-tenant access returns 404 (no existence oracle).
- Webhooks: HMAC-SHA256 signatures; SSRF guard rejects non-global targets.
- No secrets in the repo — `.env` only. TLS via Cloudflare Tunnel (no open
  ports) or Caddy. Atomic state transitions; reaper prevents stuck scenes.
