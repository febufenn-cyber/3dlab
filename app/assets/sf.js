/* SceneForge app shell — shared by index.html (2D) and vision.html (XR).
 *
 * Everything is data-attribute driven so both pages reuse one script:
 *   [data-sf-tabs]          ARIA tablist for the scene panel
 *   [data-sf-insights]      populated from the sample semantic.json
 *   [data-sf-upload]        create-scene flow (real API when configured,
 *                           otherwise a clearly-labeled simulation)
 *   [data-sf-xr-status]     honest WebXR capability readout (vision page)
 *   [data-sf-viewer-status] live chip fed by rf-walkthrough events
 *
 * Honesty rules baked in: demo mode says SIMULATION on every step, the
 * synthetic sample is labeled synthetic wherever it appears, and XR chips
 * report real navigator.xr capability — nothing is faked as supported.
 */

const SAMPLE_BASE = '../demo/sample';

/* ---------- tiny helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
// EVERY value interpolated into note() HTML goes through this — including
// API responses (scene_id, error_code): a hostile "API" pasted into the
// connect form must not be able to inject markup.
const esc = (v) => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/* ---------- nav scrollspy ---------- */
function initScrollspy() {
  const links = $$('.sf-nav-link[href^="#"]');
  if (!links.length || !('IntersectionObserver' in window)) return;
  const bySection = new Map();
  links.forEach((a) => {
    const sec = document.getElementById(a.getAttribute('href').slice(1));
    if (sec) bySection.set(sec, a);
  });
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      links.forEach((a) => a.removeAttribute('aria-current'));
      const a = bySection.get(e.target);
      if (a) a.setAttribute('aria-current', 'true');
    });
  }, { rootMargin: '-35% 0px -55% 0px' });
  bySection.forEach((_, sec) => io.observe(sec));
}

/* ---------- scene panel tabs ---------- */
function initTabs() {
  $$('[data-sf-tabs]').forEach((tabs) => {
    const btns = $$('[role="tab"]', tabs);
    const panels = btns.map((b) => document.getElementById(b.getAttribute('aria-controls')));
    const select = (i) => {
      btns.forEach((b, j) => {
        b.setAttribute('aria-selected', String(i === j));
        b.tabIndex = i === j ? 0 : -1;
        if (panels[j]) panels[j].hidden = i !== j;
      });
      btns[i].focus({ preventScroll: true });
    };
    btns.forEach((b, i) => {
      b.addEventListener('click', () => select(i));
      b.addEventListener('keydown', (e) => {
        const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
        if (d) { e.preventDefault(); select((i + d + btns.length) % btns.length); }
      });
    });
  });
}

/* ---------- insights from semantic.json ---------- */
async function initInsights() {
  const host = $('[data-sf-insights]');
  if (!host) return;
  try {
    const doc = await (await fetch(`${SAMPLE_BASE}/semantic.json`)).json();
    const room = doc.rooms[0] || {};
    const doors = doc.openings.filter((o) => o.type === 'door');
    const stats = [
      [`${doc.rooms.length}`, 'room found'],
      [`${(room.area_m2 ?? 0).toFixed(1)} m²`, 'floor area'],
      [`${(room.ceiling_height_m ?? 0).toFixed(1)} m`, 'ceiling height'],
      [`${doors.length}`, doors.length === 1 ? 'door' : 'doors'],
      [`${doc.objects.length}`, 'objects'],
      [`±${doc.scale.tolerance_pct}%`, `scale · ${doc.scale.method.replace(/_/g, ' ')}`],
    ];
    const grid = el('div', 'sf-insights');
    stats.forEach(([v, k]) => {
      const s = el('div', 'sf-stat');
      s.append(el('div', 'v', v), el('div', 'k', k));
      grid.append(s);
    });
    host.append(grid);

    const objs = el('div', null);
    objs.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-top:14px';
    doc.objects.forEach((o) => {
      const c = el('span', 'sf-chip sf-chip--info');
      c.append(el('span', 'dot'),
        el('span', null, `${o.label} · ${(o.bbox.size[0]).toFixed(1)}×${(o.bbox.size[1]).toFixed(1)} m`));
      objs.append(c);
    });
    host.append(objs);

    // The honesty block is not optional UI — warnings always render.
    (doc.quality.warnings || []).forEach((w) => {
      const note = el('div', 'sf-note');
      note.style.marginTop = '14px';
      note.append(el('span', null, '⚠️'), el('span', null, w));
      host.append(note);
    });
  } catch (e) {
    host.append(el('p', 'sf-faint', `Could not load semantic.json (${e.message}). Serve this app over HTTP.`));
  }
}

