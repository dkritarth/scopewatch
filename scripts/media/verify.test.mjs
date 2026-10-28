import test from 'node:test';
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
