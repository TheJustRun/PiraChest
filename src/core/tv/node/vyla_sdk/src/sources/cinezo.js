import { fetchJson } from '../utils/helpers.js';

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const referer = isTv
        ? `https://player.cinezo.live/embed/tv/${id}?s=${s}&e=${e}`
        : `https://player.cinezo.live/embed/movie/${id}`;

    const headers = {
        'Accept': '*/*',
        'Origin': 'https://player.cinezo.live',
        'Referer': referer,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0'
    };

    const hosts = [
        'https://proxy.yenime.net',
        'https://proxy1.flikhub.net'
    ];

    const sources = ['zendaya', 'berlin', 'jennifer', 'scarlet'];
    const allUrls = [];

    for (const host of hosts) {
        for (const source of sources) {
            const url = isTv
                ? `${host}/tv?id=${id}&season=${s}&episode=${e}&mode=json&sources=${source}&hevc=1`
                : `${host}/movie?id=${id}&mode=json&sources=${source}&hevc=1`;

            try {
                const res = await fetchJson(url, { headers });
                const streamUrl = res?.source?.url;
                if (streamUrl) {
                    allUrls.push({
                        url: streamUrl,
                        server: `Cinezo - ${res.source.label || source}`,
                        headers: {
                            Referer: 'https://player.cinezo.live/',
                            Origin: 'https://player.cinezo.live'
                        }
                    });
                }
            } catch { }
        }
        if (allUrls.length) break;
    }

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}