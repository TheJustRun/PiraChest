import { USER_AGENT, fetchText, fetchJson } from '../utils/helpers.js';

const TMDB_BASE = 'https://api.themoviedb.org/3';
const LM_DOMAINS = ['https://www.lookmovie2.to', 'https://lookmovie2.to', 'https://lookmovie.foundation'];
const HEADERS_BASE = { 'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9' };

async function searchLookMovie(type, title, year, clientIP) {
    for (const base of LM_DOMAINS) {
        try {
            const headers = { ...HEADERS_BASE, 'Accept': 'application/json', 'Referer': `${base}/`, 'X-Requested-With': 'XMLHttpRequest', ...(clientIP && { 'X-Forwarded-For': clientIP }) };
            const data = await fetchJson(`${base}/api/v1/${type}/do-search/?q=${encodeURIComponent(title)}`, { headers, signal: AbortSignal.timeout(4000) });
            const results = data?.result;
            if (results?.length) {
                const match = results.find(r => String(r.year) === String(year)) ?? results.find(r => r.title?.toLowerCase() === title.toLowerCase()) ?? results[0];
                if (match) return { match, base };
            }
        } catch { }
    }
    return null;
}

const STORAGE_RE = /window\[['"](?:movie|show)_storage['"]\]\s*=\s*\{([^}]+)\}/s;
const ID_MOVIE_RE = /['"]?(?:id_movie|movieId)['"]?\s*[:=]\s*['"]?(\d+)['"]?/i;
const SEASONS_RE = /seasons\s*:\s*(\[[\s\S]+?\])\s*[,}]/;

function getEpisodeIdFast(html, s, e) {
    const sMatch = html.match(STORAGE_RE);
    if (sMatch) {
        const block = sMatch[1], sm = block.match(SEASONS_RE);
        if (sm) {
            try {
                const seasons = JSON.parse(sm[1]);
                const season = seasons.find(x => String(x?.season ?? x?.meta?.season) === String(s));
                if (season) {
                    const ep = Array.isArray(season.episodes) ? season.episodes.find(x => String(x.episode) === String(e)) : (season.episodes?.[String(e)] || Object.values(season.episodes || {}).find(x => String(x.episode) === String(e)));
                    if (ep) return String(ep.id_episode ?? ep.id);
                }
            } catch { }
        }
    }
    const parts = html.split(/id_episode['"]?\s*[:=]\s*['"]?(\d+)['"]?/i);
    for (let i = 1; i < parts.length; i += 2) {
        const context = parts[i - 1].slice(-300) + parts[i + 1].slice(0, 300);
        if (context.match(new RegExp(`['"]?episode['"]?\\s*[:=]\\s*['"]?${e}['"]?`, 'i')) && context.match(new RegExp(`['"]?season['"]?\\s*[:=]\\s*['"]?${s}['"]?`, 'i'))) return parts[i];
    }
    const am = html.match(new RegExp(`data-season=["']${s}["'][^>]*?data-episode=["']${e}["'][^>]*?data-id=["'](\\d+)["']`, 'i')) || html.match(new RegExp(`data-episode=["']${e}["'][^>]*?data-season=["']${s}["'][^>]*?data-id=["'](\\d+)["']`, 'i'));
    return am ? am[1] : null;
}

export async function getStream({ id, s, e, sdk, clientIP }) {
    const tmdbApiKey = sdk?.tmdbApiKey || null;
    if (!tmdbApiKey) return null;
    try {
        const tmdbKey = tmdbApiKey;
        if (!tmdbKey) return null;
        const isTV = s != null && e != null;
        const typeStr = isTV ? 'shows' : 'movies';
        const tmdbData = await fetchJson(`${TMDB_BASE}/${isTV ? 'tv' : 'movie'}/${id}?api_key=${tmdbKey}`, { signal: AbortSignal.timeout(3000) });
        const title = tmdbData?.title || tmdbData?.name;
        const year = (tmdbData?.first_air_date || tmdbData?.release_date || '').slice(0, 4);
        if (!title) return null;
        const searchRes = await searchLookMovie(typeStr, title, year, clientIP);
        if (!searchRes?.match?.slug) return null;
        const { match, base } = searchRes;
        const headers = { ...HEADERS_BASE, 'Accept': 'text/html', 'Referer': `${base}/`, ...(clientIP && { 'X-Forwarded-For': clientIP }) };
        const html = await fetchText(`${base}/${typeStr}/play/${match.slug}`, { headers, signal: AbortSignal.timeout(8000) });
        const storageMatch = html.match(STORAGE_RE);
        if (!storageMatch) return null;
        const hashMatch = storageMatch[1].match(/hash\s*:\s*['"]([^'"]+)['"]/);
        const expiresMatch = storageMatch[1].match(/expires\s*:\s*(\d+)/);
        if (!hashMatch || !expiresMatch) return null;
        const streamId = isTV ? getEpisodeIdFast(html, s, e) : (match.id_movie || match.id || html.match(ID_MOVIE_RE)?.[1]);
        if (!streamId) return null;
        const accessHeaders = { ...HEADERS_BASE, 'Accept': 'application/json', 'Referer': `${base}/`, 'X-Requested-With': 'XMLHttpRequest', ...(clientIP && { 'X-Forwarded-For': clientIP }) };
        const data = await fetchJson(`${base}/api/v1/security/${isTV ? 'episode' : 'movie'}-access?id_${isTV ? 'episode' : 'movie'}=${streamId}&hash=${hashMatch[1]}&expires=${expiresMatch[1]}`, { headers: accessHeaders, signal: AbortSignal.timeout(8000) });
        const streams = data?.streams ?? data?.result?.streams ?? data?.data?.streams ?? data;
        const allUrls = Object.entries(streams || {}).filter(([, v]) => typeof v === 'string' && v.includes('.m3u8')).map(([quality, url]) => ({ url, quality }));
        return allUrls.length ? { allUrls } : null;
    } catch { return null; }
}