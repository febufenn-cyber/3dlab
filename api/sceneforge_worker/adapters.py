"""GPUWorker adapters (brief §5): where the pipeline actually runs.

Selected via env `SCENEFORGE_GPU_WORKER=local|modal|hf_space`. All three take
the same BuildJob and return the same BuildOutcome; swapping providers never
touches the API. Cost/latency notes per adapter live in docs/RUNBOOK.md.
"""

from __future__ import annotations

import abc
import json
import logging
import os

from .jobspec import BuildJob, BuildOutcome, execute_build

log = logging.getLogger(__name__)


class GPUWorker(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    def build(self, job: BuildJob) -> BuildOutcome: ...


class LocalGPUWorker(GPUWorker):
    """Run the pipeline in this process — the arq worker itself sits on a CUDA
    box (dev default, or a rented GPU running docker/Dockerfile.worker)."""

    name = "local"

    def build(self, job: BuildJob) -> BuildOutcome:
        return execute_build(job)


class ModalGPUWorker(GPUWorker):
    """Call the deployed Modal function (see modal_app.py; `modal deploy`).

    The dispatcher (this class) runs on the CPU VM; Modal spins up the GPU
    container per job and bills seconds against the free monthly credits.
    """

    name = "modal"

    def __init__(self):
        self.app_name = os.environ.get("SCENEFORGE_MODAL_APP", "sceneforge-worker")
        self.function_name = os.environ.get("SCENEFORGE_MODAL_FUNCTION", "build_scene_job")

    def build(self, job: BuildJob) -> BuildOutcome:
        import modal  # dispatcher-side dependency: pip install modal

        fn = modal.Function.from_name(self.app_name, self.function_name)
        result: dict = fn.remote(job.to_dict())
        return BuildOutcome.from_dict(result)


class HFSpaceGPUWorker(GPUWorker):
    """Call a Hugging Face Space wrapping the pipeline via gradio_client.

    Demo/burst capacity only: ZeroGPU quotas are small and Space cold starts
    are slow (see docs/RUNBOOK.md). The Space runs hf_space_app.py.
    """

    name = "hf_space"

    def __init__(self):
        self.space = os.environ.get("SCENEFORGE_HF_SPACE", "")
        if not self.space:
            raise ValueError("SCENEFORGE_HF_SPACE (e.g. 'user/sceneforge-worker') is required")
        self.token = os.environ.get("HF_TOKEN") or None

    def build(self, job: BuildJob) -> BuildOutcome:
        from gradio_client import Client  # pip install gradio_client

        client = Client(self.space, token=self.token)
        raw: str = client.predict(json.dumps(job.to_dict()), api_name="/build")
        return BuildOutcome.from_dict(json.loads(raw))


_ADAPTERS: dict[str, type[GPUWorker]] = {
    LocalGPUWorker.name: LocalGPUWorker,
    ModalGPUWorker.name: ModalGPUWorker,
    HFSpaceGPUWorker.name: HFSpaceGPUWorker,
}


def make_worker(name: str | None = None) -> GPUWorker:
    name = name or os.environ.get("SCENEFORGE_GPU_WORKER", "local")
    try:
        return _ADAPTERS[name]()
    except KeyError:
        raise ValueError(f"Unknown GPU worker adapter {name!r}; known: {sorted(_ADAPTERS)}")
