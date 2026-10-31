import { fetchJson, resolveImdbId } from '../utils/helpers.js';

export async function getStream({ id, s, e, tmdbApiKey, sdk }) {
    const isTv = s != null && e != null;
    const type = isTv ? 'series' : 'movie';
    const imdb = await resolveImdbId(id, isTv, tmdbApiKey || sdk?.tmdbApiKey);
    if (!imdb) return null;

    const CONFIG = btoa(JSON.stringify({ "torbox": "unset", "qualities": "2160p,1080p,720p", "sort": "desc", "catalogs": "featured,popular" }));
    const url = `https://hdhub.thevolecitor.qzz.io/${CONFIG}/stream/${type}/${imdb}${isTv ? `:${s}:${e}` : ""}.json`;

    const res = await fetchJson(url).catch(() => null);
    if (!res || !Array.isArray(res.streams) || !res.streams.length) return null;

    const validStreams = res.streams.filter(st => {
        if (!st.url || !st.url.trim()) return false;
        const lower = st.url.toLowerCase();
        if (lower.includes('hubcloud') || lower.includes('pixeldrain.dev') || lower.endsWith('.zip')) return false;
        return true;
    });

    if (!validStreams.length) return null;

    const allUrls = validStreams.map(st => ({
        url: st.url,
        server: `HDHub - ${st.name || 'Server'}`,
        headers: st.behaviorHints?.proxyHeaders?.request || undefined,
        skipProxy: st.behaviorHints?.notWebReady === false
    }));

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}
