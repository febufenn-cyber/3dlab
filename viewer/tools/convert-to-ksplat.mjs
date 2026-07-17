/**
 * Convert a 3DGS .ply (or .splat) to compressed .ksplat using
 * GaussianSplats3D's loaders (MIT). Called by the pipeline's splat stage
 * (sceneforge_pipeline/stages/splat.py: compress_splat) and usable manually:
 *
 *   node tools/convert-to-ksplat.mjs in.ply out.ksplat [level=1] [alpha=1] [shDegree=2]
 *
 * level 1 = 16-bit quantization (~2x smaller than raw, good quality);
 * level 2 = additionally quantizes spherical harmonics.
 *
 * API verified against @mkkellogg/gaussian-splats-3d 0.4.7:
 *   PlyLoader.loadFromFileData(plyFileData, minimumAlpha, compressionLevel,
 *       optimizeSplatData, outSphericalHarmonicsDegree=0, ...) → Promise<SplatBuffer>
 *   SplatBuffer.bufferData is the complete .ksplat byte layout (same bytes
 *   KSplatLoader.downloadFile writes).
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

// The library assumes a browser: shim the window global before importing it
// (same approach as the upstream repo's node utilities).
if (typeof globalThis.window === 'undefined') {
  globalThis.window = globalThis;
}
const GaussianSplats3D = await import('@mkkellogg/gaussian-splats-3d');

const [, , inPath, outPath, levelArg, alphaArg, shArg] = process.argv;
if (!inPath || !outPath) {
  console.error('usage: node convert-to-ksplat.mjs <in.ply|in.splat> <out.ksplat> [level] [alpha] [shDegree]');
  process.exit(2);
}

const compressionLevel = Number.parseInt(levelArg ?? '1', 10);
const minimumAlpha = Number.parseInt(alphaArg ?? '1', 10);
const shDegree = Number.parseInt(shArg ?? '2', 10);

const raw = fs.readFileSync(inPath);
// Slice to an exact ArrayBuffer view (Buffer pools share memory).
const fileData = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);
const ext = path.extname(inPath).toLowerCase();

let pending;
if (ext === '.ply') {
  pending = GaussianSplats3D.PlyLoader.loadFromFileData(
    fileData, minimumAlpha, compressionLevel, false /* optimizeSplatData */, shDegree
  );
} else if (ext === '.splat') {
  pending = GaussianSplats3D.SplatLoader.loadFromFileData(
    fileData, minimumAlpha, compressionLevel, false
  );
} else {
  console.error(`unsupported input extension: ${ext}`);
  process.exit(2);
}

Promise.resolve(pending).then((splatBuffer) => {
  fs.writeFileSync(outPath, Buffer.from(splatBuffer.bufferData));
  const inMB = (raw.length / 1e6).toFixed(1);
  const outMB = (splatBuffer.bufferData.byteLength / 1e6).toFixed(1);
  console.log(`${inPath} (${inMB} MB) → ${outPath} (${outMB} MB)`);
}).catch((e) => {
  console.error('conversion failed:', e);
  process.exit(1);
});
