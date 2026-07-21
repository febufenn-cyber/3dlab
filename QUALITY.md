# QUALITY

Evidence for what works, and an honest ledger of what is not yet proven.
(Per the no-hallucination principle, this file never claims unmeasured results.)

## Verified now (this environment, CPU, 2026-07-17)

- **151 automated tests green** (78 pipeline + 61 API + 12 real-browser E2E), covering:
  - frozen schema v1.0 (brief's example document validates; extra fields,
    bad ids, wrong units all rejected),
  - COLMAP binary model I/O round-trip (incl. rotation↔quaternion round-trip),
  - lingbot-map → ColmapModel conversion: c2w→w2c pose inversion recovers
    known camera centers, confidence filtering, colour sampling, non-finite
    drop, point cap, and backend registration/selectability,
  - blur/duplicate curation and both ingest quality gates,
  - registration quality gate + research-license gating,
  - door-prior scale math (synthetic pinhole scene recovers scale exactly;
    too-few-doors → confidence 0; user-dimension + camera-prior fallbacks),
  - semantic extraction on a synthetic 4×3 m room: 1 room found, area within
    ±20 %, ceiling 2.6 m ± 0.25, walls both orientations, the 0.9 m door gap
    found, table OBB found (long side 1.2 m ± 0.3), gravity recovery from a
    random SO(3)+translation, unseen-region boundary-coverage drop,
  - floor plan SVG (well-formed XML, labels/areas/tolerance/disclaimer/scale bar),
  - splat export: .splat 32-byte layout round-trip, PLY header/f_rest layout,
  - **integration: our PLY → GaussianSplats3D `convert-to-ksplat` → `.ksplat`
    loads back in the real loader with the correct splat count** (validates the
    exact byte layout the viewer consumes),
  - **real-ffmpeg ingest integration** (ffmpeg 6.1): a genuine synthesized
    25 s video → 75 frames at 3 fps, long-side cap honored without upscaling,
    blurry/static capture rejected with the right `reason_code`,
  - worker job flow (`run_job.process_scene`): processing→succeeded posts, the
    frozen asset-key mapping (`scene.ksplat`→`scene` etc.), video-extension
    probing, crash → `failed`/`worker_crash`, honest-failure pass-through,
  - API: auth, tenant isolation (404 across keys), full state machine, honest
    failure path (short clip → `failed` + `capture_rule`), webhook HMAC +
    firing rules, path-traversal guard, BuildJob/BuildOutcome round-trips,
  - **reliability gauntlet**: double-confirm races enqueue exactly once,
    queue outages never lose a scene, the reaper fails stuck `processing`
    scenes honestly (+ webhook) and re-enqueues stale `queued` once per
    window, requeue endpoint state-gated and tenant-isolated, zombie-worker
    reports can't regress terminal states but real late results beat timeouts,
  - **real-browser E2E** (Playwright + headless Chromium, in CI): the shipped
    `rf.js` + sample scene load end-to-end to a rendering WebGL canvas —
    element upgrade, poster gate, .ksplat parse, GaussianSplats3D boot, walk
    mode; component load stalls surface `rf-error` within a bounded timeout.
  - **app UI E2E** (`viewer/app/`, both experiences): 2D page boots with zero
    page errors and no `xr` attr; insight stats populate from the real
    `semantic.json` incl. the synthetic-sample warning; the upload demo is
    labeled SIMULATION; the honest-failure demo surfaces `capture_rule`;
    the Vision page's XR capability chips refuse to claim support headless
    Chromium doesn't have; `xr="vr"` never breaks flat loading, `rf-xr`
    reports `supported: false` honestly, **flat-screen orbit controls still
    exist when XR is unsupported** (regression for GS3D 0.4.7 dropping its
    controls whenever `webXRMode` is set), and neither page overflows
    horizontally at a 320 px viewport,
  - **CORS opt-in** (`test_cors.py`): disabled by default (no headers),
    explicit-origin preflight + echo for the browser dashboard flow, unknown
    origins get no allowance — and never a wildcard.
  - The app UI additionally survived a **51-agent adversarial review**
    (6 lenses × refutation-verified): 36 confirmed findings fixed in place —
    incl. WCAG AA contrast (tier-3 text 0.40→0.62), aria-live status/alert
    announcements, XSS-escaping every API-derived interpolation, the
    reload-crash + WebGL-context-leak lifecycle paths, capability-gated
    `webXRMode`, and the Y-up re-basing that keeps VR scenes upright.
  - **security regressions** (P2 red-team, `SECURITY.md`): the unauth
    ~300 MB-POST OOM is 413'd reading zero body bytes (proven), per-key rate
    limit + outstanding-scene cap fire, reaper batches its queries and expires
    abandoned uploads, hostile `content_type` coerced to octet-stream.
  - **observability** (`docs/OPS.md`): Prometheus registry renders valid
    exposition (cumulative histogram buckets, label escaping), `/metrics`
    token-gated (404 disabled / 401 wrong token) with scene-state gauges
    refreshed from the DB at scrape, `/readyz` 200-with-components / 503 on
    DB-down, `X-Request-ID` on every response, JSON logs carry the request id,
    edge rejections + reaper/webhook/worker events counted.

## Pending hardware (cannot be produced in this workspace — no GPU, no camera)

The brief's Phase-1 acceptance requires a real handheld video and a T4-class
GPU. This repo intentionally ships the protocol instead of fabricated numbers:

1. Record a 60–90 s 1080p walkthrough of a 1–2 room flat per
   `docs/CAPTURE_GUIDE.md`; tape-measure ≥ 3 dimensions (room long side, a
   door width, ceiling height) and note them here.
2. `rf-scene build flat.mp4 -o out/ --quality standard` on a T4
   (or `modal deploy` + one API job).
3. Commit `out/quality_report.json` (NOT the video) plus this table:

| Metric | Acceptance | Measured |
| --- | --- | --- |
| End-to-end wall-clock (T4) | ≤ 20 min | _pending_ |
| GPU stage time | ≤ ~10 min | _pending_ |
| Compact splat size | ≤ 25 MB | _pending_ |
| Tape-measure deltas | within ±10 % | _pending_ |
| Floor plan visual match | human check | _pending_ |
| Mid-range Android viewer FPS | smooth (~30+) | _pending_ |

Known-risk items to watch on first hardware run (flagged in code comments):
gsplat MCMC hyperparameters on sparse indoor clouds, OWLv2 door recall at
score ≥ 0.30 on Indian interior stock, ksplat SH quantization at level 2.

**lingbot-map is now the default backend (D34) but has never run end-to-end
here** (no GPU). The acceptance run must additionally: (a) confirm lingbot
produces poses/points that flow through undistort-skip → gsplat cleanly;
(b) tune the confidence-gate floors (`lingbot_min_confident_points` 2000 /
`lingbot_min_confident_ratio` 10 %) against real captures so honest failures
fire without false rejects; and ideally (c) run COLMAP vs lingbot on the same
video to quantify the pose-accuracy / splat-quality trade before relying on the
faster default in production. Until then, `--backend colmap_glomap` is the
verified-working classical path.
