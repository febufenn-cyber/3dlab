# Deploying SceneForge on the free 1 GB Oracle E2 instance

Copy-paste guide for running the whole control plane on an OCI Always-Free
`VM.Standard.E2.1.Micro` — 1 GB RAM, 1/8 AMD OCPU (burstable), x86_64,
50 Mbps external bandwidth, **$0/month forever**, and every tenancy gets two
of them ([verified specs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm),
2026-07). The same setup runs unchanged on the bigger Ampere A1 shape — same
$0, more concurrent users; E2→A1 is a capacity dial, not an architecture
change.

## Why 1 GB is enough

The instance is a pure control plane:

- **No heavy bytes**: videos upload browser→R2 and assets download R2→browser
  via presigned URLs. The 50 Mbps NIC only ever carries JSON.
- **No heavy compute**: GPU stages run on Modal free credits / a local CUDA
  box / (roadmap) the agent's own browser via WebGPU. The API box never
  imports numpy or opencv.
- **Micro profile swaps**: SQLite instead of Postgres, Valkey capped at 48 MB,
  one uvicorn worker, per-container memory limits.

Budget (steady state): OS ≈ 200 MB · api **67 MB measured** (uvicorn + app,
warm, SQLite/local-storage smoke — budget 160 for S3/boto3 headroom) ·
dispatcher ≈ 140 MB · valkey ≈ 30 MB · cloudflared ≈ 40 MB →
**≈ 480–570 MB used, ≥ 400 MB headroom**, plus a 2 GB swapfile as OOM
insurance. The smoke behind the measured figure also exercised the full
state machine (create → presigned upload → queued → worker result →
succeeded) against the SQLite profile, including key minting against the
live server — the `docker compose exec … keycli` flow.

## 1. Create the instance

Console → Compute → Create instance:

- Shape: `VM.Standard.E2.1.Micro` (Always Free eligible)
- Image: Ubuntu 24.04 Minimal (x86_64 — no ARM caveats on this shape)
- Boot volume: default (counts against the free 200 GB total)
- Networking: default VCN. If you'll use the Cloudflare Tunnel profile
  (recommended) you need **no inbound ports at all** except SSH. Otherwise
  open 80/443 in the subnet security list AND in the instance firewall.

## 2. Swapfile first (do not skip on 1 GB)

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo sysctl vm.swappiness=10   # swap is insurance, not working memory
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-sceneforge.conf
```

## 3. Docker + repo

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
git clone https://github.com/febufenn-cyber/3dlab.git && cd 3dlab/api
cp .env.example .env
```

Edit `.env`: fill the R2 credentials, `SCENEFORGE_WEBHOOK_SECRET`, and (for
serverless GPU) the Modal tokens. `POSTGRES_PASSWORD` is **not needed** — the
micro profile uses SQLite.

## 4. Launch

```bash
docker compose -f docker/docker-compose.micro.yml up -d                      # api + queue
docker compose -f docker/docker-compose.micro.yml --profile dispatcher up -d # + Modal dispatch
# mint keys (worker key goes back into .env as SCENEFORGE_WORKER_API_KEY):
docker compose -f docker/docker-compose.micro.yml exec api \
  python -m sceneforge_api.keycli create --name worker --worker
docker compose -f docker/docker-compose.micro.yml exec api \
  python -m sceneforge_api.keycli create --name first-customer
```

## 5. TLS / public hostname (pick one)

**A — Cloudflare Tunnel (recommended, free, zero open ports):** create a
tunnel in the Cloudflare Zero Trust dashboard, point it at
`http://api:8000`, put the token in `.env` as `CLOUDFLARE_TUNNEL_TOKEN`, then:

```bash
docker compose -f docker/docker-compose.micro.yml --profile tunnel up -d
```

**B — Caddy on the host:** `sudo apt install caddy` and a two-line
`Caddyfile` (`api.yourdomain.com { reverse_proxy localhost:8000 }`); Caddy
handles Let's Encrypt automatically. Adds ~40 MB.

## 6. Verify

```bash
curl -s localhost:8000/healthz          # {"ok":true,...}
free -h                                  # expect ~400 MB available
docker stats --no-stream                 # each container within its mem_limit
```

Then run the API happy path from your laptop per docs/QUICKSTART.md.

## Limits of the micro profile — and the A1 dial

| Symptom | Meaning | Fix |
| --- | --- | --- |
| SQLite `database is locked` under load | write concurrency ceiling (~dozens of simultaneous mutations) | move to the A1 shape + standard `docker-compose.yml` (Postgres). Migrate: `sqlite3 /data/sceneforge.db .dump` → adapt inserts, or replay scenes — the schema is created automatically |
| api container OOM-killed | traffic outgrew 320 MB | raise `mem_limit` (A1 has 12 GB) or add a second uvicorn worker there |
| Queue full errors from Valkey | >48 MB of pending jobs (thousands) | raise `--maxmemory` on A1 |

Everything else (R2, Modal, Pages, the viewer) is identical across shapes.
The second free E2 instance makes a good staging clone of this exact setup.
