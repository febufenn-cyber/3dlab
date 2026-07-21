/**
 * <rf-walkthrough> — the SceneForge embeddable viewer (brief §6 Phase 3).
 *
 *   <script src="https://your-cdn/rf.js"></script>
 *   <rf-walkthrough src="https://assets/scn_x/assets/scene.ksplat"
 *                   poster="https://assets/scn_x/assets/poster.png"
 *                   height="480px"></rf-walkthrough>
 *
 * Attributes:
 *   src        splat URL (.ksplat/.splat/.ply) — the public-embed path
 *   scene-id + api-base + api-key
 *              authenticated-dashboard path: resolves assets via the API.
 *              NEVER put a production api-key in a public page.
 *   poster     image shown before load (lazy load: splat bytes only move
 *              after the user taps, or when `autoload` is set)
 *   height     CSS height of the widget (default 420px)
 *   mode       "orbit" (default) or "walk" (WASD + arrows/drag)
 *   xr         "vr" or "ar" — immersive WebXR walkthrough. Capability is
 *              checked BEFORE the renderer is built: XR mode only turns on
 *              when navigator.xr reports the session type supported (GS3D
 *              0.4.7 disables its flat-screen orbit controls whenever
 *              webXRMode is set, so enabling it blind would break non-XR
 *              devices — verified against its source). On a supported
 *              device an Enter-VR/AR control appears in the stage after
 *              load, and the scene is re-based to Y-up (WebXR convention;
 *              our Z-up frame would render sideways in-headset otherwise).
 *              Unsupported devices keep the full flat walkthrough. Requires
 *              HTTPS. Hosts gate their chrome via the `rf-xr` event, which
 *              fires after load with { requested, supported }.
 *   load-timeout  seconds before an unresponsive load surfaces rf-error
 *                 instead of an infinite spinner (default 120)
 *
 * Design notes: sharedMemoryForWorkers is off so host pages don't need
 * COOP/COEP headers; Z-up matches the SceneForge scene frame.
 */

import * as GS3D from '@mkkellogg/gaussian-splats-3d';
import * as THREE from 'three';

const TEMPLATE = `
<style>
  :host { display: block; position: relative; width: 100%;
          background: #14161a; border-radius: 8px; overflow: hidden;
          font-family: Helvetica, Arial, sans-serif; }
  .stage, .poster { position: absolute; inset: 0; }
  .poster { background-size: cover; background-position: center;
            display: flex; align-items: center; justify-content: center;
            cursor: pointer; z-index: 2; }
  .poster button { background: rgba(0,0,0,.65); color: #fff; border: 1px solid #888;
                   border-radius: 24px; padding: 10px 22px; font-size: 15px;
                   cursor: pointer; }
  .poster button:hover { background: rgba(20,90,180,.8); }
  .badge { position: absolute; right: 8px; bottom: 6px; z-index: 3;
           font-size: 10px; color: rgba(255,255,255,.55); pointer-events: none; }
  .hint { position: absolute; left: 8px; bottom: 6px; z-index: 3; font-size: 11px;
          color: rgba(255,255,255,.75); background: rgba(0,0,0,.4);
          padding: 2px 8px; border-radius: 4px; pointer-events: none; }
  .err { position: absolute; inset: 0; display: flex; align-items: center;
         justify-content: center; color: #f88; font-size: 13px; z-index: 4;
         padding: 16px; text-align: center; }
</style>
<div class="stage"></div>
<div class="poster"><button part="play">▶&nbsp;&nbsp;Walk through in 3D</button></div>
<div class="hint"></div>
<div class="badge">SceneForge</div>
`;

const WALK_SPEED = 1.6;   // m/s
const TURN_SPEED = 1.6;   // rad/s

