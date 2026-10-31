import { fetchJson, fetchText } from '../utils/helpers.js';

const API_URL = "https://raw.githubusercontent.com/Watchout2025/api/refs/heads/main/hls/";

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const type = isTv ? 'tv' : 'movie';

    const targetUrl = isTv
        ? `${API_URL}tv/${id}/S${s}.json`
        : `${API_URL}movie/${id}`;

    const headers = {
        'Origin': 'https://vidout.pages.dev',
        'Referer': 'https://vidout.pages.dev'
    };

    if (type === 'movie') {
        const data = await fetchText(targetUrl).catch(() => null);
        if (!data || !data.includes('://')) return null;

        return {
            url: data.trim(),
            headers,
            allUrls: [{
                url: data.trim(),
                server: 'Watchout - Direct',
                headers
            }]
        };
    } else {
        const data = await fetchJson(targetUrl).catch(() => null);
        const streamUrl = data?.[String(e)] || null;
        if (!streamUrl) return null;

        return {
            url: streamUrl,
            headers,
            allUrls: [{
                url: streamUrl,
                server: 'Watchout - Direct',
                headers
            }]
        };
    }
}
