#!/usr/bin/env node
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolve, join } from 'node:path';
import { parseArgs } from 'node:util';

const root = fileURLToPath(new URL('../../', import.meta.url));
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
export async function verify(directory) {
  const manifest = JSON.parse(await readFile(join(directory, 'manifest.json'), 'utf8'));
  assert.equal(manifest.schema_version, 1);
  assert.match(manifest.captured_commit, /^[a-f0-9]{40}$/);
  assert.equal(manifest.mode, 'scripted');
  assert.equal(manifest.agent_profile, 'mock');
  assert.equal(manifest.auditor_profile, 'mock');
  assert.equal(manifest.reasoning_provenance, 'SYNTHETIC_FIXTURE');
  assert.equal(manifest.provider_calls, 0);
  assert.equal(manifest.assets.length, 4);
  assert.equal(new Set(manifest.assets.map(asset => asset.file)).size, 4);
  for (const asset of manifest.assets) {
    assert.match(asset.file, /^(scripted-[a-z-]+|architecture)\.png$/);
    assert.match(asset.caption, asset.file === 'architecture.png' ? /^ARCHITECTURE:/ : /^SCRIPTED MOCK:/);
    assert.ok(asset.alt.length > 30);
    const png = await readFile(join(directory, asset.file));
    assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a', 'PNG signature');
    assert.equal(hash(png), asset.sha256, `Image hash changed: ${asset.file}`);
    assert.equal(png.readUInt32BE(16), asset.width);
    assert.equal(png.readUInt32BE(20), asset.height);
    assert.equal(asset.width, manifest.viewport.width);
    assert.ok(asset.height >= manifest.viewport.height);
  }
  assert.equal(hash(await readFile(join(root, 'scripts/media/capture.mjs'))), manifest.capture_script_sha256, 'Capture script changed; regenerate assets.');
  assert.equal(hash(await readFile(join(directory, 'architecture.html'))), manifest.architecture_source_sha256, 'Architecture source changed; regenerate render.');
  const metadata = JSON.stringify(manifest);
  assert.doesNotMatch(metadata, /\/home\/|\/Users\/|sk-or-|ghp_|Bearer\s|X-Demo-Token/i);
  return manifest;
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const { values } = parseArgs({ options: { output: { type: 'string', default: 'docs/submission/media' } } });
  verify(resolve(root, values.output)).then(manifest => {
    console.log(`Verified ${manifest.assets.length} PNGs, dimensions, hashes, scripted captions, and source provenance.`);
  }).catch(() => { console.error('Media verification failed. Check manifest, PNG hashes/dimensions, captions, and source hashes.'); process.exitCode = 1; });
}
