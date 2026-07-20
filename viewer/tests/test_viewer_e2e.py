"""Real-browser E2E for the <rf-walkthrough> component.

Loads the actual demo page (committed rf.js bundle + the synthetic sample
scene) in headless Chromium via Playwright, clicks play, and asserts the
component reaches its `rf-loaded` state with a rendering canvas — i.e. the
entire chain works in a real browser: custom-element upgrade → poster →
splat fetch → .ksplat parse → GaussianSplats3D viewer boot → WebGL render.

Requires: `pip install playwright` + a Chromium (preinstalled in this repo's
dev/CI environments; locally `playwright install chromium`). Skips — loudly —
when the environment cannot create a WebGL context at all, because then the
component's failure is the environment's, not the code's.

Run:  python3 -m pytest viewer/tests/test_viewer_e2e.py -q
"""

from __future__ import annotations

import http.server
import threading
from functools import partial
from pathlib import Path

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
)

VIEWER_DIR = Path(__file__).resolve().parents[1]

# Attach outcome listeners WITHOUT returning the promise (page.evaluate
# auto-awaits returned promises, which would block before load() is called —
# a lesson this test learned the hard way). The promise resolves with the
# first event, or 'timeout' after 90s so the test always reports, never hangs.
ARM_OUTCOME_JS = """
() => {
  const el = document.querySelector('rf-walkthrough');
  window.__sfOutcome = new Promise((resolve) => {
    el.addEventListener('rf-loaded', () => resolve('rf-loaded'), {once: true});
    el.addEventListener('rf-error', (e) => resolve('rf-error: ' + e.detail.error), {once: true});
    setTimeout(() => resolve('timeout: neither rf-loaded nor rf-error fired in 90s'), 90000);
  });
  return true;
}
"""


@pytest.fixture(scope="module")
def viewer_server():
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(VIEWER_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _chromium_executable() -> str | None:
    """Explicit Chromium override for environments that preinstall a browser
    build not matching the pip playwright revision (e.g. /opt/pw-browsers).
    None → let Playwright resolve its own managed browser (CI installs it)."""
    import os

    for candidate in (
        os.environ.get("SCENEFORGE_CHROMIUM"),
        "/opt/pw-browsers/chromium",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


@pytest.fixture(scope="module")
def page(viewer_server):
    with playwright_api.sync_playwright() as p:
        exe = _chromium_executable()
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        page = browser.new_page(viewport={"width": 1024, "height": 768})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{viewer_server}/demo/", wait_until="networkidle")
        page._sf_errors = errors  # stashed for assertions
        yield page
        browser.close()


def _webgl_available(page) -> bool:
    return page.evaluate(
        "() => { const c = document.createElement('canvas');"
        " return !!(c.getContext('webgl2') || c.getContext('webgl')); }"
    )


def test_component_registers_and_shows_poster(page):
    assert page.evaluate("() => !!customElements.get('rf-walkthrough')"), (
        "rf.js did not define the custom element"
    )
    el = page.locator("rf-walkthrough")
    assert el.count() == 1
    # Poster overlay with the play button is visible before any splat bytes move.
    button = page.locator("rf-walkthrough >> internal:role=button")
    assert button.count() >= 1
    assert page._sf_errors == [], f"page errors during load: {page._sf_errors}"


def test_full_load_chain_renders_scene(page):
    if not _webgl_available(page):
        pytest.skip("no WebGL in this environment — component chain untestable here")

    page.evaluate(ARM_OUTCOME_JS)
    # Tap the poster — this is the lazy-load gate.
    page.evaluate("() => { document.querySelector('rf-walkthrough').load(); return true; }")
    outcome = page.evaluate("() => window.__sfOutcome")  # awaits the promise
    assert outcome == "rf-loaded", f"viewer failed to load the sample scene: {outcome}"

    # A real rendering surface exists inside the component.
    has_canvas = page.evaluate(
        "() => !!document.querySelector('rf-walkthrough')"
        ".shadowRoot.querySelector('.stage canvas, .stage div canvas')"
        " || !!document.querySelector('rf-walkthrough').shadowRoot"
        ".querySelector('.stage').getElementsByTagName('canvas').length"
    )
    assert has_canvas, "rf-loaded fired but no canvas is attached to the stage"


def test_walk_mode_hint_present_after_load(page):
    if not _webgl_available(page):
        pytest.skip("no WebGL in this environment")
    hint = page.evaluate(
        "() => document.querySelector('rf-walkthrough')"
        ".shadowRoot.querySelector('.hint').textContent"
    )
    assert "W/A/S/D" in hint  # demo page sets mode="walk"
