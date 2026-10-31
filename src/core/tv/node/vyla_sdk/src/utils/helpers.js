import crypto from 'node:crypto';

export const UA_LIST = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0',
];

export const getUA = () => UA_LIST[Math.floor(Math.random() * UA_LIST.length)];
export const USER_AGENT = UA_LIST[0];

let sharedDcKey = null;

export function decryptPartner(responseText, keyEnv) {
    try {
        const sep = responseText.indexOf(':');
        if (sep === -1) return null;
        const iv = Buffer.from(responseText.slice(0, sep), 'base64');
        const encrypted = responseText.slice(sep + 1);
        if (!sharedDcKey) sharedDcKey = crypto.createHash('sha256').update(keyEnv || '').digest();
        const decipher = crypto.createDecipheriv('aes-256-cbc', sharedDcKey, iv);
        return JSON.parse(decipher.update(encrypted, 'base64', 'utf8') + decipher.final('utf8'));
    } catch {
        return null;
    }
}

export async function fetchJson(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
}

export async function fetchText(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.text();
}

const tmdbInfoCache = new Map();
const anilistCache = new Map();
const tmdbValidationCache = new Map();

function pruneHelperCache(cache) {
    const now = Date.now();
    for (const [k, v] of cache) if (now - v.ts >= v.ttl) cache.delete(k);
    if (cache.size > 3000) {
        const overflow = cache.size - 3000;
        const it = cache.keys();
        for (let i = 0; i < overflow; i++) cache.delete(it.next().value);
    }
}

export async function resolveImdbId(id, isTv, tmdbApiKey) {
    if (typeof id === 'string' && id.startsWith('tt')) return id;
    const apiKey = tmdbApiKey || process.env.TMDB_API_KEY;
    if (!apiKey) return `tt${id}`;
    const type = isTv ? 'tv' : 'movie';
    try {
        const res = await fetch(`https://api.themoviedb.org/3/${type}/${id}?api_key=${apiKey}&append_to_response=external_ids`, {
            signal: AbortSignal.timeout(5000),
        });
        if (!res.ok) return `tt${id}`;
        const data = await res.json();
        return data.imdb_id || data.external_ids?.imdb_id || `tt${id}`;
    } catch {
        return `tt${id}`;
    }
}

setInterval(() => {
    pruneHelperCache(tmdbValidationCache);
    pruneHelperCache(tmdbInfoCache);
    pruneHelperCache(anilistCache);
}, 60000).unref();

function cacheGet(cache, key) {
    const entry = cache.get(key);
    if (!entry) return undefined;
    if (Date.now() - entry.ts >= entry.ttl) { cache.delete(key); return undefined; }
    return entry.val;
}

function cacheSet(cache, key, val, ttl) { cache.set(key, { val, ts: Date.now(), ttl }); }

export async function createStreamArgs(source, sdk, id, s, e, clientIP = null) {
    const audio = /dub$/.test(source.key) ? 'dub' : 'sub';
    return {
        id: id,
        s: s,
        e: e,
        clientIP: clientIP,
        absoluteBase: '/',
        audio,
        sdk,
        config: source,
    }
}

export async function validateTmdbId(tmdbApiKey, tmdbId, mediaType = 'movie') {
    let apiKey = tmdbApiKey;
    let id = tmdbId;
    let type = mediaType;
    if (id === 'movie' || id === 'tv' || !id) {
        if (apiKey && apiKey !== 'movie' && apiKey !== 'tv') {
            type = id || 'movie';
            id = apiKey;
            apiKey = process.env.TMDB_API_KEY;
        }
    }
    if (!apiKey) {
        apiKey = process.env.TMDB_API_KEY;
    }
    const key = `${id}-${type}`;
    const cached = cacheGet(tmdbValidationCache, key);
    if (cached !== undefined) return cached;
    if (!apiKey) return { valid: false, error: 'TMDB API key not configured' };
    try {
        const res = await fetch(`https://api.themoviedb.org/3/${type}/${id}?api_key=${apiKey}`, { signal: AbortSignal.timeout(5000) });
        if (res.ok) {
            const data = await res.json();
            const isValid = !!(data?.id && (data?.title || data?.name));
            const result = { valid: isValid, error: isValid ? null : 'Invalid TMDB ID' };
            cacheSet(tmdbValidationCache, key, result, 300000);
            return result;
        }
        const result = { valid: false, error: `TMDB API error: ${res.status}` };
        cacheSet(tmdbValidationCache, key, result, 60000);
        return result;
    } catch (error) {
        const result = { valid: false, error: `Failed to validate TMDB ID: ${error.message}` };
        cacheSet(tmdbValidationCache, key, result, 60000);
        return result;
    }
}

