# TECH RADAR — faster generation, smaller scenes, better viewing (mid-2026)

A 7-lens web-research sweep (feed-forward geometry, splat training, formats,
browser rendering, GPU capacity, capture UX, visionOS design) synthesized into
ranked recommendations, 2026-07-21. Every claim traces to an evidence URL the
researcher actually fetched; anything resting only on search snippets says so.
Maturity labels are honest — research code is never called production-ready.

**License context that frames everything:** the mid-2026 accuracy leaders are
ALL non-commercial (Pi3/Pi3X weights CC-BY-NC, Depth Anything 3 CC-BY-NC,
MASt3R lineage CC-BY-NC-SA, HunyuanWorld-Mirror capped community license,
VGGT-1B-Commercial gated). SceneForge's permissive roster — lingbot-map,
MapAnything-**apache**, AnySplat — is essentially the complete legal frontier.

## Adopt now

| # | Recommendation | Payoff | Key risk |
| --- | --- | --- | --- |
| 2 | **Pin gsplat main-branch SHA + enable its camera-opt** (joint pose optimization) so splat training absorbs residual lingbot pose error; calibrate the honest-failure gate on post-alignment reprojection error. Picks up AccuTile, native CUDA MCMC noise, fp16 SH (~30% faster on A100 path — benchmark the T4 delta). | Free wall-clock cut on the exact loop we ship + the strongest evidenced fix for feed-forward-initialized 3DGS quality. | Untagged code — pin an exact SHA, gate on a regression benchmark. Don't vendor VGGT-X (unclear license); use gsplat's own Apache camera-opt. |
| 3 | **Productize honest-failure**: ~10 s CPU preflight (blur + angular-diversity) that blocks doomed videos pre-GPU; `failure_reason` enum with one reshoot tip each; staged progress ("Solving geometry → Training splat → …"); early draft splat at ~min 3–4. | Turns the no-hallucination constraint into the differentiator; saves free-tier GPU minutes; matches Scaniverse/Polycam/Luma convergent UX. | Preflight thresholds need real-capture tuning; drafts must be labeled drafts. |
| 4 | **GPU strategy: Modal primary** (recurring $30/mo credit ≈ 150–250 T4 scenes/mo at $0), **demote hf_space to demo-only** (ZeroGPU's 60–120 s call cap structurally can't run 5–20 min jobs), **Cloud Run Jobs L4 as first paid adapter** (~$0.30–0.40/scene, scale-to-zero). | Preserves $0 fixed cost with the only recurring free GPU credit found. | Reconfirm live pricing pages before contracts (proxy-blocked; multi-source corroborated). Kaggle's 30 T4-h/wk is non-commercial ToS — dev/CI only. |
| 5 | **License CI gate**: allowlist Apache/MIT/BSD, seeded with the verified traps above; pin the MapAnything **-apache** checkpoint hash so a silent NC swap is impossible. | Makes it impossible for a well-meaning "upgrade" PR to breach the hard constraint. | HF card metadata lags repos — needs a human-curated mapping. |
| 6 | **Embed hardening + Vision Pro guardrails**: poster/click-to-load stays default (a WebGL context on page load gets embeds banned from listing pages); Matterport-style URL attributes; `/embed` route without app chrome. XR: request `immersive-vr` only (Safari visionOS has no `immersive-ar` through 26.2), transient-pointer input, controls as real DOM elements (canvas-drawn buttons get no gaze highlight), warn standalone headsets about splat perf. | Attribute plumbing, not engine work — makes embeds safe for the core distribution channel and XR feel native. | Safari specifics from Apple release notes/WWDC (webkit.org proxy-blocked) — re-verify before advertising. |

## v1.1

