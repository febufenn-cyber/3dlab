"""Modal deployment for the GPU pipeline (brief §5 `modal` adapter).

  modal deploy api/sceneforge_worker/modal_app.py

Builds the same environment as docker/Dockerfile.worker (COLMAP+GLOMAP+gsplat)
as a Modal image, and exposes `build_scene_job` which the ModalGPUWorker
dispatcher calls with a BuildJob dict. T4 by default (cheapest card that fits
the ≤10 min/scene budget); switch to "A10G" via SCENEFORGE_MODAL_GPU.

Cost model (verify against modal.com/pricing — see docs/RUNBOOK.md): with free
monthly credits and ~$1/hr-class cards at ≤10 min/scene, dozens of scenes/month
run at $0 marginal cost.
"""

from __future__ import annotations

import os

import modal

GPU_KIND = os.environ.get("SCENEFORGE_MODAL_GPU", "T4")

app = modal.App("sceneforge-worker")

# The worker image: reuse the Dockerfile so local and Modal builds stay identical.
image = modal.Image.from_dockerfile("api/docker/Dockerfile.worker")


@app.function(image=image, gpu=GPU_KIND, timeout=2400)
def build_scene_job(job_dict: dict) -> dict:
    """Runs inside the Modal GPU container."""
    from sceneforge_worker.jobspec import BuildJob, execute_build

    outcome = execute_build(BuildJob.from_dict(job_dict))
    return outcome.to_dict()
