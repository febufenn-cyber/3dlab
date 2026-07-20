"""Shared scene → API-response mapping.

Lives at module scope (not inside create_app) so both the HTTP routes and the
background reaper build identical payloads — one contract, two producers.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from .db import Scene
from .storage import Storage


class SceneStatusResponse(BaseModel):
    scene_id: str
    state: str
    quality_report: Optional[dict[str, Any]] = None
    assets: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None
    created_at: str
    updated_at: str


def status_response(scene: Scene, storage: Storage) -> SceneStatusResponse:
    assets = None
    if scene.assets:
        assets = {
            name: storage.public_url(key) if not str(key).startswith("http") else key
            for name, key in scene.assets.items()
        }
    return SceneStatusResponse(
        scene_id=scene.id,
        state=scene.state,
        quality_report=scene.quality_report,
        assets=assets,
        error_code=scene.error_code,
        created_at=scene.created_at.isoformat(),
        updated_at=scene.updated_at.isoformat(),
    )
