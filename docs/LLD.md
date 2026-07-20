# LLD-001 — SceneForge v1: video → labeled 3D walkthrough engine

## 1. Title block

| | |
| --- | --- |
| Drawing no. | LLD-001 (no in-repo LLD standard found → one-page equivalent, brief §3) |
| Product | SceneForge: phone-video walkthrough → Gaussian splat + semantic scene JSON + floor plan + embeddable viewer |
| Author | systems engineering (solo MVP build) |
| Date / revision | 2026-07-17 · **Rev A** |
| Status | Implemented (CPU-verifiable scope); GPU acceptance pending hardware (QUALITY.md) |
| Provenance rule | every external-tool claim here is verified in LICENSES.md from the linked primary source, checked 2026-07-17 |
| Relation to the Drawing House standard | `STANDARD.md` (rev C) is now in the repo; the per-phase design sheets that satisfy it live at `designs/phase1-cli` (**DO-012**), `designs/phase2-api` (**DO-013**), `designs/phase3-viewer` (**DO-014**) and pass `tools/validate.py` (run in CI). This page is the one-page cross-index over them and the as-built record; the DO sheets are the authoritative per-phase drawings. |

## 2. Assembly drawing (system at one glance)

```
 agent's phone video (60–120 s, 1080p)
        │  presigned PUT (R2/S3-compatible)
        ▼
┌─ OCI Always-Free VM (ARM, 2 OCPU/12 GB — verified 2026-06-15 halving) ─┐
│  api/  FastAPI ── Postgres 16 (scenes, hashed api_keys)                │
│   │        └──── Valkey 8 (arq queue)  ◄── states: awaiting_upload →   │
│   │                     ▲                queued → processing →         │
│   └─ webhooks (HMAC) ───┼──────────────  succeeded | failed            │
└──────────────────────────┼─────────────────────────────────────────────┘
                           │ pull (never push): GPUWorker adapter
        ┌──────────────────┼───────────────────┐
        ▼                  ▼                   ▼
   local CUDA box     Modal fn (T4)      HF Space (demo)
        └──────── execute_build(BuildJob) ─────┘
                   pipeline/ rf-scene:
   ingest(ffmpeg,blur,dedup) → geometry(COLMAP global SfM) → scale(OWLv2
   door prior) → semantics(slice→grid→walls/rooms/openings/objects) →
   quality gates → splat(gsplat MCMC ≤25MB .ksplat) → artifacts
        │ presigned PUTs
        ▼
   R2 bucket ── public CDN base ──► <rf-walkthrough> (viewer/dist/rf.js)
                                    + floorplan.svg + semantic.json
```

## 3. BOM (bill of materials)

Complete table with licenses & evidence: **LICENSES.md** (single source of truth).
Key line items: COLMAP 4.1.0 (BSD-3, includes global mapper — GLOMAP merged/archived 2026-03),
gsplat 1.5.3 (Apache-2.0), OWLv2 base-patch16-ensemble (Apache-2.0 incl. weights),
GaussianSplats3D 0.4.7 + three 0.170.0 (MIT), FastAPI/pydantic/SQLAlchemy/arq (MIT/BSD),
Valkey 8 (BSD-3), Postgres 16, ffmpeg (LGPL, subprocess-only), numpy/scipy/shapely/OpenCV.
Opt-in geometry backend: lingbot-map (Robbyant/Ant Group; code Apache-2.0, weights license inferred — see LICENSES.md D29), feed-forward alternative to COLMAP, `--backend lingbot`.
Research-gated (never default): DUSt3R/MASt3R (CC-BY-NC-SA), VGGT (custom Meta license).
Excluded: SpatialLM (no commercially-safe variant), Redis 8 server, Ultralytics, svgwrite.

## 4. Detail drawings (module contracts)

