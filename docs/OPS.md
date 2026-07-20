# Operations & observability

How to see what SceneForge is doing in production — and get paged when it
misbehaves — using only free tiers. Everything here is stdlib-light so it runs
inside the 1 GB micro profile.

## The three signals

| Endpoint | Purpose | Auth | Who calls it |
| --- | --- | --- | --- |
| `GET /healthz` | **Liveness** — process is up. Always cheap, always 200. | none | uptime monitor, Docker healthcheck |
| `GET /readyz` | **Readiness** — can actually serve: probes the DB (and the queue broker if configured). 200 with `{ready, components}` or **503** when a dependency is down. | none | orchestrator, uptime monitor |
| `GET /metrics` | **Prometheus metrics**. Token-gated: 404 when `SCENEFORGE_METRICS_TOKEN` is unset (disabled by default so scene/tenant counts aren't public), 401 on wrong token. | bearer token or `?token=` | a scraper (Grafana Cloud free, etc.) |

Every response carries an `X-Request-ID` (honored from an inbound header or
minted), and every app log line is JSON carrying that same `request_id` — so
one request is greppable end to end.

## Metrics catalog

| Metric | Type | Labels | Watch for |
| --- | --- | --- | --- |
| `sceneforge_http_requests_total` | counter | route, method, status | 5xx ratio, 401/403 spikes |
| `sceneforge_http_request_duration_seconds` | histogram | route | p95 latency creep |
| `sceneforge_http_in_flight` | gauge | — | saturation on the ⅛-OCPU box |
| `sceneforge_scenes` | gauge | state | **backlog** (queued/processing climbing), failure ratio |
| `sceneforge_reaper_actions_total` | counter | kind=processing_timeout/requeued/upload_abandoned | timeouts rising ⇒ workers dying |
| `sceneforge_worker_results_total` | counter | state | succeeded vs failed rate |
| `sceneforge_webhook_deliveries_total` | counter | result=success/failed/blocked_ssrf | customers not receiving callbacks; `blocked_ssrf` ⇒ someone probing |
| `sceneforge_rejections_total` | counter | kind=body_too_large/rate_limit/outstanding_cap | **abuse signal** — a spike is an attacker or a runaway client |

Scene-state gauges are refreshed from the DB at scrape time (pull), so they
never drift from reality.

## Free scraping + dashboards

**Grafana Cloud free tier** (10k series, 14-day retention — ample here):

1. Create a free stack → get a Prometheus remote-write / hosted Prometheus
   endpoint, or use **Grafana Agent** in `scrape` mode.
2. Point it at `https://<your-host>/metrics` with an
   `Authorization: Bearer <SCENEFORGE_METRICS_TOKEN>` header, ~30 s interval.
3. Import a dashboard built on the metrics above (backlog, latency p95,
   rejection rate, failure ratio).

Zero-infra alternative: a cron/Routine that curls `/metrics`, parses the few
lines it cares about, and posts to a free Discord/Slack webhook on threshold
breach.

## Alerts worth setting (all derivable from the metrics)

| Alert | Condition | Why |
| --- | --- | --- |
| **Service down** | `/healthz` unreachable 2× in a row | the basics — wire UptimeRobot free to it |
| **Not ready** | `/readyz` 503 for > 2 min | DB/queue down; process alive but useless |
| **Backlog growing** | `sceneforge_scenes{state="queued"}` rising for 15 min | workers not keeping up / all dead |
| **Worker death** | `rate(sceneforge_reaper_actions_total{kind="processing_timeout"}[15m]) > 0` | scenes timing out ⇒ investigate the GPU worker |
| **Failure spike** | failed/(succeeded+failed) > 0.3 over 1 h | pipeline or capture-quality regression |
| **Abuse** | `rate(sceneforge_rejections_total[5m])` spikes | flood / oversized-body attacker (the P2 findings in action) |
| **Webhook breakage** | `sceneforge_webhook_deliveries_total{result="failed"}` climbing | customers silently not getting results |

## Uptime monitoring (free)

Point **UptimeRobot** (free: 50 monitors, 5-min interval) or **Better Stack**
free at `https://<your-host>/healthz` (liveness) and `/readyz` (readiness).
Both page/email/Slack on failure.

## Logs

Set `SCENEFORGE_LOG_FORMAT=json` (default) for structured logs;
`SCENEFORGE_LOG_LEVEL` controls verbosity. App logs are JSON with `request_id`;
to also get uvicorn's access logs as JSON, run it with a `--log-config` that
uses the same formatter (or accept uvicorn's default access format — the app's
own logs are what carry the operational signal: reaper actions, webhook
failures, security rejections). On the micro box, Docker's json-file driver
captures stdout; ship to a free log sink (Grafana Loki free, Better Stack)
if you want search + retention.

## Config

| Env | Default | Meaning |
| --- | --- | --- |
| `SCENEFORGE_METRICS_TOKEN` | *(empty ⇒ /metrics disabled)* | bearer token a scraper presents |
| `SCENEFORGE_LOG_FORMAT` | `json` | `json` (prod) or `text` (dev) |
| `SCENEFORGE_LOG_LEVEL` | `INFO` | root log level |