export async function getTmdbInfo(tmdbApiKey, tmdbId, mediaType, season) {
    let apiKey = tmdbApiKey;
    let id = tmdbId;
    let type = mediaType;
    let s = season;
    if (apiKey === 'movie' || apiKey === 'tv') {
        type = apiKey;
        id = tmdbId;
        apiKey = process.env.TMDB_API_KEY;
    } else if (id === 'movie' || id === 'tv') {
        type = id;
        id = apiKey;
        apiKey = process.env.TMDB_API_KEY;
    }
    if (!apiKey) {
        apiKey = process.env.TMDB_API_KEY;
    }
    const key = `${id}-${type}-${s || ''}`;
    const cached = cacheGet(tmdbInfoCache, key);
    if (cached !== undefined) return cached;
    if (!apiKey) return { isAnime: false, titles: [], year: null, imdbId: null };
    try {
        const [mainRes, seasonRes] = await Promise.all([
            fetch(`https://api.themoviedb.org/3/${type}/${id}?api_key=${apiKey}&append_to_response=external_ids`, { signal: AbortSignal.timeout(5000) }),
            s && type === 'tv' ? fetch(`https://api.themoviedb.org/3/tv/${id}/season/${s}?api_key=${apiKey}`, { signal: AbortSignal.timeout(5000) }) : Promise.resolve(null),
        ]);
        let mainData = null, seasonData = null;
        if (mainRes.ok) mainData = await mainRes.json(); else mainRes.body?.cancel();
        if (seasonRes) { if (seasonRes.ok) seasonData = await seasonRes.json(); else seasonRes.body?.cancel(); }
        const genres = mainData?.genres || [];
        const isAnime = genres.some(g => g.id === 16) && ((mainData?.origin_country || []).includes('JP') || mainData?.original_language === 'ja');
        const titles = [];
        if (seasonData?.name) titles.push(seasonData.name);
        const titleText = mainData?.title || mainData?.name || '';
        const ot = mainData?.original_title || mainData?.original_name || '';
        if (titleText) titles.push(titleText);
        if (ot && ot !== titleText) titles.push(ot);
        const dateStr = seasonData?.air_date || mainData?.release_date || mainData?.first_air_date || '';
        const result = { isAnime, titles: [...new Set(titles.filter(Boolean))], year: dateStr ? parseInt(dateStr.slice(0, 4), 10) : null, imdbId: mainData?.imdb_id || mainData?.external_ids?.imdb_id || null };
        cacheSet(tmdbInfoCache, key, result, 600000);
        return result;
    } catch { return { isAnime: false, titles: [], year: null, imdbId: null }; }
}

export async function tmdbToAnilist(tmdbId, mediaType, season, titles = [], year = null) {
    const key = `${tmdbId}-${mediaType}-${season || ''}`;
    const cached = cacheGet(anilistCache, key);
    if (cached !== undefined) return cached;
    try {
        const res = await fetch(`https://api.ani.zip/mappings?tmdb_id=${tmdbId}&type=${mediaType}&season=${season || 1}`, { signal: AbortSignal.timeout(6000) });
        if (res.ok) {
            const id = (await res.json())?.mappings?.[0]?.anilist_id;
            if (id) { cacheSet(anilistCache, key, id, 600000); return id; }
        } else res.body?.cancel();
    } catch { }
    if (!titles.length) return null;
    const query = `query ($s: String) { Page(page:1,perPage:10) { media(search:$s,type:ANIME) { id title { romaji english native } startDate { year } format } } }`;
    let bestId = null, bestScore = -1;
    for (const searchTitle of titles) {
        try {
            const res = await fetch('https://graphql.anilist.co', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ query, variables: { s: searchTitle } }), signal: AbortSignal.timeout(6000) });
            if (!res.ok) { res.body?.cancel(); continue; }
            for (const entry of (await res.json())?.data?.Page?.media || []) {
                const entryTitles = [entry.title?.romaji, entry.title?.english, entry.title?.native].filter(Boolean).map(t => t.toLowerCase());
                const sl = searchTitle.toLowerCase();
                let score = 0;
                if (entryTitles.some(t => t === sl)) score += 5;
                else if (entryTitles.some(t => t.includes(sl) || sl.includes(t))) score += 3;
                else if (entryTitles.some(t => { const w = sl.split(/\s+/).filter(x => x.length > 3); return w.length > 0 && w.every(x => t.includes(x)); })) score += 2;
                else continue;
                if (year && entry.startDate?.year) {
                    const diff = Math.abs(entry.startDate.year - year);
                    if (diff === 0) score += 3; else if (diff === 1) score += 1; else if (diff > 2) score -= 3;
                }
                if (mediaType === 'tv' && ['TV', 'TV_SHORT', 'ONA', 'OVA'].includes(entry.format)) score += 1;
                if (mediaType === 'movie' && entry.format === 'MOVIE') score += 1;
                if (score > bestScore) { bestScore = score; bestId = entry.id; }
            }
            if (bestScore >= 8) break;
        } catch { }
    }
    if (bestId) cacheSet(anilistCache, key, bestId, 600000);
    return bestId;
}

export async function imdbToTmdb(tmdbApiKey, imdbId, mediaType = 'movie') {
    let apiKey = tmdbApiKey;
    let id = imdbId;
    let type = mediaType;
    if (id === 'movie' || id === 'tv') {
        type = id;
        id = apiKey;
        apiKey = process.env.TMDB_API_KEY;
    }
    if (!apiKey) {
        apiKey = process.env.TMDB_API_KEY;
    }
    const key = `imdb-${id}`;
    const cached = cacheGet(tmdbInfoCache, key);
    if (cached !== undefined) return cached;
    if (!apiKey) return null;
    try {
        const data = await fetchJson(`https://api.themoviedb.org/3/find/${id}?api_key=${apiKey}&external_source=imdb_id`, { signal: AbortSignal.timeout(5000) });
        let result = null;
        if (type === 'movie' && data.movie_results?.length) result = data.movie_results[0].id;
        else if (type === 'tv' && data.tv_results?.length) result = data.tv_results[0].id;
        if (result) cacheSet(tmdbInfoCache, key, result, 600000);
        return result;
    } catch { return null; }
}