| Module | Contract (in → out) | Key internals |
| --- | --- | --- |
| `pipeline.stages.ingest` | video → curated frames dir + stats | ffmpeg 2–4 fps, long side ≤1280; variance-of-Laplacian ≥ 60; NCC dedup > 0.985; gates: duration ≥ 20 s, kept ≥ 40 |
| `pipeline.stages.geometry` | frames → `GeometryResult` (COLMAP model) | `GeometryBackend` ABC; sequential matching + loop detection (vocab tree); largest model wins; gate: ≥30 frames & ≥55 % registered |
| `pipeline.stages.scale` | model (+detections) → `ScaleResult` | OWLv2 over ≤40 frames; door height from sparse depth (median in box); scale = 2.0 m/median; confidence = f(count, MAD); fallbacks per D8 |
| `pipeline.stages.semantics` | metric Z-up points + cams + dets → rooms/walls/openings/objects + coverage | gravity from camera-plane SVD; 1.0–1.5 m slice → 5 cm occupancy → Hough+merge walls; free-space erosion → room components; wall-gap + detector doors; voxel-BFS clusters → PCA OBBs |
| `pipeline.stages.splat` | frames+model → `.ply` + `.ksplat/.splat` + poster | gsplat MCMC (`cap_max` = 700 k std / 450 k fast); undistort→PINHOLE; export through gravity+scale similarity so all assets share one frame |
| `pipeline.stages.floorplan` | validated `SceneSemantics` → SVG | zero-dep renderer; scale bar, ±tolerance labels, "not survey-grade" footer |
| `pipeline.stages.quality` | stage stats → `Quality` + report file | coverage = boundary-coverage × registration factor; warnings enumerate unseen boundary per room |
| `api` | REST per §5 of QUICKSTART; states as drawn above | tenant isolation via 404; worker `_result` callback; HMAC webhooks |
| `sceneforge_worker` | `BuildJob → BuildOutcome` | adapters local/modal/hf_space; artifacts via presigned URLs only |
| `viewer` | `<rf-walkthrough>` custom element | poster-gated lazy load; orbit/walk; no COOP/COEP requirement |

## 5. Contracts & tolerances

- **Semantic JSON schema v1.0 is frozen** (`pipeline/sceneforge_pipeline/schema.py`, `rf-scene schema`). Extra fields rejected; additive optional fields allowed within 1.x; breaking ⇒ version bump.
- **Dimensions**: ±10 % stated tolerance at `scale.confidence ≥ 0.5`; ±15 % for `camera_height_prior`; never survey-grade, stated in JSON, SVG footer and viewer HUD.
- **Payload budget**: compact splat ≤ 25 MB (hard warning + report field over budget; MCMC cap sized to stay under).
- **GPU time budget**: ≤ ~10 min/scene on T4 (fast preset ≤ ~5 min target) — to be measured and recorded in RUNBOOK.md/QUALITY.md on first hardware run.
- **No hallucinated geometry**: rooms derive only from observed free space; unseen boundary → coverage warnings; capture-contract violations fail fast with machine-readable reports (`reason_code`, `capture_rule`). Generative infill is out of scope for v1.
- **Cost ceiling**: $0 fixed/month (OCI free VM + R2 free tier + GitHub Pages); variable GPU inside Modal $30/mo credits ⇒ ~150–300 fast scenes/month free (T4 ≈ $0.59/hr verified).

## 6. Process plan (build & acceptance)

| Phase | Deliverable | Acceptance | Status |
| --- | --- | --- | --- |
| 1 — engine CLI | `rf-scene build video.mp4 -o out/` → splat + semantic.json + floorplan.svg + quality_report.json + viewer.html | 60–90 s 1080p video end-to-end ≤ 20 min on T4; splat ≤ 25 MB; floor plan matches; dims ±10 % vs tape measure | code + 65 CPU tests done; **GPU acceptance pending hardware** (QUALITY.md protocol written) |
| 2 — API | POST/GET /v1/scenes, webhook, keys, compose (ARM) + worker image (x86 CUDA) | happy path + honest-failure path (10 s clip → `failed`/insufficient coverage) | done; 36 tests incl. both paths + reliability gauntlet |
| 3 — viewer + quickstart | rf.js web component, demo page, ≤10-line integration | smooth on mid-range Android, sane 4G payload | component + 720 KB bundle done; device test pending hardware |

## 7. Revision history

| Rev | Date | Change |
| --- | --- | --- |
| A | 2026-07-17 | Initial LLD; GLOMAP→COLMAP 4.1 migration absorbed (D3); OCI free-tier halving absorbed (D17); SpatialLM excluded after license verification (D18) |
