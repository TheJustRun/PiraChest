import { fetchJson } from '../utils/helpers.js';

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const path = isTv
        ? `/local/stream/series/tmdb:${id}:${s}:${e}.json`
        : `/local/stream/movie/tmdb:${id}.json`;

    const url = `https://hyper.stravo.site${path}`;
    const res = await fetchJson(url);
    if (!res || !Array.isArray(res.streams)) return null;

    const validStreams = res.streams.filter(item => {
        if (!item.url || item.externalUrl) return false;
        if (typeof item.title === 'string' && item.title.includes('🔒')) return false;
        return true;
    });

    if (!validStreams.length) return null;

    const allUrls = validStreams.map(item => ({
        url: item.url,
        server: `Hyper - ${item.title?.replace(/[^\x20-\x7E]+/g, '').trim() || 'Server'}`,
        headers: item.behaviorHints?.proxyHeaders?.request || {
            Referer: 'https://mzfi.me/',
            Accept: '*/*'
        },
        skipProxy: item.behaviorHints?.notWebReady === false
    }));

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}