// Use the pinned Harness parser, including !!js and relative plugin module names.
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';

export function checkPatches(sources) {
  const ids = new Map();
  const claims = new Map();
  const rows = new Map();
  const appends = new Map();
  const conflict = (first, second, contribution) => {
    throw new Error(`Plugins ${first} and ${second} conflict on Cordis ${contribution}.`);
  };
  const claim = (key, value, plugin) => {
    const owners = claims.get(key) ?? new Map();
    for (const [owner, previous] of owners) {
      if (owner !== plugin && !isDeepStrictEqual(previous, value)) conflict(owner, plugin, key);
    }
    owners.set(plugin, value);
    claims.set(key, owners);
  };
  const append = (id, plugin) => {
    for (const owner of (claims.get(`${id}.config`) ?? new Map()).keys()) {
      if (owner !== plugin) conflict(owner, plugin, `${id}.config replacement versus group insert`);
    }
    const owners = appends.get(id) ?? new Set();
    owners.add(plugin);
    appends.set(id, owners);
  };
  const insert = (entries, plugin) => {
    for (const entry of entries) {
      if (entry.id) {
        const owner = ids.get(entry.id);
        if (owner && owner !== plugin) conflict(owner, plugin, `inserted id ${entry.id}`);
        ids.set(entry.id, plugin);
        for (const [key, value] of Object.entries(entry)) {
          // Group child lists append in order, rather than replacing one another.
          if (key !== 'id' && !(key === 'config' && entry.group)) claim(`${entry.id}.${key}`, value, plugin);
        }
      }
      rows.set(entry.id || Symbol(), { ...entry, plugin });
      if (entry.group && Array.isArray(entry.config)) {
        if (entry.id) append(entry.id, plugin);
        insert(entry.config, plugin);
      }
    }
  };
  for (const { plugin, patches } of sources) {
    for (const patch of patches) {
      if (Array.isArray(patch.insert)) {
        if (patch.id) append(patch.id, plugin);
        insert(patch.insert, plugin);
      } else if (patch.id) {
        // Upstream replaces whole entry fields, including config; it does not deep-merge them.
        for (const [key, value] of Object.entries(patch)) {
          if (key === 'config') {
            for (const owner of appends.get(patch.id) ?? []) {
              if (owner !== plugin) conflict(owner, plugin, `${patch.id}.config replacement versus group insert`);
            }
          }
          if (key !== 'id') claim(`${patch.id}.${key}`, value, plugin);
        }
        const row = rows.get(patch.id);
        if (row) {
          for (const [key, value] of Object.entries(patch)) {
            if (key !== 'id' && key !== 'name') row[key] = value;
          }
        }
      }
    }
  }
  const servers = new Map();
  for (const { plugin, name, config } of rows.values()) {
    if (name !== '@deepseek-ai/dsh-mcp-client' || typeof config?.serverName !== 'string') continue;
    const owner = servers.get(config.serverName);
    if (owner && owner !== plugin) conflict(owner, plugin, `MCP serverName ${config.serverName}`);
    servers.set(config.serverName, plugin);
  }
}

function runtimes() {
  if (process.env.DSH_PLUGIN_RUNTIMES) return JSON.parse(process.env.DSH_PLUGIN_RUNTIMES);
  // Preserve direct Compose use of the original convention.
  return existsSync('/opt/dsh-plugin') ? [{ plugin: '/opt/dsh-plugin', runtime: '/opt/dsh-plugin' }] : [];
}

async function check(selected) {
  if (!selected.some(({ runtime }) => existsSync(join(runtime, 'cordis.yml')))) return;
  const require = createRequire('/usr/local/lib/node_modules/@deepseek-ai/dsh/package.json');
  const { loadOverlayPatches } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-app-boot')));
  const sources = selected.map(({ plugin, runtime }) => {
    const file = join(runtime, 'cordis.yml');
    return { plugin, patches: existsSync(file) ? loadOverlayPatches(`Plugin ${plugin}`, file) : [] };
  });
  checkPatches(sources);
}

async function main(mode) {
  const selected = runtimes();
  if (mode === 'paths') {
    for (const { runtime } of selected) console.log(runtime);
    return;
  }
  if (mode === 'check' || mode === 'validate') await check(selected);
  if (mode === 'bootstrap' || mode === 'validate') {
    for (const { plugin, runtime } of selected) {
      const script = join(runtime, mode === 'bootstrap' ? 'bootstrap.sh' : 'validate.sh');
      if (!existsSync(script)) continue;
      const result = spawnSync('/bin/sh', [script], {
        stdio: 'inherit', env: { ...process.env, DSH_PLUGIN_RUNTIME: runtime },
      });
      if (result.error || result.status !== 0) {
        throw new Error(`Plugin ${plugin}: ${mode} hook failed (${result.error ?? result.signal ?? result.status}).`);
      }
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv[2]).catch(error => { console.error(error.message); process.exitCode = 1; });
}
