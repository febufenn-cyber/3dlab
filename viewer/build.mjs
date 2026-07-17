/**
 * Build dist/rf.js — the single-file, one-script-tag bundle (IIFE, minified,
 * three + GaussianSplats3D inlined). dist/ is committed: it IS the deliverable
 * a third-party developer drops into their page (served via GitHub Pages /
 * Cloudflare Pages — see docs/QUICKSTART.md).
 */

import { build } from 'esbuild';
import { readFileSync } from 'node:fs';

const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url)));

const banner = `/*! SceneForge rf.js v${pkg.version}
 * Bundles: three (MIT, Copyright 2010-2026 three.js authors),
 * @mkkellogg/gaussian-splats-3d (MIT, Copyright 2023 Mark Kellogg).
 * SceneForge viewer code: MIT. See LICENSES.md in the repository. */`;

await build({
  entryPoints: ['src/rf-walkthrough.js'],
  bundle: true,
  minify: true,
  format: 'iife',
  target: ['es2020'],
  outfile: 'dist/rf.js',
  banner: { js: banner },
  logLevel: 'info',
});