class RfWalkthrough extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = TEMPLATE;
    this._viewer = null;
    this._keys = new Set();
    this._raf = null;
    this._loaded = false;
  }

  static get observedAttributes() { return ['height', 'src']; }

  attributeChangedCallback(name, oldValue, newValue) {
    this.style.height = this.getAttribute('height') || '420px';
    // A changed src on an already-loaded element swaps the scene in place —
    // the supported path for dashboards; nobody should poke private state.
    if (name === 'src' && this._loaded && oldValue !== newValue && newValue) {
      this._reload();
    }
  }

  /** Tear down the current viewer and load the (possibly new) src. */
  _reload() {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    if (this._viewer) { try { this._viewer.dispose(); } catch (_) { /* gone */ } }
    this._viewer = null;
    this._loaded = false;
    const stale = this.shadowRoot.querySelector('.err');
    if (stale) stale.remove();
    this.load();
  }

  connectedCallback() {
    this.style.height = this.getAttribute('height') || '420px';
    const poster = this.shadowRoot.querySelector('.poster');
    const posterUrl = this.getAttribute('poster');
    if (posterUrl) poster.style.backgroundImage = `url("${posterUrl.replace(/"/g, '%22')}")`;
    poster.addEventListener('click', () => this.load());
    if (this.hasAttribute('autoload')) {
      // Still lazy: wait until the element is actually on screen.
      const io = new IntersectionObserver((entries) => {
        if (entries.some((e) => e.isIntersecting)) { io.disconnect(); this.load(); }
      });
      io.observe(this);
    }
  }

  disconnectedCallback() {
    if (this._raf) cancelAnimationFrame(this._raf);
    if (this._viewer) { try { this._viewer.dispose(); } catch (_) { /* already gone */ } }
    this._viewer = null;
  }

  async resolveSplatUrl() {
    const direct = this.getAttribute('src');
    if (direct) return direct;
    const sceneId = this.getAttribute('scene-id');
    const apiBase = (this.getAttribute('api-base') || '').replace(/\/$/, '');
    const apiKey = this.getAttribute('api-key');
    if (!sceneId || !apiBase) {
      throw new Error('rf-walkthrough needs either src="…" or scene-id + api-base');
    }
    const resp = await fetch(`${apiBase}/v1/scenes/${sceneId}`, {
      headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
    });
    if (!resp.ok) throw new Error(`scene lookup failed: HTTP ${resp.status}`);
    const scene = await resp.json();
    if (scene.state !== 'succeeded') {
      throw new Error(`scene is ${scene.state}${scene.error_code ? ` (${scene.error_code})` : ''}`);
    }
    const assets = scene.assets || {};
    const url = assets.scene || assets.splat;
    if (!url) throw new Error('scene has no splat asset');
    if (!this.getAttribute('poster') && assets.poster) {
      this.shadowRoot.querySelector('.poster').style.backgroundImage = `url("${assets.poster}")`;
    }
    return url;
  }

  async load() {
    if (this._loaded) return;
    this._loaded = true;
    const poster = this.shadowRoot.querySelector('.poster');
    const stage = this.shadowRoot.querySelector('.stage');
    const btn = poster ? poster.querySelector('button') : null;  // absent on reload
    if (btn) btn.textContent = 'Loading…';
    try {
      const url = await this.resolveSplatUrl();
      // XR is decided BEFORE the renderer exists: GS3D 0.4.7 skips its orbit
      // controls + pointer handlers entirely when webXRMode is set, so it is
      // only enabled when this device can actually start the session.
      const requested = this._xrRequested();
      const supported = requested ? await this._xrSupported(requested) : false;
      const xrMode = !supported ? GS3D.WebXRMode.None
        : requested === 'vr' ? GS3D.WebXRMode.VR : GS3D.WebXRMode.AR;
      // WebXR poses are Y-up; our scene frame is Z-up. In XR mode the scene
      // (and the flat-preview camera basis) is re-based to Y-up so the room
      // stands upright in the headset instead of lying on its side.
      const yUp = xrMode !== GS3D.WebXRMode.None;
      this._yUp = yUp;
      const zyx = (v) => (yUp ? [v[0], v[2], -v[1]] : v);
      if (this._viewer) { try { this._viewer.dispose(); } catch (_) { /* replaced */ } }
      this._viewer = new GS3D.Viewer({
        rootElement: stage,
        cameraUp: yUp ? [0, 1, 0] : [0, 0, 1],
        initialCameraPosition: zyx(this._attrVec('camera-position', [3, 3, 1.6])),
        initialCameraLookAt: zyx(this._attrVec('look-at', [0, 0, 1])),
        sharedMemoryForWorkers: false,   // host pages keep working without COOP/COEP
        antialiased: false,              // cheaper on mid-range mobile GPUs
        webXRMode: xrMode,
      });
      // Bounded load: a stalled fetch or broken GPU must surface rf-error,
      // never an infinite spinner.
      const timeoutS = Number(this.getAttribute('load-timeout')) > 0
        ? Number(this.getAttribute('load-timeout')) : 120;
      await Promise.race([
        this._viewer.addSplatScene(url, {
          progressiveLoad: true,
          showLoadingUI: true,
          // Z-up → Y-up: −90° about X, quaternion [x,y,z,w].
          rotation: yUp ? [-Math.SQRT1_2, 0, 0, Math.SQRT1_2] : [0, 0, 0, 1],
        }),
        new Promise((_, reject) => setTimeout(
          () => reject(new Error(`scene did not load within ${timeoutS}s`)),
          timeoutS * 1000
        )),
      ]);
      this._viewer.start();
      if (poster) poster.remove();
      this._setupModes();
      this.dispatchEvent(new CustomEvent('rf-loaded', { detail: { url } }));
      if (requested) {
        this.dispatchEvent(new CustomEvent('rf-xr', { detail: { requested, supported } }));
      }
    } catch (e) {
      // A half-built viewer must not keep downloading/rendering behind the
      // error overlay (leaked WebGL contexts add up fast on SPAs).
      if (this._viewer) { try { this._viewer.dispose(); } catch (_) { /* gone */ } }
      this._viewer = null;
      const err = document.createElement('div');
      err.className = 'err';
      err.setAttribute('role', 'alert');   // announce the failure, honestly
      err.textContent = `Could not load scene: ${e.message || e}`;
      this.shadowRoot.appendChild(err);
      if (poster) poster.remove();
      this.dispatchEvent(new CustomEvent('rf-error', { detail: { error: String(e) } }));
    }
  }

  _xrRequested() {
    const raw = (this.getAttribute('xr') || '').toLowerCase();
    return raw === 'vr' || raw === 'ar' ? raw : null;
  }

  async _xrSupported(requested) {
    if (!navigator.xr || !navigator.xr.isSessionSupported) return false;
    try {
      return await navigator.xr.isSessionSupported(
        requested === 'vr' ? 'immersive-vr' : 'immersive-ar');
    } catch (_) {
      return false;
    }
  }

  _attrVec(name, fallback) {
    const raw = this.getAttribute(name);
    if (!raw) return fallback;
    const v = raw.split(',').map(Number);
    return v.length === 3 && v.every((x) => Number.isFinite(x)) ? v : fallback;
  }

  _setupModes() {
    const hint = this.shadowRoot.querySelector('.hint');
    const walk = (this.getAttribute('mode') || 'orbit') === 'walk';
    hint.textContent = walk
      ? 'W/A/S/D move · arrows turn · drag to look'
      : 'drag to orbit · scroll/pinch to zoom';
    if (!walk) return;

    this.tabIndex = 0; // make the element focusable for key events
    if (!this.hasAttribute('aria-label')) {
      // The focus stop needs a name; the hint text doubles as the recipe.
      this.setAttribute(
        'aria-label', '3D scene walkthrough — W, A, S, D to move, arrow keys to turn'
      );
    }
    // Removing the poster (the previous focus target) drops keyboard focus to
    // <body>; hand it to the element so keys work immediately.
    if (this.shadowRoot.activeElement === null && document.activeElement === document.body) {
      this.focus({ preventScroll: true });
    }
    this.addEventListener('keydown', (e) => {
      if (['KeyW', 'KeyA', 'KeyS', 'KeyD', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']
          .includes(e.code)) {
        this._keys.add(e.code);
        e.preventDefault();
      }
    });
    this.addEventListener('keyup', (e) => this._keys.delete(e.code));
    this.addEventListener('mouseenter', () => this.focus({ preventScroll: true }));

    let last = performance.now();
    const step = (now) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      this._applyWalk(dt);
      this._raf = requestAnimationFrame(step);
    };
    this._raf = requestAnimationFrame(step);
  }

  _applyWalk(dt) {
    const v = this._viewer;
    if (!v || !v.camera || this._keys.size === 0) return;
    const cam = v.camera;
    const up = this._yUp
      ? new THREE.Vector3(0, 1, 0)     // XR-enabled: scene re-based to Y-up
      : new THREE.Vector3(0, 0, 1);    // SceneForge native Z-up
    const fwd = new THREE.Vector3();
    cam.getWorldDirection(fwd);
    fwd.addScaledVector(up, -fwd.dot(up));  // stay at eye height — it's a walkthrough
    if (fwd.lengthSq() < 1e-6) return;
    fwd.normalize();
    const right = new THREE.Vector3().crossVectors(fwd, up);
    const move = new THREE.Vector3();
    if (this._keys.has('KeyW') || this._keys.has('ArrowUp')) move.add(fwd);
    if (this._keys.has('KeyS') || this._keys.has('ArrowDown')) move.sub(fwd);
    if (this._keys.has('KeyD')) move.add(right);
    if (this._keys.has('KeyA')) move.sub(right);
    let yaw = 0;
    if (this._keys.has('ArrowLeft')) yaw += TURN_SPEED * dt;
    if (this._keys.has('ArrowRight')) yaw -= TURN_SPEED * dt;

    if (move.lengthSq() > 0) {
      move.normalize().multiplyScalar(WALK_SPEED * dt);
      cam.position.add(move);
      if (v.controls && v.controls.target) v.controls.target.add(move);
    }
    if (yaw !== 0 && v.controls && v.controls.target) {
      const t = v.controls.target.clone().sub(cam.position);
      const vertical = up.clone().multiplyScalar(t.dot(up));
      const planar = t.clone().sub(vertical);
      planar.applyAxisAngle(up, yaw);
      v.controls.target.copy(cam.position).add(planar).add(vertical);
    }
    if (v.controls) v.controls.update();
  }
}

if (!customElements.get('rf-walkthrough')) {
  customElements.define('rf-walkthrough', RfWalkthrough);
}

export { RfWalkthrough };