| # | Recommendation | Payoff | Key risk |
| --- | --- | --- | --- |
| 1 | **Migrate the viewer to Spark** (World Labs, MIT, active releases): isolate rf.js's renderer glue behind an interface, swap GaussianSplats3D 0.4.7 (self-declared "no longer in active development"). three.js 0.170 → ~0.180. .ksplat stays loadable. | Escapes the abandoned dependency (biggest structural risk); fused worker sorting, fixed-budget LOD (~500 K mobile / 1.5 M desktop), WebXR intact; unlocks SOG/SPZ. | Real migration effort; Spark LOD self-describes WIP; WebGL2-only by design. |
| 7 | **Reimplement training-speed patterns in gsplat** (never vendor the NC trainers): EDGS-style dense init from lingbot's pointmap (~30 k steps → ~5–8 k), DashGaussian-style resolution+primitive ramp (−45.7% reported), Taming-style deterministic splat budget from the 25 MB target. | If EDGS's 4× holds indoors: 10–15 min T4 training → ~3–6 min. The realistic path to comfortable ≤20 min headroom. | Research patterns, not drop-in code; EDGS validated forward-facing — must re-validate on inward indoor video; gate behind the quality benchmark. |
| 8 | **MapAnything-apache as the fallback** (replacing COLMAP as tier 2; COLMAP stays last resort) + a metric-scale calibration pass on keyframes (it outputs METRIC geometry; lingbot is up-to-scale; can take EXIF intrinsics priors). | Feed-forward fallback in seconds + honest metric dimensions — a direct product claim. | No published latency table — profile on T4 (`minibatch_size=1`); license gate must pin the -apache hash. |
| 9 | **SOG delivery format** via PlayCanvas splat-transform (MIT), after Spark: ~2× smaller than .ksplat at equal quality (~10.5 B/splat → typical room ~5–16 MB); `.spz` export for interop (Khronos glTF extension candidate). | Single biggest first-load-time win available. .ksplat is ecosystem-orphaned. | Hard dependency on Spark migration; verify real-scene sizes before changing the size SLA. |
| 10 | **Matterport viewer grammar**: floor-plan minimap overlay with click-a-room camera animation (rooms already exist in scene JSON), dollhouse orbit mode, 2–3 s fly-in intro. | Highest perceived-quality-per-effort found — reads like Matterport using assets the pipeline already makes. | Do on/after Spark to avoid building choreography on the abandoned renderer. |

## Later / Watch

- **AnySplat (MIT) instant-preview tier** — feasibility spike first: published
  numbers are A100-40GB; T4 fit unproven. If it fits: a strictly-labeled
  preview seconds after upload + free pose cross-check for the honesty gate.
  Raw PSNR ~18–22 is below product quality — preview only. *(later)*
- **Quarterly watchlist** — Meta re-license events (a permissive VGGT-Omega or
  DA3 would be a same-week adoption; note VGGT-Omega needs 21–35 GB at 200–360
  frames — no T4 fit regardless of license); Khronos `KHR_gaussian_splatting`
  ratification ("download as glTF" = one CLI flag); WebGPU splat renderers
  (WebGPU is default-on everywhere incl. Safari 26, but no production three.js
  splat renderer uses it yet — nothing to adopt); ETH ReSplat/YoNoSplat
  16×-fewer-gaussians line vs the 25 MB budget. *(watch)*

## visionOS design tokens (applied in `viewer/app/assets/sf.css`)

The design-research lens returned concrete CSS values (Apple HIG/WWDC-derived;
the fluid-design-io/vision-ui reference repo has NO license — numbers
reimplemented, code never vendored): glass = `saturate(1.5) blur(24–96px)
brightness(0.85)` (the brightness clamp is what separates visionOS glass from
generic glassmorphism), fill `rgba(255,255,255,0.07)`, ring `0.10`, vibrancy
tiers `0.90/0.70/0.45` (tertiary never for interactive labels), body at weight
**500**, 44 px controls with 8 px padding → 60 px effective gaze targets,
`prefers-reduced-transparency` solid fallback. All reflected in the shipped
design system.
