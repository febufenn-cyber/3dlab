# Runbook — deploy, operate, and what it costs

## Topology (zero fixed cost — terms verified 2026-07-17)

| Piece | Where | Free-tier reality (verified, see notes) |
| --- | --- | --- |
| API + Postgres + Valkey | OCI Always-Free Ampere A1 VM | **2 OCPU / 12 GB RAM** (1,500 OCPU-hrs + 9,000 GB-hrs/mo — HALVED from 4/24 on 2026-06-15 with no announcement; brief's figure is stale). 200 GB block volume, 10 TB egress. Watch for silent limit changes; existing over-limit instances were stopped by Oracle. |
| Splat/asset storage | Cloudflare R2 | 10 GB-mo storage, 1 M class-A + 10 M class-B ops/mo, **zero egress** — ~400 scenes at 25 MB before paying $0.015/GB-mo |
| Viewer CDN (`rf.js`) + demo | GitHub Pages / Cloudflare Pages | free for public repos |
| GPU (choose per env `SCENEFORGE_GPU_WORKER`) | see below | no always-free GPU exists anywhere — adapters below |
| CI | GitHub Actions | free unlimited standard-runner minutes for public repos (2,000 min/mo if private) |

## GPU adapters — measured cost model

| Adapter | What it is | Cost (verified) | Use for |
| --- | --- | --- | --- |
| `local` | any CUDA box runs `arq` + pulls jobs | your electricity | dev, rented GPUs |
| `modal` | dispatcher on VM → Modal function (T4/A10G) | $30/mo recurring free credits; T4 $0.000164/s ≈ **$0.59/hr** → ~$0.10/scene at 10 min; A10G ≈ $1.10/hr | primary serverless path (~150–300 free scenes/mo within credits) |
| `hf_space` | dispatcher → gradio Space | creating ZeroGPU Spaces requires **PRO ($9/mo)** — not free; daily usage quotas small | demo bursts only, or skip to preserve $0 fixed cost |
| Kaggle/Colab | 30 GPU-hrs/wk (Kaggle, batch OK via "Save & Run All") / dynamic quotas (Colab, batch disallowed by FAQ) | free | experiments & splat-tuning only, never production |

Fill in measured wall-clock after first hardware runs (protocol in QUALITY.md):

| Preset | Stage timings on T4 (target) | Measured |
| --- | --- | --- |
| fast | ingest <1 min · SfM 2–4 min · splat 3–5 min · semantics <1 min | _pending_ |
| standard | total ≤ 10 min GPU | _pending_ |

## Deploy — control plane (OCI VM)

```bash
git clone <repo> && cd 3dlab/api
cp .env.example .env            # fill: POSTGRES_PASSWORD, R2 creds, webhook secret
docker compose -f docker/docker-compose.yml up -d          # api + db + valkey
docker compose -f docker/docker-compose.yml --profile dispatcher up -d  # if modal/hf_space
# mint keys
docker compose -f docker/docker-compose.yml exec api python -m sceneforge_api.keycli create --name worker --worker
docker compose -f docker/docker-compose.yml exec api python -m sceneforge_api.keycli create --name first-customer
```

Put the worker key into `.env` as `SCENEFORGE_WORKER_API_KEY` and restart the
dispatcher. Front with Caddy/nginx + TLS (not in compose; any ACME setup works).

## Deploy — GPU worker

```bash
# x86 CUDA host (never the ARM VM):
docker build -f api/docker/Dockerfile.worker -t sceneforge-worker .
docker run --gpus all --env-file api/.env \
  -e SCENEFORGE_GPU_WORKER=local sceneforge-worker
# or serverless:
pip install modal && modal deploy api/sceneforge_worker/modal_app.py
```

## Operations

- **Job stuck in `processing`**: worker crash is auto-reported (`worker_crash`);
  if the process died hard, arq's `job_timeout` (40 min) frees the slot — re-enqueue
  by POSTing `/uploaded` is blocked (state machine), use `keycli`-side SQL or requeue tooling (v1.1).
- **Scene failed `insufficient_registration`**: expected behavior — forward the
  quality report to the agent; see CAPTURE_GUIDE.md.
- **R2 nearing 10 GB**: delete `work/` uploads (video originals) after success;
  splats+SVGs are the durable assets.
- **Valkey down**: API still accepts uploads (state `awaiting_upload`/`queued`
  rows persist); enqueue happens on `/uploaded` — replay by re-POSTing after recovery. 
- **Model prefetch**: worker image bakes OWLv2 weights; first cold Modal call
  still pays image pull (~1 min).

## Security

- API keys: SHA-256 hashes at rest; plaintext shown once.
- Webhooks: HMAC-SHA256 signature header; rotate `SCENEFORGE_WEBHOOK_SECRET` per env.
- No secrets in repo — `.env` only (gitignored, `.env.example` documents keys).
- Local-storage dev endpoints are path-traversal-guarded and only mount when
  `SCENEFORGE_STORAGE=local`.
