// Local, tool-free bridge. No candidate execution, evaluation, or promotion.
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

try {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 512 * 1024) throw new Error('input_limit');
    chunks.push(chunk);
  }
  const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const { project, baseline, evidence, model } = input;
  if (!project || typeof project.id !== 'string' || !project.id.trim()
      || !baseline || typeof baseline.strategy !== 'string' || !baseline.strategy.trim()
      || (baseline.projectId !== undefined && baseline.projectId !== project.id)
      || !Array.isArray(baseline.skills) || !Array.isArray(evidence) || !evidence.length
      || evidence.length > 200 || !model || typeof model !== 'object') throw new Error('invalid_input');
  const assigned = baseline.assignments?.skillIds;
  if (!Array.isArray(assigned) || new Set(assigned).size !== assigned.length
      || baseline.skills.length !== assigned.length
      || baseline.skills.some(skill => !skill || typeof skill.id !== 'string'
        || typeof skill.instructions !== 'string' || !assigned.includes(skill.id))
      || new Set(baseline.skills.map(skill => skill.id)).size !== baseline.skills.length) throw new Error('invalid_baseline');
  // Read the package manifest before import so an arbitrary path cannot be
  // mistaken for the configured RNA SDK package.
  const root = resolve(process.argv[2]);
  const manifest = JSON.parse(await readFile(resolve(root, 'package.json'), 'utf8'));
  if (manifest.name !== '@rna-agent/sdk') throw new Error('invalid_sdk');
  const { proposeEvolution } = await import(pathToFileURL(resolve(root, 'src/evolution.mjs')).href);
  const result = await proposeEvolution({ project, baseline, evidence, model,
    apiKey: process.env.RNA_ADAPTER_API_KEY, maxOutputTokens: 4096 });
  result.sdkVersion = manifest.version;
  const output = JSON.stringify(result);
  if (Buffer.byteLength(output, 'utf8') > 512 * 1024
      || output.includes(process.env.RNA_ADAPTER_API_KEY)) throw new Error('output_limit');
  // A provider's keep-alive handles must not keep this one-shot bridge alive.
  process.stdout.write(output, () => process.exit(0));
} catch {
  // Never reflect provider errors, prompts, endpoint credentials or raw stderr.
  process.stderr.write('RNA SDK proposal failed');
  process.exitCode = 1;
}
