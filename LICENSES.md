# LICENSES

Every external component, verified from its **actual repo LICENSE file /
model card** (not from memory, not from the design brief) on **2026-07-17**
by fetching the linked evidence. The commercial (default) code path uses only
the components marked ✅. Anything ❌ is either excluded entirely or locked
behind the `--research` flag (`rf-scene build --research`), which is never to
be used in a commercial deployment.

Verification notes: this environment's egress proxy blocks `huggingface.co`
and `modal.com` directly; those rows were verified via the projects' canonical
GitHub sources and search-indexed copies of the model cards, as noted. Re-verify
those two directly before a production launch. **lingbot-map weights** are a
sharper case: unlike OWLv2 (whose upstream states verbatim that code *and*
checkpoints are Apache-2.0), lingbot-map's README declares the *project*
Apache-2.0 but does not separately name the weights, and its HF card tag could
not be fetched here — so the weights row is marked *inferred*, not observed,
and lingbot is kept off the default path until confirmed (COLMAP is default).

## Commercial path (default) — all permissive

| Component | Use | License | Commercial-safe | Version pinned | Evidence |
| --- | --- | --- | --- | --- | --- |
| COLMAP (+pycolmap) | SfM features/matching, undistort, global mapper (≥4.1) — **default** geometry backend | BSD-3-Clause | ✅ | 4.1.0 | [COPYING.txt](https://github.com/colmap/colmap/blob/main/COPYING.txt) |
| lingbot-map (Robbyant / Ant Group) | feed-forward streaming geometry backend (opt-in `--backend lingbot`) | **code** Apache-2.0 (verified from README); **weights** license *inferred* Apache-2.0 — repo declares no separate weights license and the HF card is proxy-blocked in this env, so **confirm directly before commercial use** | ⚠️ code ✅ / weights unverified | commit-pinned at deploy | [repo](https://github.com/Robbyant/lingbot-map) · [HF weights](https://huggingface.co/robbyant/lingbot-map) |
| GLOMAP (legacy fallback) | global SfM (final release; repo **archived 2026-03-09**, merged into COLMAP 4.1) | BSD-3-Clause | ✅ | 1.2.0 | [LICENSE](https://github.com/colmap/glomap/blob/main/LICENSE) |
| gsplat | 3DGS training/rasterization | Apache-2.0 | ✅ | 1.5.3 | [LICENSE](https://github.com/nerfstudio-project/gsplat/blob/main/LICENSE) |
| GaussianSplats3D | web splat viewer + .ksplat converter (maintenance mode since 2025; MIT stands) | MIT | ✅ | 0.4.7 | [LICENSE](https://github.com/mkkellogg/GaussianSplats3D/blob/main/LICENSE) |
| Three.js | 3D runtime for the viewer | MIT | ✅ | 0.170.0 | [LICENSE](https://github.com/mrdoob/three.js/blob/dev/LICENSE) |
| OWLv2 checkpoint `google/owlv2-base-patch16-ensemble` | open-vocab door/object detection (scale prior + labels) | Apache-2.0 (code **and** weights, per google-research/scenic README + HF card tag) | ✅ | HF unversioned | [scenic OWL-ViT README](https://github.com/google-research/scenic/blob/main/scenic/projects/owl_vit/README.md) |
| transformers | OWLv2 inference | Apache-2.0 | ✅ | 5.14.1 | [LICENSE](https://github.com/huggingface/transformers/blob/main/LICENSE) |
| PyTorch | training runtime | BSD-style | ✅ | 2.13.0 | [LICENSE](https://github.com/pytorch/pytorch/blob/main/LICENSE) |
| FastAPI / uvicorn / pydantic | API service | MIT / BSD-3 / MIT | ✅ | 0.139.2 / 0.51.0 / 2.13.4 | repo LICENSE files |
| SQLAlchemy | DB layer | MIT | ✅ | 2.0.51 | repo LICENSE |
| arq (+redis-py client) | job queue client (arq is in maintenance mode; RQ (BSD-2) is the named fallback) | MIT | ✅ | 0.28.0 | [LICENSE](https://github.com/python-arq/arq/blob/main/LICENSE) |
| **Valkey** (server) | queue broker in docker-compose | BSD-3-Clause | ✅ | 8-alpine image | [COPYING](https://github.com/valkey-io/valkey/blob/unstable/COPYING) |
| PostgreSQL | scene/key store | PostgreSQL License | ✅ | 16-alpine image | postgresql.org/about/licence |
| numpy / scipy / shapely / opencv-python-headless | geometry + vision math | BSD-3 / BSD-3 / BSD-3 / MIT wrapper + Apache-2.0 core | ✅ | 2.5.1 / 1.18.0 / 2.1.2 / 5.0.0.93 | repo LICENSE files |
| boto3 | S3/R2 storage adapter | Apache-2.0 | ✅ | 1.43.50 | repo LICENSE |
| esbuild | viewer bundler (build-time only) | MIT | ✅ | 0.28.1 | repo LICENSE |
| SPZ (Niantic) | optional splat container (not yet wired; candidate) | MIT | ✅ | 3.0.0 | [LICENSE](https://github.com/nianticlabs/spz/blob/main/LICENSE) |
| SAM2 | optional masks (not wired in v1; pre-cleared) | Apache-2.0 (code + checkpoints, per README) | ✅ | SAM 2.1 | [facebookresearch/sam2](https://github.com/facebookresearch/sam2) |
| Grounding DINO | alternative open-vocab detector | Apache-2.0 (code; release weights inherit) | ✅ | v0.1.0-alpha2 | [LICENSE](https://github.com/IDEA-Research/GroundingDINO/blob/main/LICENSE) |
| Depth-Anything-V2 **Small only** | optional depth assist (not wired in v1) | Apache-2.0 (Small checkpoint; **Base/Large/Giant are CC-BY-NC-4.0 — forbidden here**) | ✅ (Small only) | — | [repo README license section](https://github.com/DepthAnything/Depth-Anything-V2) |

### Copyleft-but-contained (shipped as separate processes/binaries, never linked)

| Component | Use | License | Handling |
| --- | --- | --- | --- |
| ffmpeg/ffprobe | frame extraction via subprocess | LGPL-2.1+ (default build); GPL if built with `--enable-gpl` | We only shell out — copyleft does not reach our code. Shipping it in the worker image is distribution: the image must carry the ffmpeg license text and use a distro LGPL/GPL build (apt), never `--enable-nonfree`. |
| GEOS (inside shapely wheels), ffmpeg libs (inside opencv wheels) | transitively bundled | LGPL-2.1 | Dynamically linked by upstream wheels; include license notices in distributed images. |

## Research flag only (`--research`) — NOT commercially safe

| Component | Why gated | License | Evidence |
| --- | --- | --- | --- |
| DUSt3R / MASt3R | feed-forward geometry backend candidate | CC-BY-NC-SA-4.0 (code AND weights, plus NC dataset terms stacked on checkpoints) | [naver/dust3r](https://github.com/naver/dust3r), [naver/mast3r](https://github.com/naver/mast3r) LICENSE + CHECKPOINTS_NOTICE |
| VGGT (Meta) | feed-forward geometry backend candidate | Custom "VGGT License" v1 (AUP, share-alike, unilateral changes — not permissive). Weights: `VGGT-1B` CC-BY-NC-4.0; `VGGT-1B-Commercial` allows commercial use but is **gated** and still non-permissive | [facebookresearch/vggt](https://github.com/facebookresearch/vggt) |
| SpatialLM | model-assisted layout | **No variant is commercially safe**: all released weights embed a CC-BY-NC-4.0 point-cloud encoder (SceneScript in 1.0, Sonata in 1.1); repo LICENSE is Llama 3.2 Community | [manycore-research/SpatialLM](https://github.com/manycore-research/SpatialLM) |
| Depth-Anything-V2 Base/Large/Giant | higher-quality depth assist | CC-BY-NC-4.0 | repo README |
| Inria 3DGS reference rasterizer | never used (gsplat replaces it) | non-commercial | graphdeco-inria/gaussian-splatting |

## Explicitly avoided

| Component | Reason |
| --- | --- |
| Ultralytics/YOLO builds | AGPL-3.0 — viral for a hosted service |
| Redis 8 server | RSALv2/SSPLv1/AGPLv3 tri-license — Valkey (BSD-3) used instead |
| svgwrite | archived/inactive since 2024 — floor plan SVG is hand-generated (zero-dep) |
