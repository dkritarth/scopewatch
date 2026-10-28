import test from 'node:test';
import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { cp, mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { verify } from './verify.mjs';

const source = fileURLToPath(new URL('../../docs/submission/media/', import.meta.url));
test('committed captures verify; altered image, source, provenance, and unsafe filename fail', async () => {
  await verify(source);
  const scratch = await mkdtemp(join(tmpdir(), 'scopewatch-media-test-'));
  try {
    await cp(source, scratch, { recursive: true });
    const original = await readFile(join(scratch, 'manifest.json'), 'utf8');
    const manifest = JSON.parse(original);
    for (const mutate of [
      m => { m.assets[0].sha256 = '0'.repeat(64); },
      m => { m.reasoning_provenance = 'PROVIDER_EXPOSED_TRACE'; },
      m => { m.assets[0].caption = 'Live sponsor inference'; },
      m => { m.assets[0].file = '../outside.png'; },
      m => { m.architecture_source_sha256 = '0'.repeat(64); },
      m => { m.assets[0].width = 1; },
    ]) {
      const changed = structuredClone(manifest);
      mutate(changed);
      await writeFile(join(scratch, 'manifest.json'), JSON.stringify(changed));
      await assert.rejects(verify(scratch));
    }
    await writeFile(join(scratch, 'manifest.json'), original);
    await writeFile(join(scratch, manifest.assets[0].file), 'not a PNG');
    await assert.rejects(verify(scratch));
  } finally { await rm(scratch, { recursive: true, force: true }); }
});

test('invalid capture ref refuses before writing artifacts and emits no stack or host path', async () => {
  const before = await readFile(join(source, 'manifest.json'), 'utf8');
  const capture = fileURLToPath(new URL('./capture.mjs', import.meta.url));
  const result = spawnSync(process.execPath, [capture, '--ref', '0000000000000000000000000000000000000000'], { encoding: 'utf8' });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Capture failed during runtime provenance/);
  assert.doesNotMatch(result.stderr, /\/home\/|\/Users\/|at file:/);
  assert.equal(await readFile(join(source, 'manifest.json'), 'utf8'), before);
});
