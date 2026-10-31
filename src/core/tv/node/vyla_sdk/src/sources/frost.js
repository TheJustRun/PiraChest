import { fetchJson, resolveImdbId } from '../utils/helpers.js';

export async function getStream({ id, s, e, tmdbApiKey, sdk }) {
    const isTv = s != null && e != null;
    const type = isTv ? 'series' : 'movie';
    const imdb = await resolveImdbId(id, isTv, tmdbApiKey || sdk?.tmdbApiKey);
    if (!imdb) return null;

    const url = isTv
        ? `https://froststream.cloutteam.com/stream/series/${imdb}:${s}:${e}.json`
        : `https://froststream.cloutteam.com/stream/movie/${imdb}.json`;

    const res = await fetchJson(url).catch(() => null);
    if (!res || !Array.isArray(res.streams) || !res.streams.length) return null;

    const allUrls = res.streams.filter(st => st.url).map(st => ({
        url: st.url,
        server: `Frost - ${st.name || 'Server'} ${st.title ? `- ${st.title}` : ''}`,
        headers: st.behaviorHints?.proxyHeaders?.request || undefined,
        skipProxy: st.behaviorHints?.notWebReady === false
    }));

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}