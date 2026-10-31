import { fetchJson } from '../utils/helpers.js';

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const path = isTv
        ? `/stream/series/tmdb:${id}:${s}:${e}.json`
        : `/stream/movie/tmdb:${id}.json`;

    const url = `https://yastream.tamthai.de${path}`;
    const res = await fetchJson(url);
    if (!res || !Array.isArray(res.streams) || !res.streams.length) return null;

    const allUrls = res.streams.map(item => {
        if (!item.url) return null;
        return {
            url: item.url,
            server: `YaStream - ${item.name?.replace(/\n/g, ' ') || 'Server'}`,
            headers: item.behaviorHints?.proxyHeaders?.request || undefined,
            skipProxy: item.behaviorHints?.notWebReady === false
        };
    }).filter(Boolean);

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}