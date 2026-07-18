"""Hugging Face Space wrapper (brief §5 `hf_space` adapter) — demo/burst only.

Deploy: create a Space from the worker Docker image (or a GPU Space with the
same deps), add this file as app.py. The HFSpaceGPUWorker dispatcher calls the
/build endpoint with a BuildJob JSON string via gradio_client.

NOTE (verified in LICENSES.md/RUNBOOK.md): creating ZeroGPU Spaces requires a
PRO account and free-tier callers have small daily quotas — treat this path as
demo capacity, never production.
"""

from __future__ import annotations

import json

import gradio as gr

from sceneforge_worker.jobspec import BuildJob, execute_build


def build(job_json: str) -> str:
    job = BuildJob.from_dict(json.loads(job_json))
    outcome = execute_build(job)
    return json.dumps(outcome.to_dict())


demo = gr.Interface(
    fn=build,
    inputs=gr.Textbox(label="BuildJob JSON"),
    outputs=gr.Textbox(label="BuildOutcome JSON"),
    title="SceneForge worker",
    api_name="build",
)

if __name__ == "__main__":
    demo.launch()
