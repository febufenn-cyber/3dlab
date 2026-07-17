# Quickstart — integrate SceneForge in 10 lines

You are a web developer at a real-estate portal. An agent uploads a phone
video; you show buyers a 3D walkthrough, a floor plan, and structured room
data. Here is the whole integration.

## 1. Get an API key

Your SceneForge operator mints it (no dashboard in v1):

```bash
python -m sceneforge_api.keycli create --name "acme-portal"
# → api_key: sk_...   (shown once)
```

## 2. Create a scene and upload the video (3 calls)

```bash
# 1) create → returns scene_id + a presigned upload URL
curl -s -X POST https://api.yourhost.com/v1/scenes \
  -H "Authorization: Bearer $SK" -H "Content-Type: application/json" \
  -d '{"filename":"flat.mp4","webhook_url":"https://acme.com/hooks/sceneforge"}'

# 2) upload the video bytes to upload_url from the response
curl -s -X PUT "$UPLOAD_URL" -H "Content-Type: video/mp4" --data-binary @flat.mp4

# 3) confirm → job is queued
curl -s -X POST https://api.yourhost.com/v1/scenes/$SCENE_ID/uploaded \
  -H "Authorization: Bearer $SK"
```

## 3. Wait for the webhook (or poll)

`GET /v1/scenes/{id}` → `state`: `queued → processing → succeeded | failed`.
On completion your `webhook_url` receives a signed POST
(`X-SceneForge-Signature: sha256=HMAC(secret, body)`):

```json
{"event": "scene.succeeded", "scene": {"scene_id": "scn_…", "state": "succeeded",
  "assets": {"scene": "https://assets…/scene.ksplat", "floorplan": "…/floorplan.svg",
             "poster": "…/poster.png", "semantic": "…/semantic.json"},
  "quality_report": {"quality": {"coverage_pct": 87, "warnings": []}}}}
```

A `failed` state is a feature, not a bug: the quality report says exactly which
capture rule broke (see CAPTURE_GUIDE.md) so the agent can re-shoot.

## 4. Embed the walkthrough — the 10 lines

```html
<!DOCTYPE html>
<html>
  <body>
    <script src="https://your-cdn.example.com/rf.js"></script>
    <rf-walkthrough
        src="https://assets.example.com/scn_abc/assets/scene.ksplat"
        poster="https://assets.example.com/scn_abc/assets/poster.png"
        mode="walk" height="480px">
    </rf-walkthrough>
  </body>
</html>
```

That's it. The poster shows instantly; splat bytes only transfer after the
buyer taps (add `autoload` to load on scroll instead). Orbit and walk modes,
touch controls included. Full attribute list: `viewer/README.md`.

## 5. Use the semantic JSON (the differentiator)

`GET /v1/scenes/{id}/semantic` returns machine-readable geometry —
rooms/areas, walls, doors/windows, object boxes, with honesty metadata:

```js
const scene = await (await fetch(`${API}/v1/scenes/${id}/semantic`,
    {headers: {Authorization: `Bearer ${SK}`}})).json();
const totalArea = scene.rooms.reduce((s, r) => s + r.area_m2, 0);
// scene.scale = {method: "door_prior", confidence: 0.8, tolerance_pct: 10}
// → always render dimensions as "≈54 m² (±10%)"
```

Validate against the frozen schema anytime: `rf-scene schema`.

## Notes

- **Never ship your `sk_` key to a browser.** Public embeds use asset URLs
  (`src=`/`poster=`); the `scene-id`+`api-key` attributes are for internal
  dashboards only.
- Asset URLs above assume a public R2/CDN base; with private buckets, proxy
  short-lived presigned URLs through your backend.
