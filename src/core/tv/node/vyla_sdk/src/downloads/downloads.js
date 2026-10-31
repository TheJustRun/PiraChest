import SOURCES from "../config.js";

export async function fetchDownloads(sdkOrId, idOrS = null, sOrE = null, maybeE = null) {
    let sdk = null;
    let id = sdkOrId;
    let s = idOrS;
    let e = sOrE;

    if (sdkOrId && typeof sdkOrId === 'object' && typeof sdkOrId.getStream === 'function') {
        sdk = sdkOrId;
        id = idOrS;
        s = sOrE;
        e = maybeE;
    }

    if (!sdk || !id) return [];

    const activeSources = SOURCES.filter(cfg => !cfg.disabled && cfg.skipProxy);
    const seenUrls = new Set();
    const downloads = [];

    const results = await Promise.allSettled(
        activeSources.map(async (cfg) => {
            try {
                const res = await sdk.getStream(cfg.key, id, s, e);
                if (!res) return;

                const items = Array.isArray(res.allUrls) && res.allUrls.length > 0
                    ? res.allUrls
                    : (res.url ? [{ url: res.url, label: cfg.label }] : []);

                for (const item of items) {
                    if (!item?.url || typeof item.url !== 'string') continue;
                    const url = item.url.trim();

                    if (!url.startsWith('http') || url.includes('.m3u8')) continue;
                    if (!/\.mp4(\?|$)/i.test(url) && !url.includes('.mp4')) continue;
                    if (seenUrls.has(url)) continue;

                    seenUrls.add(url);

                    downloads.push({
                        source: cfg.key,
                        label: item.label || cfg.label,
                        url,
                    });
                }
            } catch { }
        })
    );

    return downloads;
}