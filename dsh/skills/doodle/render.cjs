#!/usr/bin/env node
'use strict';
// Offline, DOM-free Rough.js SVG renderer. Requires a locally deployed
// vendor/rough.cjs.js or an installed roughjs@4.6.6 package.
const fs = require('node:fs');
const path = require('node:path');

function number(value, name, min, max) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) {
    throw new Error(`${name} must be a finite number in [${min}, ${max}]`);
  }
  return value;
}
function escaped(value) {
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;');
}
function color(value, name) {
  if (typeof value !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(value)) {
    throw new Error(`${name} must be a six-digit hex color`);
  }
  return value;
}
function loadRough() {
  let lib;
  const vendor = path.join(__dirname, 'vendor', 'rough.cjs.js');
  if (fs.existsSync(vendor)) lib = require(vendor);
  else {
    try { lib = require('roughjs/bundled/rough.cjs.js'); }
    catch { throw new Error('Rough.js is missing; provision vendor/rough.cjs.js (version 4.6.6)'); }
  }
  const rough = lib.generator ? lib : lib.default;
  if (!rough || typeof rough.generator !== 'function') throw new Error('Invalid Rough.js bundle');
  return rough;
}
function render(scene, rough = loadRough()) {
  if (!scene || typeof scene !== 'object' || Array.isArray(scene)) throw new Error('scene must be an object');
  const width = number(scene.width ?? 512, 'width', 64, 2048);
  const height = number(scene.height ?? 512, 'height', 64, 2048);
  const seed = number(scene.seed ?? 1, 'seed', 1, 2147483647);
  const roughness = number(scene.roughness ?? 1.1, 'roughness', 0, 3);
  const bowing = number(scene.bowing ?? 0.7, 'bowing', 0, 3);
  const strokeWidth = number(scene.strokeWidth ?? 2.6, 'strokeWidth', 0.5, 10);
  const ink = color(scene.ink ?? '#242424', 'ink');
  const paper = color(scene.paper ?? '#faf8f2', 'paper');
  if (!Array.isArray(scene.paths) || !scene.paths.length || scene.paths.length > 150) {
    throw new Error('paths must contain 1–150 path entries');
  }
  if (scene.labels !== undefined && (!Array.isArray(scene.labels) || scene.labels.length > 40)) {
    throw new Error('labels must contain at most 40 entries');
  }
  const generator = rough.generator({ options: { disableMultiStroke: true, disableMultiStrokeFill: true } });
  const lines = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`,
    `<rect width="100%" height="100%" fill="${paper}"/>`
  ];
  for (const [i, item] of scene.paths.entries()) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error(`paths[${i}] must be an object`);
    const d = item.d;
    if (typeof d !== 'string' || d.length < 3 || d.length > 8000 ||
        !/^[Mm]/.test(d) || !/^[0-9eE.,+\-\sMmLlHhVvCcSsQqTtAaZz]+$/.test(d)) {
      throw new Error(`paths[${i}].d is not an allowed SVG path`);
    }
    const opts = {
      stroke: color(item.ink ?? ink, `paths[${i}].ink`),
      strokeWidth: number(item.strokeWidth ?? strokeWidth, `paths[${i}].strokeWidth`, 0.5, 10),
      roughness: number(item.roughness ?? roughness, `paths[${i}].roughness`, 0, 3),
      bowing: number(item.bowing ?? bowing, `paths[${i}].bowing`, 0, 3),
      maxRandomnessOffset: number(item.maxRandomnessOffset ?? 2, `paths[${i}].maxRandomnessOffset`, 0, 5),
      disableMultiStroke: true,
      disableMultiStrokeFill: true,
      seed: ((seed + i * 7919) % 2147483646) + 1,
      fill: undefined,
    };
    const result = generator.toPaths(generator.path(d, opts));
    if (!Array.isArray(result) || result.length > 12) throw new Error(`unexpected Rough.js output for paths[${i}]`);
    for (const info of result) {
      if (typeof info.d !== 'string' || !/^[0-9eE.,+\-\sMmLlHhVvCcSsQqTtAaZz]+$/.test(info.d)) {
        throw new Error(`invalid output path from Rough.js at paths[${i}]`);
      }
      lines.push(`<path d="${escaped(info.d)}" fill="none" stroke="${opts.stroke}" stroke-width="${opts.strokeWidth}" stroke-linecap="round" stroke-linejoin="round"/>`);
    }
  }
  for (const [i, label] of (scene.labels ?? []).entries()) {
    if (!label || typeof label !== 'object' || typeof label.text !== 'string' || label.text.length > 80) {
      throw new Error(`labels[${i}] has invalid text`);
    }
    const x = number(label.x, `labels[${i}].x`, 0, width);
    const y = number(label.y, `labels[${i}].y`, 0, height);
    const size = number(label.size ?? 18, `labels[${i}].size`, 8, 60);
    lines.push(`<text x="${x}" y="${y}" fill="${ink}" font-size="${size}" font-family="sans-serif">${escaped(label.text)}</text>`);
  }
  lines.push('</svg>');
  return lines.join('\n') + '\n';
}
function main(argv) {
  if (argv.length !== 2) throw new Error('Usage: node render.cjs scene.json output.svg');
  const [input, output] = argv.map(p => path.resolve(p));
  if (path.extname(output).toLowerCase() !== '.svg') throw new Error('output must have an .svg extension');
  const inputStat = fs.statSync(input);
  if (!inputStat.isFile() || inputStat.size > 150000) throw new Error('input JSON must be a file <= 150 KB');
  const svg = render(JSON.parse(fs.readFileSync(input, 'utf8')));
  fs.writeFileSync(output, svg, { flag: 'wx' }); // Never clobber an existing drawing.
  process.stdout.write(`${output}\n`);
}
if (require.main === module) {
  try { main(process.argv.slice(2)); }
  catch (err) { process.stderr.write(`doodle: ${err.message}\n`); process.exitCode = 1; }
}
module.exports = { render, main };
