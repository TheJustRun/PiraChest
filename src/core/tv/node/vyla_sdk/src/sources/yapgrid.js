import { fetchJson } from '../utils/helpers.js';

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const targetEmbedUrl = isTv
        ? `https://embedwave.cc/embed/tv/${id}/${s}/${e}`
        : `https://embedwave.cc/embed/movie/${id}`;

    const headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Origin': 'https://yapgrid.com',
        'Referer': isTv ? `https://yapgrid.com/embed/tv/${id}/${s}/${e}` : `https://yapgrid.com/embed/movie/${id}`,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
    };

    const payload = {
        url: targetEmbedUrl,
        source: 'embedwave',
        strictSource: true,
        waitMs: 5000,
        nativeHls: false,
        cacheOnly: true
    };

    const res = await fetchJson('https://yapgrid.com/player/api/extract', {
        method: 'POST',
        headers,
        body: JSON.stringify(payload)
    }).catch(() => null);

    if (!res?.success || !Array.isArray(res.found) || !res.found.length) return null;

    const allUrls = [];

    for (const item of res.found) {
        if (!item.url) continue;

        let streamUrl = item.url;
        if (streamUrl.startsWith('/')) {
            streamUrl = `https://yapgrid.com${streamUrl}`;
        }

        allUrls.push({
            url: streamUrl,
            server: `YapGrid - ${item.nxshaProvider || item.provider || 'Server'}`,
            type: item.type === 'master' ? 'hls' : (streamUrl.includes('.m3u8') ? 'hls' : 'mp4'),
            headers: {
                Referer: 'https://yapgrid.com/',
                Origin: 'https://yapgrid.com',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            }
        });
    }

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}