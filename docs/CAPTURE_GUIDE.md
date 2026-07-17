# Capture guide — how to record a walkthrough that works

SceneForge builds the 3D scene **only from what your camera saw**. It never
invents rooms, so a bad capture produces a rejection with a reason — not a
pretty lie. The pipeline enforces every rule below and tells you which one a
failed capture broke (`quality_report.json → failure.capture_rule`).

## The rules

| # | Rule | Why | Enforced as (`capture_rule`) |
| --- | --- | --- | --- |
| 1 | **60–120 seconds** of continuous video (hard floor: 20 s) | Shorter clips can't cover a room from enough angles | `duration_60_120s` |
| 2 | **1080p or better, landscape** | Feature matching needs pixels | resolution logged; low-res lowers registration → `slow_continuous_pan` |
| 3 | **Slow, continuous pan** — walk at a stroll, rotate like you're showing a friend around, never whip the phone | Motion blur and fast rotation kill SfM registration | `slow_pan_lights_on`, `slow_continuous_pan` |
| 4 | **Lights on**, curtains open | Low light = blur = dropped frames | `slow_pan_lights_on` |
| 5 | **Visit every corner** you want reconstructed; overlap your path | Unvisited areas are reported as uncovered, never filled in | coverage warnings per room |
| 6 | **Include at least one fully visible door** (top and bottom in frame) | Doors are the metric-scale prior (≈2.0 m) | low `scale.confidence` + warning |
| 7 | **Avoid mirror-dominated shots** and glass walls | Reflections create phantom geometry and break matching | shows up as poor registration |
| 8 | Keep the phone **at chest height, roughly level** | The camera path doubles as the fallback scale/gravity prior | degraded scale confidence |
| 9 | Video only — **photo sets are rejected** in v1 | Photos-only reconstruction is a non-goal for v1 | `duration_60_120s` |

## What you get back

- A **quality report** with: coverage %, per-room boundary coverage,
  frames registered, blur %, scale method + confidence, and explicit warnings
  such as `bedroom_2 partially covered (61% of boundary observed)`.
- Dimensions are estimates with a stated tolerance (±10 % typical). They are
  **not survey-grade** and must not be presented as such to buyers.

## If your capture is rejected

| Reason code | Fix |
| --- | --- |
| `video_too_short` | Record ≥ 60 s covering the whole space |
| `insufficient_sharp_frames` | More light, slower movement |
| `insufficient_registration` | Slow down rotation; add texture-rich framing (furniture, doorways); avoid mirrors filling the frame; keep the path continuous — don't "teleport" between rooms |
