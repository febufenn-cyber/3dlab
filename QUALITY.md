# QUALITY

Evidence for what works, and an honest ledger of what is not yet proven.
(Per the no-hallucination principle, this file never claims unmeasured results.)

## Verified now (this environment, CPU, 2026-07-17)

- **100 automated tests green** (75 pipeline + 25 API), covering:
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
    firing rules, path-traversal guard, BuildJob/BuildOutcome round-trips.

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
