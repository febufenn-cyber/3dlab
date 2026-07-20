# SceneForge

**Phone video in → labeled, walkable 3D scene out.** Developer infrastructure
for spatial capture: a 60–120 s walkthrough video of an interior becomes

1. a photoreal **Gaussian-splat scene** a buyer walks through in the browser,
2. a **semantic scene JSON** — rooms, walls, doors, windows, object boxes,
   estimated dimensions with stated tolerance (the differentiator: structured,
   queryable geometry, not just pixels),
3. an auto-generated **2D floor plan** (SVG),
4. an **embeddable viewer** any site integrates with one script tag.

```html
<script src="https://your-cdn/rf.js"></script>
<rf-walkthrough src=".../scene.ksplat" poster=".../poster.png" mode="walk"></rf-walkthrough>
```

## Honesty as a product rule

SceneForge never invents geometry. Uncovered areas are reported
(`quality.warnings`), bad captures fail fast with the exact broken capture
rule, and every dimension carries `scale.{method, confidence}` ± tolerance.
Estimates are never survey-grade and say so.

## Monorepo

| Path | What | Docs |
| --- | --- | --- |
| `pipeline/` | the engine: `rf-scene build video.mp4 -o out/` (GPU worker) | [LLD](docs/LLD.md) |
| `api/` | FastAPI + queue + storage + GPU adapters + Docker | [RUNBOOK](docs/RUNBOOK.md) |
| `viewer/` | `<rf-walkthrough>` web component (`dist/rf.js`) | [viewer/README](viewer/README.md) |
| `docs/` | LLD, quickstart, capture contract, runbook | [QUICKSTART](docs/QUICKSTART.md) · [CAPTURE_GUIDE](docs/CAPTURE_GUIDE.md) |
| meta | [DECISIONS.md](DECISIONS.md) · [LICENSES.md](LICENSES.md) (all verified) · [QUALITY.md](QUALITY.md) | |

## Quick dev loop (CPU parts)

```bash
pip install -e pipeline[dev] -e api[dev]
python -m pytest pipeline/tests api/tests          # 121 tests + browser E2E
rf-scene schema                                     # frozen contract v1.0
uvicorn sceneforge_api.main:app                     # dev API (SQLite + local storage)
cd viewer && npm install && npm run build           # dist/rf.js
```

The GPU stages (COLMAP/gsplat/OWLv2) run in `api/docker/Dockerfile.worker` —
see the [runbook](docs/RUNBOOK.md) for local-GPU, Modal, and HF-Space paths and
the verified free-tier cost model ($0 fixed; ~$0.10/scene GPU inside free credits).

Commercial path is Apache/MIT/BSD-only — every component license verified from
primary sources in [LICENSES.md](LICENSES.md). Non-commercial research models
(DUSt3R/MASt3R/VGGT class) exist only behind `rf-scene build --research`.
