# ROADMAP — what stands between here and "fully production ready"

Produced by a 6-lens adversarial audit of this repo (API, pipeline, viewer,
ops, product, testing — 2026-07-21), merged and ranked by
(severity, customer impact, effort). Items only leave this list by shipping
with tests. The bottom section lists what the audit confirmed as already
solid, so no one re-does finished work.

Severity: **blocker** = a real customer or the $0 constraint hits this in
week one · **high** = production incident or churn risk · **medium** = polish
that paying integrators will ask for.

| # | Sev | Effort | Item |
| --- | --- | --- | --- |
| 1 | blocker | S | ~~**CORS for the browser flow**~~ **DONE this PR**: `SCENEFORGE_CORS_ORIGINS` env-gated CORSMiddleware (explicit origins, no wildcard, no credentials) + R2 bucket CORS documented in RUNBOOK. Without it the app's "Connect your API" flow fails preflight — it shipped in the same PR as that flow. |
| 2 | blocker | S | **Nightly DB backup to R2 + tested restore drill.** SQLite/pgdata on one free-tier volume holds every key hash and the only scene→owner mapping; one reclaimed disk is unrecoverable tenant loss. Cron `sqlite3 .backup`/`pg_dump` → `_backups/` prefix in the existing bucket; restore drill in RUNBOOK. |
| 3 | blocker | M | **Honest-failure hardening in the pipeline.** `build_scene` catches only `QualityGateError`/`PipelineError`: GPU OOM, truncated phone video (ffmpeg `CalledProcessError`), missing ffprobe duration escape as raw `worker_crash` tracebacks — violating the honest-failure rule on the most likely real-world failures. Catch-all quality-report writer, OOM retry with halved `cap_max`, stderr-carrying typed errors, per-stage timeouts, hostile-video test corpus (zero-byte, truncated, portrait). |
| 4 | blocker | M | **Storage deletion + retention.** No code path ever deletes an object: raw uploads (50–500 MB each) live in R2 forever and blow the 10 GB free tier in ~a month at target volume. `Storage.delete/delete_prefix`, delete video on success/`upload_abandoned`, retention reaper phase, R2 lifecycle rule, bucket-bytes gauge. Prerequisite for #13. |
| 5 | blocker | M | **`<rf-walkthrough>` lifecycle: survive SPA re-mounts and load errors.** `_loaded` is set before the try and never reset → any transient network error bricks the embed permanently; re-parenting throws; GS3D 0.4.7's `dispose()` bug leaks sort workers on SPA teardown (sf.js already pokes `_loaded` to work around it). Retryable `load()`, clean `disconnectedCallback`, observed `src`, re-mount + bad-src E2E. |
| 6 | blocker | M | **`build_scene` CPU end-to-end test + golden snapshots.** Nothing tests the orchestrator: the `T_final` similarity that keeps splat/plan/semantics in one metric frame, scale-fallback branches, failed-path quality report. A sign error would ship mis-scaled floor plans past 149 green tests. Golden `schema-1.0.json` + golden floorplan diff + unit tests for `_apply_similarity`/`_rotate_quats`. |
| 7 | high | M | **Worker→API result-path fixes.** (a) `_upload_artifacts` drops artifacts on HTTP ≥ 300 while status stays `succeeded` → viewer gets "scene has no splat asset" on a "successful" scene; (b) worker probes 4 hardcoded video extensions, missing allowed containers; (c) webhooks fire synchronously in-request with retries (~37 s) and no `event_id` → duplicate terminal webhooks. Plus a real ASGI contract test for the boundary. |
| 8 | high | S | **Stop GS3D hijacking the host page's keyboard.** Its window-level keydown has no target check: typing `i`/`o`/`p` in any form on a customer's site toggles debug panels; arrow-turn also rolls `camera.up` ~1.4°/press so the horizon tilts. Remove its listener + stopPropagation; E2E typing into a host input. |
| 9 | high | M | **Touch locomotion for walk mode.** On phones — the product's core audience — walk mode has no movement input at all and the hint says "W/A/S/D". Coarse-pointer thumbstick or hold-to-walk, modality-specific hints, `hasTouch` E2E. |
| 10 | high | M | **Scope the worker key.** Any worker key can post results for ANY scene in any state, with attacker-hosted asset URLs served verbatim into third-party embeds. Per-job HMAC callback token, require `processing` state, validate assets are storage keys under the scene's own prefix. |
| 11 | high | M | **Alembic before the first schema-changing release.** `create_all()` never ALTERs: the first added column no-ops on prod Postgres then 500s at query time. Initial revision, upgrade-at-startup, documented SQLite→Postgres promotion. Prerequisite for #17. |
| 12 | high | M | **CI-built, SHA-tagged images + pinned `LINGBOT_SHA`.** Neither Dockerfile builds in CI; `ARG LINGBOT_SHA=main` means rebuilding the "same" image changes the default backend's code — rollback is impossible. Build per push, publish to ghcr.io, pin lingbot + base digest, add a dist/rf.js drift check. |
| 13 | high | M | **`GET /v1/scenes` (list, cursor-paginated) + `DELETE /v1/scenes/{id}` + retention/privacy policy.** Customers can't enumerate scenes (lost id = orphaned paid output) and can't delete videos of people's homes — no right-to-erasure path. Ship with terms/privacy pages linked from the app footers. Depends on #4. |
| 14 | high | M | **Alert delivery.** OPS.md's alert catalog has zero provisioning behind it — nothing pages when the queue backs up. Commit the ~100-line stdlib threshold-checker (cron/compose), Grafana dashboard + rules JSON, fire-a-test-alert deploy step. |
| 15 | high | M | **Partial success.** Semantics + floorplan are computed before splat training but written after — a splat crash discards finished deliverables. Persist CPU artifacts first; add `status=partial` (`splat_failed`) through BuildOutcome and the API. |
| 16 | high | M | **Pipeline RAM peaks.** lingbot NPZ loads ~1.6 GB incl. unused depth channels; all frames stay resident (~1.1 GB) through training. Selective key loading, per-frame confidence masking, memmap frames. |
| 17 | high | M | **Quota + usage metering.** No per-key quota: one key drains the shared Modal credits funding everyone. `monthly_scene_quota` column, GPU-seconds in `_result`, `GET /v1/usage`. Depends on #11. |
| 18 | high | L | **Resumable builds.** 20-minute jobs restart from zero on preemption (routine on free-tier GPUs). Marker-file stage skipping, splat checkpoints every ~2k iters, persistent scratch; the content-deterministic `scene_id` is the ready-made cache key. |
| 19 | medium | M | **`docs/API.md`.** Full endpoint table incl. requeue, 429/413 semantics, limits from settings, complete error-code catalog (`worker_timeout`, `worker_crash`, `upload_abandoned`), at-least-once webhook contract. |
| 20 | medium | S | **Runbook completeness.** Per-secret rotation table (7 credentials, incl. dual-secret webhook overlap), backlog-alert scaling response (`SCENEFORGE_WORKER_MAX_JOBS`), delete the stale hand-edit-the-DB instruction in favor of the shipped requeue endpoint. |

## Confirmed solid (don't redo)

State-machine reliability + reaper gauntlet · edge security from the red-team
pass (proven zero-byte 413) · tenant isolation + key hygiene · observability
signal layer (alerting is the gap, not metrics) · webhook signing/SSRF guard ·
honest-failure capture gates incl. the lingbot confidence gate · backend-agnostic
geometry adapter · structurally-enforced 25 MB splat budget · the CPU pipeline
test suite · viewer core design (lazy load, bounded timeouts, COOP/COEP-free) ·
the E2E harness + app-shell honesty assertions · app accessibility (ARIA
tablist, reduced-motion/contrast/transparency) · $0 deploy scaffolding ·
viewer CD via Pages · quickstart happy path · content-deterministic scene ids.

## The one thing no roadmap item replaces

The **GPU acceptance run** (QUALITY.md): a real capture on a T4, tape-measure
checks, lingbot confidence-floor tuning, COLMAP-vs-lingbot A/B. Several items
above (3, 6, 7, 15, 16, 18) become dramatically easier to verify once that
hardware loop exists.