/* ---------- embed snippet copy ---------- */
function initCopy() {
  $$('[data-sf-copy]').forEach((btn) => {
    // Announce the outcome to screen readers via a dedicated status region.
    const live = el('span', 'sf-vh');
    live.setAttribute('role', 'status');
    btn.after(live);
    btn.addEventListener('click', async () => {
      const src = document.getElementById(btn.getAttribute('data-sf-copy'));
      try {
        await navigator.clipboard.writeText(src.textContent.trim());
        const old = btn.textContent;
        btn.textContent = 'Copied ✓';
        live.textContent = 'Snippet copied to clipboard';
        setTimeout(() => { btn.textContent = old; live.textContent = ''; }, 1600);
      } catch {
        btn.textContent = 'Copy failed — select manually';
        live.textContent = 'Copy failed — select the snippet manually';
      }
    });
  });
}

/* ---------- viewer status chip ---------- */
function initViewerStatus() {
  const chip = $('[data-sf-viewer-status]');
  const viewer = $('rf-walkthrough');
  if (!chip || !viewer) return;
  const set = (cls, text) => { chip.className = `sf-chip ${cls}`;
    chip.replaceChildren(el('span', 'dot'), el('span', null, text)); };
  viewer.addEventListener('rf-loaded', () => set('sf-chip--ok', 'Scene loaded · rendering live'));
  viewer.addEventListener('rf-error', (e) => set('sf-chip--err', `Load failed: ${e.detail.error}`));
}

/* ---------- honest WebXR capability readout ---------- */
function initXr() {
  const host = $('[data-sf-xr-status]');
  if (!host) return;
  const report = (label, supported, why) => {
    const c = el('span', `sf-chip ${supported ? 'sf-chip--ok' : 'sf-chip--warn'}`);
    c.append(el('span', 'dot'), el('span', null,
      `${label}: ${supported ? 'available on this device' : why}`));
    host.append(c);
  };
  if (!('xr' in navigator)) {
    // navigator.xr is SecureContext-only: over plain HTTP the honest diagnosis
    // is "needs HTTPS", not "your browser can't" — say the right one.
    const why = window.isSecureContext
      ? 'WebXR not exposed by this browser'
      : 'needs HTTPS (WebXR is secure-context only)';
    report('Immersive VR', false, why);
    report('Passthrough AR', false, why);
    return;
  }
  navigator.xr.isSessionSupported('immersive-vr')
    .then((ok) => report('Immersive VR', ok, 'not supported here'))
    .catch(() => report('Immersive VR', false, 'not supported here'));
  navigator.xr.isSessionSupported('immersive-ar')
    .then((ok) => report('Passthrough AR', ok, 'not supported here'))
    .catch(() => report('Passthrough AR', false, 'not supported here'));
}

/* ---------- create-scene flow ---------- */
// API states are awaiting_upload/queued/processing/succeeded/failed; the four
// middle rows are pipeline STAGES inside `processing` (labeled as such — the
// API does not report per-stage progress yet; that's on the roadmap).
const STEPS = [
  ['awaiting_upload', 'Upload', 'presigned PUT straight to storage'],
  ['queued', 'Queued', 'waiting for a GPU worker'],
  ['geometry', 'Geometry', 'pipeline stage — camera poses + world points'],
  ['splat', 'Splat training', 'pipeline stage — gsplat MCMC → compact .ksplat'],
  ['semantics', 'Understanding', 'pipeline stage — rooms · doors · objects · scale'],
  ['floorplan', 'Floor plan', 'pipeline stage — auto SVG with stated tolerance'],
  ['succeeded', 'Ready', 'walkthrough + JSON + plan delivered'],
];
const PROCESSING_STAGES = ['geometry', 'splat', 'semantics', 'floorplan'];

function getConfig() {
  if (window.SF_CONFIG?.apiBase) return window.SF_CONFIG;
  try { return JSON.parse(localStorage.getItem('sf.config') || 'null'); }
  catch { return null; }
}

