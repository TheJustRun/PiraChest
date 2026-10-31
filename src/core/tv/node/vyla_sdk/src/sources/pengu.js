import { fetchJson } from '../utils/helpers.js';

// const PENGU_CONFIG = '%7B%22source_4khdhub%22%3A%22on%22%2C%22source_moviebox%22%3A%22on%22%2C%22source_moviesdrives%22%3A%22on%22%2C%22source_vaplayer%22%3A%22on%22%2C%22source_hdghartv%22%3A%22on%22%2C%22res_2160%22%3A%22on%22%2C%22res_1080%22%3A%22on%22%2C%22res_720%22%3A%22on%22%2C%22disable_direct%22%3A%22on%22%7D';
// const BASE_URL = `https://pengu.uk/${PENGU_CONFIG}`;

async function getImdbId(tmdbApiKey, tmdbId, s, e) {
    const key = tmdbApiKey;
    if (!key) return null;
    try {
        const url = s != null && e != null
            ? `https://api.themoviedb.org/3/tv/${tmdbId}/external_ids?api_key=${key}`
            : `https://api.themoviedb.org/3/movie/${tmdbId}?api_key=${key}`;
        const res = await fetchJson(url);
        return res?.imdb_id || null;
    } catch { return null; }
}

export async function getStream({ id, s, e, sdk }) {
    const tmdbApiKey = sdk?.tmdbApiKey || null;
    const manifest = sdk?.penguManifest || null;
    if (!tmdbApiKey || !manifest || (manifest && !manifest.endsWith('manifest.json'))) return null;
    const BASE_URL = manifest ? manifest.split('/manifest.json')[0] : null;
    if (!BASE_URL) return null;
    try {
        const imdbId = await getImdbId(tmdbApiKey, id, s, e);
        if (!imdbId) return null;

        const path = s != null && e != null
            ? `/stream/series/${imdbId}:${s}:${e}.json`
            : `/stream/movie/${imdbId}.json`;

        const res = await fetchJson(`${BASE_URL}${path}`);
        if (!res?.streams?.length) return null;

        const allUrls = res.streams.map(stream => {
            if (!stream.url) return null;
            const headers = stream.behaviorHints?.proxyHeaders?.request || {};
            return {
                url: stream.url,
                server: `Pengu - ${stream.name || 'Server'}`,
                headers: Object.keys(headers).length > 0 ? headers : undefined,
                skipProxy: stream.behaviorHints?.notWebReady === false
            };
        }).filter(Boolean);

        return allUrls.length ? { allUrls } : null;
    } catch {
        return null;
    }
}