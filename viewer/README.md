# SceneForge viewer — `<rf-walkthrough>`

The embeddable web component third-party developers drop into a page:

```html
<script src="https://your-cdn/rf.js"></script>
<rf-walkthrough src="https://assets.example.com/scn_abc/assets/scene.ksplat"
                poster="https://assets.example.com/scn_abc/assets/poster.png"
                mode="walk" height="480px"></rf-walkthrough>
```

Full integration guide: [docs/QUICKSTART.md](../docs/QUICKSTART.md).

## Layout

| Path | Purpose |
| --- | --- |
| `src/rf-walkthrough.js` | the web component (Three.js + GaussianSplats3D, both MIT) |
| `build.mjs` | esbuild → `dist/rf.js` (single-file IIFE bundle, committed) |
| `dist/rf.js` | **the deliverable** — serve from GitHub/Cloudflare Pages |
| `tools/convert-to-ksplat.mjs` | `.ply`/`.splat` → compressed `.ksplat` (used by the pipeline) |
| `tools/make-sample-scene.py` / `make-sample-semantics.py` | regenerate the labeled-synthetic demo sample (splat + poster + semantic.json + floorplan.svg) |
| `demo/index.html` | minimal one-scene embed demo |
| `app/` | the product UI, one design system ("Vision glass"), two experiences: `index.html` (2D devices) and `vision.html` (spatial/XR — immersive WebXR features live only here). Static, no framework; deployed as the GitHub Pages front door. |

## Develop

```bash
npm install
npm run build           # rebuild dist/rf.js
python -m http.server   # from viewer/ → open /demo/
```

## Attributes

| Attribute | Meaning |
| --- | --- |
| `src` | splat URL (`.ksplat`, `.splat`, `.ply`) — public-embed path |
| `poster` | image shown before load; splat bytes move only after tap (or `autoload`) |
| `autoload` | start loading when scrolled into view instead of on tap |
| `mode` | `orbit` (default) or `walk` (WASD + arrows) |
| `xr` | `vr` or `ar` — immersive WebXR walkthrough. An Enter-VR/AR control appears inside the stage after load; needs HTTPS + a WebXR browser (Quest; Safari on visionOS where immersive WebXR is enabled). Non-XR devices keep the flat walkthrough and the control honestly reads NOT SUPPORTED. |
| `height` | CSS height, default `420px` |
| `camera-position` / `look-at` | `x,y,z` overrides (Z-up, meters) |
| `scene-id` + `api-base` (+ `api-key`) | authenticated dashboards: resolve assets via the API. Never ship a production key to a public page. |

Events: `rf-loaded`, `rf-error`, `rf-xr` (`{ requested, supported }` — fires
after load when `xr` is set, so hosts can gate immersive chrome honestly).
