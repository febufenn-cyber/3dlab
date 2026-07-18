"""Self-contained viewer.html for CLI output (Phase 1 deliverable).

Loads Three.js and GaussianSplats3D (both permissively licensed; see
LICENSES.md) from jsDelivr with pinned versions, and the splat file from a
relative path — so `rf-scene build` output can be opened from any static file
server (`python -m http.server`) with no build step. The production embed path
is the viewer/ web component; this file is for instant local inspection.
Versions are pinned in one place below and must match viewer/package.json.
"""

from __future__ import annotations

import json
from pathlib import Path

THREE_VERSION = "0.170.0"        # keep in sync with viewer/package.json
GS3D_VERSION = "0.4.7"           # @mkkellogg/gaussian-splats-3d

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SceneForge — {scene_id}</title>
<style>
  html, body {{ margin: 0; height: 100%; background: #111; color: #eee;
                font-family: Helvetica, Arial, sans-serif; }}
  #viewer {{ width: 100%; height: 100%; }}
  #hud {{ position: fixed; left: 12px; bottom: 12px; font-size: 12px;
          background: rgba(0,0,0,.55); padding: 8px 10px; border-radius: 6px; }}
  #hud b {{ color: #8fd0ff; }}
</style>
</head>
<body>
<div id="viewer"></div>
<div id="hud">
  <b>{scene_id}</b> · drag to orbit · scroll to zoom · <b>W/A/S/D</b> + arrows to move<br>
  coverage {coverage_pct}% · scale: {scale_method} (conf {scale_confidence}) ·
  dimensions ±{tolerance_pct}% — not survey-grade
</div>
<script type="importmap">
{{
  "imports": {{
    "three": "https://cdn.jsdelivr.net/npm/three@{three_version}/build/three.module.js",
    "@mkkellogg/gaussian-splats-3d": "https://cdn.jsdelivr.net/npm/@mkkellogg/gaussian-splats-3d@{gs3d_version}/build/gaussian-splats-3d.module.min.js"
  }}
}}
</script>
<script type="module">
import * as GS3D from '@mkkellogg/gaussian-splats-3d';

const viewer = new GS3D.Viewer({{
  rootElement: document.getElementById('viewer'),
  cameraUp: [0, 0, 1],
  initialCameraPosition: {initial_pos},
  initialCameraLookAt: {look_at},
  sharedMemoryForWorkers: false
}});
viewer.addSplatScene('{splat_file}', {{ showLoadingUI: true, progressiveLoad: true }})
  .then(() => viewer.start())
  .catch((e) => {{
    document.getElementById('hud').innerHTML =
      'Failed to load splat: ' + e + '<br>Serve this folder over HTTP, e.g. <code>python -m http.server</code>.';
  }});
</script>
</body>
</html>
"""


def write_viewer_html(
    out_path: Path,
    scene_id: str,
    splat_filename: str,
    quality: dict,
    scale: dict,
    initial_pos: list[float] | None = None,
    look_at: list[float] | None = None,
) -> Path:
    html = _TEMPLATE.format(
        scene_id=scene_id,
        splat_file=splat_filename,
        coverage_pct=quality.get("coverage_pct", "?"),
        scale_method=scale.get("method", "none"),
        scale_confidence=scale.get("confidence", 0.0),
        tolerance_pct=scale.get("tolerance_pct", 10),
        three_version=THREE_VERSION,
        gs3d_version=GS3D_VERSION,
        initial_pos=json.dumps([round(v, 2) for v in (initial_pos or [3.0, 3.0, 1.5])]),
        look_at=json.dumps([round(v, 2) for v in (look_at or [0.0, 0.0, 1.0])]),
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