function initUpload() {
  const root = $('[data-sf-upload]');
  if (!root) return;
  const drop = $('.sf-drop', root);
  const input = $('input[type=file]', root);
  const stepsHost = $('.sf-steps', root);
  const noteHost = $('[data-sf-upload-note]', root);
  const failBtn = $('[data-sf-fail-demo]', root);

  // Render the step list once. State is conveyed three ways — class (color),
  // indicator glyph (✓/✕/number), and a visually-hidden text label — so it
  // survives colorblindness, reduced-motion, and screen readers alike.
  const stepEls = new Map();
  stepsHost.setAttribute('role', 'list');
  STEPS.forEach(([key, lbl, sub], i) => {
    const s = el('div', 'sf-step');
    s.setAttribute('role', 'listitem');
    const ind = el('div', 'ind', String(i + 1));
    ind.setAttribute('aria-hidden', 'true');
    const state = el('span', 'sf-vh', 'not started');
    const txt = el('div');
    txt.append(el('div', 'lbl', lbl), el('div', 'sub', sub));
    s.append(ind, txt, state);
    stepsHost.append(s);
    stepEls.set(key, { root: s, ind, state, n: i + 1 });
  });

  const note = (cls, html) => {
    noteHost.replaceChildren();
    const n = el('div', `sf-note ${cls}`);
    // Failures interrupt politely-queued output; everything else is polite.
    n.setAttribute('role', cls === 'err' ? 'alert' : 'status');
    n.append(el('span', null, cls === 'ok' ? '✅' : cls === 'err' ? '⛔' : 'ℹ️'));
    const body = el('span');
    body.innerHTML = html;   // app-authored markup; interpolations pre-escaped via esc()
    n.append(body);
    noteHost.append(n);
  };

  const setStep = (entry, cls) => {
    entry.root.classList.remove('active', 'done', 'failed');
    if (cls) entry.root.classList.add(cls);
    entry.root.removeAttribute('aria-current');
    if (cls === 'active') entry.root.setAttribute('aria-current', 'step');
    entry.ind.textContent = cls === 'done' ? '✓' : cls === 'failed' ? '✕' : String(entry.n);
    entry.state.textContent =
      cls === 'done' ? 'completed' : cls === 'active' ? 'in progress'
      : cls === 'failed' ? 'failed' : 'not started';
  };

  const setProgress = (activeKey, { failedAt, band } = {}) => {
    let reached = true;
    STEPS.forEach(([key]) => {
      const entry = stepEls.get(key);
      if (failedAt && key === failedAt) { setStep(entry, 'failed'); reached = false; }
      else if (band && band.includes(key)) { setStep(entry, 'active'); reached = false; }
      else if (key === activeKey) { setStep(entry, 'active'); reached = false; }
      else setStep(entry, reached ? 'done' : null);
    });
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function runSimulation(file, { fail = false } = {}) {
    note('', `<b>SIMULATION</b> — no backend is connected, so nothing was uploaded and no
      reconstruction ran. This walks the API states a real scene passes
      (<code>awaiting_upload → queued → processing → succeeded/failed</code>); the four
      middle rows are pipeline stages <i>inside</i> <code>processing</code>, simulated here
      because the API reports the single processing state, not per-stage progress (roadmap).
      Connect an API below to run it for real.`);
    const seq = ['awaiting_upload', 'queued', 'geometry', 'splat', 'semantics', 'floorplan'];
    for (const key of seq) {
      setProgress(key);
      await sleep(key === 'queued' ? 700 : 1050);
      if (fail && key === 'geometry') {
        setProgress(null, { failedAt: 'geometry' });
        note('err', `<b>Honest failure (simulated):</b> <code>failed / capture_rule</code> — too little
          camera motion to reconstruct. SceneForge never invents geometry it didn't see:
          you get the reason code + re-shoot guidance from the capture guide, and a
          <code>requeue</code> once you've re-uploaded.`);
        return;
      }
    }
    setProgress('succeeded');
    setStep(stepEls.get('succeeded'), 'done');
    note('ok', `<b>Simulation complete.</b> The viewer above shows the bundled <i>synthetic</i>
      sample scene — labeled synthetic because it is procedurally generated, not a capture
      ${file ? `of <code>${esc(file.name)}</code>` : ''}. A connected API returns your real scene here.`);
  }

  async function runReal(file, cfg) {
    const headers = { Authorization: `Bearer ${cfg.apiKey}` };
    try {
      setProgress('awaiting_upload');
      note('', `Creating scene at <code>${esc(cfg.apiBase)}</code>…`);
      const created = await fetch(`${cfg.apiBase}/v1/scenes`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: file.name, content_type: file.type || 'video/mp4' }),
      });
      if (!created.ok) throw new Error(`create: HTTP ${created.status} ${await created.text()}`);
      const { scene_id, upload_url } = await created.json();

      const put = await fetch(upload_url, { method: 'PUT', body: file,
        headers: { 'Content-Type': file.type || 'video/mp4' } });
      if (!put.ok) throw new Error(`upload: HTTP ${put.status}`);

      const confirmed = await fetch(`${cfg.apiBase}/v1/scenes/${encodeURIComponent(scene_id)}/uploaded`,
        { method: 'POST', headers });
      if (!confirmed.ok) throw new Error(`confirm: HTTP ${confirmed.status}`);
      setProgress('queued');
      note('', `Scene <code>${esc(scene_id)}</code> queued. Polling status…`);

      for (;;) {
        await sleep(5000);
        const r = await fetch(`${cfg.apiBase}/v1/scenes/${encodeURIComponent(scene_id)}`, { headers });
        if (!r.ok) throw new Error(`status: HTTP ${r.status}`);
        const s = await r.json();
        if (s.state === 'processing') {
          // Honest: the API reports one processing state; the four stage rows
          // pulse as a band rather than pretending per-stage knowledge.
          setProgress(null, { band: PROCESSING_STAGES });
        } else if (s.state === 'queued') setProgress('queued');
        else if (s.state === 'succeeded') {
          setProgress('succeeded');
          setStep(stepEls.get('succeeded'), 'done');
          const url = s.assets?.scene;
          note('ok', `<b>Scene ready.</b> ${url ? 'Loading it into the viewer above.' : ''}`);
          const viewer = $('rf-walkthrough');
          if (url && viewer) {
            if (s.assets?.poster) viewer.setAttribute('poster', s.assets.poster);
            // The component observes `src` and swaps the scene in place —
            // its supported reload path, no private state involved.
            viewer.setAttribute('src', url);
            if (!viewer._loaded) viewer.load();
          }
          return;
        } else if (s.state === 'failed') {
          setProgress(null, { failedAt: 'splat' });
          note('err', `<b>Honest failure:</b> <code>failed / ${esc(s.error_code || 'unknown')}</code>.
            See the capture guide, then <code>POST /v1/scenes/${esc(scene_id)}/requeue</code> after re-upload.`);
          return;
        }
      }
    } catch (e) {
      setProgress(null, { failedAt: 'awaiting_upload' });
      note('err', `<b>Request failed:</b> ${esc(e.message || e)}.
        Check the API base URL, key, and CORS configuration.`);
    }
  }

  let busy = false;   // one flow at a time — a second drop mid-run is ignored
  const start = async (file, opts) => {
    if (busy) return;
    busy = true;
    try {
      const cfg = getConfig();
      if (cfg?.apiBase && cfg?.apiKey && file) await runReal(file, cfg);
      else await runSimulation(file, opts);
    } finally {
      busy = false;
      input.value = '';   // same file can be dropped again
    }
  };

  drop.addEventListener('click', () => input.click());
  drop.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
  });
  input.addEventListener('change', () => start(input.files[0]));
  ['dragover', 'dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => {
    e.preventDefault();
    drop.classList.toggle('drag', t === 'dragover');
    if (t === 'drop' && e.dataTransfer.files[0]) start(e.dataTransfer.files[0]);
  }));
  if (failBtn) failBtn.addEventListener('click', () => start(null, { fail: true }));

  // Optional API connection form (keys stay in this browser's localStorage).
  const form = $('[data-sf-config-form]', root);
  if (form) {
    const cfg = getConfig();
    if (cfg?.apiBase) { form.apiBase.value = cfg.apiBase; form.apiKey.value = cfg.apiKey || ''; }
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const apiBase = form.apiBase.value.trim().replace(/\/$/, '');
      const apiKey = form.apiKey.value.trim();
      try {
        if (!apiBase) {
          localStorage.removeItem('sf.config');
          note('', 'API disconnected — demo simulation mode.');
          return;
        }
        localStorage.setItem('sf.config', JSON.stringify({ apiBase, apiKey }));
        note('ok', `API connected: <code>${esc(apiBase)}</code>. Drop a video to build a real scene.`);
      } catch (err) {
        // Private browsing / blocked storage: say so instead of a dead form.
        note('err', `Could not save the connection (browser storage is blocked:
          ${esc(err.message || err)}). The API config would not survive this page — fix
          storage permissions or use a normal window.`);
      }
    });
  }
}

/* ---------- boot ---------- */
initScrollspy();
initTabs();
initInsights();
initCopy();
initViewerStatus();
initXr();
initUpload();
document.documentElement.classList.add('sf-ready');
