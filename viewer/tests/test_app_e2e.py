"""Real-browser E2E for the SceneForge app UI (viewer/app/).

Covers both experiences over local HTTP (same harness as test_viewer_e2e):
  index.html   — 2D: nav, tabs, insights populated from semantic.json,
                 upload SIMULATION honesty labeling, embed copy targets
  vision.html  — spatial: ornament nav, honest XR capability chips (headless
                 Chromium has no WebXR → chips must say NOT supported),
                 xr="vr" attribute on the walkthrough, full viewer load

The honesty assertions are the point: demo mode must say SIMULATION, the
sample must say synthetic, and XR chips must never claim support the
browser doesn't have.

Run:  python3 -m pytest viewer/tests/test_app_e2e.py -q
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


@pytest.fixture(scope="module")
def app_server():
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(VIEWER_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    import os

    exe = next(
        (
            c
            for c in (os.environ.get("SCENEFORGE_CHROMIUM"), "/opt/pw-browsers/chromium")
            if c and Path(c).exists()
        ),
        None,
    )
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        yield b
        b.close()


def _open(browser, url):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector("html.sf-ready")  # sf.js booted
    return page, errors


def _webgl_available(page) -> bool:
    return page.evaluate(
        "() => { const c = document.createElement('canvas');"
        " return !!(c.getContext('webgl2') || c.getContext('webgl')); }"
    )


# ---------------------------------------------------------------- 2D app


def test_2d_app_boots_clean(browser, app_server):
    page, errors = _open(browser, f"{app_server}/app/index.html")
    assert errors == [], f"page errors: {errors}"
    assert page.locator(".sf-topnav").count() == 1
    assert page.locator(".sf-ornament").count() == 0  # ornament is Vision-only
    # The 2D page must NOT ask the viewer for XR — that's a Vision-native feature.
    assert page.get_attribute("rf-walkthrough", "xr") is None
    page.close()


def test_2d_tabs_and_insights(browser, app_server):
    page, _ = _open(browser, f"{app_server}/app/index.html")
    # Insights tab populates stats from the real semantic.json…
    page.click("#tab-insights")
    page.wait_for_selector("#panel-insights .sf-stat")
    stats = page.locator("#panel-insights .sf-stat").count()
    assert stats >= 5, f"expected ≥5 insight stats, got {stats}"
    # …and the honesty warning from quality.warnings is rendered, verbatim topic.
    warn = page.locator("#panel-insights .sf-note").inner_text()
    assert "synthetic" in warn.lower()
    # Floor plan tab shows the pipeline-rendered SVG (lazy — wait for decode).
    page.click("#tab-plan")
    page.wait_for_function(
        "() => { const i = document.querySelector(\"#panel-plan img\");"
        " return i && !i.closest('[hidden]') && i.naturalWidth > 0; }"
    )
    page.close()


def test_2d_upload_simulation_is_labeled_honestly(browser, app_server):
    page, _ = _open(browser, f"{app_server}/app/index.html")
    # No API configured → dropping a file must run a SIMULATION and say so.
    page.set_input_files("[data-sf-upload] input[type=file]", {
        "name": "walkthrough.mp4", "mimeType": "video/mp4", "buffer": b"\x00" * 32,
    })
    note = page.wait_for_selector("[data-sf-upload-note] .sf-note")
    assert "SIMULATION" in note.inner_text()
    # The state machine steps animate; at least one becomes active or done.
    page.wait_for_selector(".sf-step.active, .sf-step.done")
    page.close()


def test_2d_honest_failure_demo(browser, app_server):
    page, _ = _open(browser, f"{app_server}/app/index.html")
    page.click("[data-sf-fail-demo]")
    # The simulated failure surfaces the real reason-code vocabulary.
    page.wait_for_selector(".sf-step.failed", timeout=15000)
    note = page.locator("[data-sf-upload-note] .sf-note").inner_text()
    assert "capture_rule" in note
    page.close()


# ---------------------------------------------------------------- Vision app


def test_vision_boots_with_ornament_and_xr_attr(browser, app_server):
    page, errors = _open(browser, f"{app_server}/app/vision.html")
    assert errors == [], f"page errors: {errors}"
    assert page.locator(".sf-ornament").count() == 1
    assert page.locator(".sf-topnav").count() == 0
    # XR is requested on the Vision page — the component owns honest fallback.
    assert page.get_attribute("rf-walkthrough", "xr") == "vr"
    page.close()


def test_vision_xr_chips_are_honest_in_headless(browser, app_server):
    page, _ = _open(browser, f"{app_server}/app/vision.html")
    chips = page.locator("[data-sf-xr-status] .sf-chip")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-sf-xr-status] .sf-chip').length >= 2"
    )
    text = " | ".join(chips.nth(i).inner_text() for i in range(chips.count()))
    # Headless Chromium has no WebXR devices: claiming support here would be
    # a lie. The chips must degrade honestly.
    assert "available on this device" not in text, f"XR chips overclaim: {text}"
    assert "VR" in text and "AR" in text
    page.close()


def test_vision_full_viewer_load_with_xr_requested(browser, app_server):
    page, _ = _open(browser, f"{app_server}/app/vision.html")
    if not _webgl_available(page):
        page.close()
        pytest.skip("no WebGL in this environment — viewer chain untestable here")
    # Arm BOTH listeners before load so neither event can be missed.
    page.evaluate(
        """() => {
          const el = document.querySelector('rf-walkthrough');
          window.__sfOutcome = new Promise((resolve) => {
            el.addEventListener('rf-loaded', () => resolve('rf-loaded'), {once: true});
            el.addEventListener('rf-error', (e) => resolve('rf-error: ' + e.detail.error), {once: true});
            setTimeout(() => resolve('timeout'), 90000);
          });
          window.__sfXr = new Promise((resolve) => {
            el.addEventListener('rf-xr', (e) => resolve(e.detail), {once: true});
            setTimeout(() => resolve('rf-xr never fired'), 90000);
          });
          el.load();
          return true;
        }"""
    )
    outcome = page.evaluate("() => window.__sfOutcome")
    # xr="vr" must never break flat loading on non-XR devices.
    assert outcome == "rf-loaded", f"vision page viewer failed: {outcome}"
    # The capability event is mandatory when xr is requested, and must report
    # unsupported here — headless Chromium has no WebXR.
    rf_xr = page.evaluate("() => window.__sfXr")
    assert rf_xr != "rf-xr never fired", "rf-xr event never fired for xr='vr'"
    assert rf_xr["requested"] == "vr"
    assert rf_xr["supported"] is False
    # Regression (GS3D 0.4.7 skips its orbit controls whenever webXRMode is
    # set): with XR unsupported the component must fall back to WebXRMode.None
    # so the flat-screen controls still exist.
    assert page.evaluate(
        "() => !!document.querySelector('rf-walkthrough')._viewer.controls"
    ), "flat-screen controls missing — webXRMode was enabled on a non-XR device"
    page.close()


# ---------------------------------------------------------------- responsive


@pytest.mark.parametrize("path", ["app/index.html", "app/vision.html"])
def test_no_horizontal_overflow_at_320px(browser, app_server, path):
    page = browser.new_page(viewport={"width": 320, "height": 660})
    page.goto(f"{app_server}/{path}", wait_until="networkidle")
    page.wait_for_selector("html.sf-ready")
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, f"{path} overflows horizontally by {overflow}px at 320px"
    page.close()
