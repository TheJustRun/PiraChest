import { fetchJson } from '../utils/helpers.js';

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const separator = isTv ? '&' : '?';
    const basePath = isTv
        ? `https://f.rivo.st/api/source/tv/${id}?s=${s}&e=${e}`
        : `https://f.rivo.st/api/source/movie/${id}`;

    const headers = {
        'Accept': '*/*',
        'Referer': 'https://f.rivo.st/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
    };

    const serverIndices = Array.from({ length: 26 }, (_, i) => i + 1);

    const responses = await Promise.allSettled(
        serverIndices.map(serverNum =>
            fetchJson(`${basePath}&server=${serverNum}`, { headers })
                .catch(() => null)
        )
    );

    const allUrls = [];

    for (const res of responses) {
        if (res.status !== 'fulfilled' || !res.value) continue;
        const data = res.value;

        if (Array.isArray(data.sources)) {
            for (const src of data.sources) {
                if (src?.url) {
                    allUrls.push({
                        url: src.url,
                        server: `Rivo - ${data.server || src.subProvider || 'Server'}`,
                        headers: {
                            Referer: 'https://f.rivo.st/',
                            Origin: 'https://f.rivo.st'
                        }
                    });
                }
            }
        }
    }

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}