import { HEALTH_PROBE_ID } from "./config.js";

const withTimeout = (promise, ms) => Promise.race([promise, new Promise(r => setTimeout(() => r(null), ms))]);
async function withRetry(fn, attempts = 2, delay = 300) {
    for (let i = 0; i < attempts; i++) {
        try {
            const result = await fn();
            if (result != null) return result;
        } catch (err) {
            if (i === attempts - 1) throw err;
            await new Promise(r => setTimeout(r, delay + Math.random() * delay * 0.5));
        }
    }
    return null;
}

export async function probeSource(sdk, cfg, mod) {
    const audio = /dub$/.test(cfg.key) ? 'dub' : 'sub';
    const streamArgs = {
        id: HEALTH_PROBE_ID,
        s: null,
        e: null,
        clientIP: null,
        absoluteBase: null,
        audio,
        sdk,
        config: cfg,
    };

    const t = Date.now();
    const result = await withTimeout(
        withRetry(() => mod.getStream(streamArgs), cfg.retries, 300),
        cfg.timeout
    );

    const firstUrl = result?.url ?? result?.allUrls?.[0]?.url;
    return { ok: !!(typeof firstUrl === 'string' && firstUrl.startsWith('http')), ms: Date.now() - t };
}