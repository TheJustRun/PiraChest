// PiraChest <-> Vyla SDK bridge.
//
// Long-lived line-delimited JSON process. The Python side owns discovery,
// provisioning and caching; this process only loads the vendored SDK and
// executes stream/subtitle requests with bounded concurrency.
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

let sdk = null;
let sdkPromise = null;
let sdkPath = process.env.VYLA_SDK_PATH || '';

const send = (payload) => {
  try { process.stdout.write(JSON.stringify(payload) + '\n'); } catch { /* parent gone */ }
};
const log = (...args) => {
  try { process.stderr.write('[vyla] ' + args.map(String).join(' ') + '\n'); } catch { /* ignore */ }
};

function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`timeout: ${label}`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function loadSdk() {
  if (sdk) return sdk;
  if (!sdkPromise) {
    if (!sdkPath) throw new Error('Vyla SDK path is not configured');
    const entry = path.join(sdkPath, 'src', 'sdk.js');
    sdkPromise = import(pathToFileURL(entry).href)
      .then((mod) => {
        const Ctor = mod.default || mod.VylaSDK;
        if (typeof Ctor !== 'function') throw new Error('Vyla SDK did not export a constructor');
        sdk = new Ctor();
        return sdk;
      })
      .catch((err) => {
        sdkPromise = null;
        throw err;
      });
  }
  return sdkPromise;
}

async function streams(params) {
  const instance = await loadSdk();
  const id = Number(params.id);
  if (!Number.isFinite(id) || id <= 0) throw new Error('A TMDB id is required');
  const season = params.season == null ? null : Number(params.season);
  const episode = params.episode == null ? null : Number(params.episode);
  const deadline = Date.now() + Math.max(2000, Number(params.deadlineMs) || 20000);
  const concurrency = Math.max(1, Math.min(16, Number(params.concurrency) || 8));
  const sources = instance.getSources(true) || [];
  const urls = [];
  const failures = [];
  let cursor = 0;

  async function worker() {
    while (true) {
      const index = cursor++;
      if (index >= sources.length || Date.now() >= deadline) return;
      const cfg = sources[index];
      const remaining = deadline - Date.now();
      if (remaining < 500) return;
      try {
        const result = await withTimeout(
          instance.getStream(cfg.key, id, season, episode),
          remaining,
          cfg.key,
        );
        const items = Array.isArray(result?.allUrls) && result.allUrls.length
          ? result.allUrls
          : (result?.url ? [result] : []);
        for (const item of items) {
          if (!item || typeof item.url !== 'string' || !/^https?:/i.test(item.url)) continue;
          urls.push({
            ...item,
            sourceKey: cfg.key,
            sourceLabel: cfg.label || cfg.key,
            sourceType: cfg.type || '',
          });
        }
      } catch (err) {
        failures.push({ key: cfg.key, error: err && err.message ? err.message : String(err) });
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, sources.length) }, worker));
  return { urls, failures, total: sources.length, elapsedMs: 0 };
}

async function subtitles(params) {
  const instance = await loadSdk();
  const id = params.imdbId || params.id;
  if (!id) return [];
  const season = params.season == null ? null : Number(params.season);
  const episode = params.episode == null ? null : Number(params.episode);
  const result = await withTimeout(
    instance.getSubtitles(id, season, episode),
    Math.max(4000, Number(params.timeoutMs) || 12000),
    'subtitles',
  );
  return Array.isArray(result) ? result : [];
}

async function probe(params) {
  const instance = await loadSdk();
  return withTimeout(instance.probeAllSources(), Math.max(6000, Number(params.timeoutMs) || 20000), 'probe');
}

const handlers = { streams, subtitles, probe };

async function main() {
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of rl) {
    const text = line.trim();
    if (!text) continue;
    let request;
    try {
      request = JSON.parse(text);
    } catch {
      continue;
    }
    const { id, method, params = {} } = request;
    if (method === 'shutdown') {
      send({ id, ok: true, result: true });
      process.exit(0);
    }
    if (method === 'configure') {
      if (params.sdkPath) {
        sdkPath = params.sdkPath;
        sdk = null;
        sdkPromise = null;
      }
      send({ id, ok: true, result: true });
      continue;
    }
    const handler = handlers[method];
    if (!handler) {
      send({ id, ok: false, error: `Unknown method: ${method}` });
      continue;
    }
    try {
      const result = await handler(params);
      send({ id, ok: true, result });
    } catch (err) {
      log(method, 'failed:', err && err.stack ? err.stack : err);
      send({ id, ok: false, error: err && err.message ? err.message : String(err) });
    }
  }
}

process.on('unhandledRejection', (err) => log('unhandled rejection:', err));
process.on('uncaughtException', (err) => log('uncaught exception:', err && err.stack ? err.stack : err));

main().catch((err) => {
  log('fatal:', err && err.stack ? err.stack : err);
  process.exit(1);
});
