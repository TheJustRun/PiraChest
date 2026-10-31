import { getUA } from '../utils/helpers.js';

const SERVICE_ORIGIN = 'https://st.111477.xyz';

function generateManifestBaseUrl(tmdbKey) {
    let config = 'https://a.111477.xyz/::sort=file-desc::limit=3';
    if (tmdbKey) {
        config += `::tmdb=${tmdbKey}`;
    }
    const b64 = Buffer.from(config, 'utf8')
        .toString('base64')
        .replace(/\+/g, '-')
        .replace(/\//g, '_')
        .replace(/=+$/, '');
    return `${SERVICE_ORIGIN}/config/${b64}`;
}

async function resolveImdbId(sdk, id, isTv) {
    const apiKey = sdk?.tmdbApiKey || process.env.TMDB_API_KEY;
    if (!apiKey) return null;
    const type = isTv ? 'tv' : 'movie';
    try {
        const res = await fetch(`https://api.themoviedb.org/3/${type}/${id}?api_key=${apiKey}&append_to_response=external_ids`, {
            signal: AbortSignal.timeout(6000),
        });
        if (!res.ok) return null;
        const data = await res.json();
        return data.imdb_id || data.external_ids?.imdb_id || null;
    } catch {
        return null;
    }
}

export async function getStream({ id, s, e, sdk }) {
    const isTv = Boolean(s && e);
    const ua = getUA();
    const apiKey = sdk?.tmdbApiKey || process.env.TMDB_API_KEY;

    const imdbId = await resolveImdbId(sdk, id, isTv);
    const targetIds = [];

    if (imdbId && imdbId.startsWith('tt')) {
        targetIds.push(imdbId);
    }
    targetIds.push(`tmdb:${id}`);

    const addonBase = generateManifestBaseUrl(apiKey);
    const seenUrls = new Set();
    const allUrls = [];

    const defaultHeaders = {
        'User-Agent': ua,
        Accept: 'application/json, text/plain, */*',
    };

    for (const targetId of targetIds) {
        const endpoint = isTv
            ? `${addonBase}/stream/series/${targetId}:${s || 1}:${e || 1}.json`
            : `${addonBase}/stream/movie/${targetId}.json`;

        try {
            const res = await fetch(endpoint, {
                headers: defaultHeaders,
                signal: AbortSignal.timeout(12000),
            });

            if (!res.ok) continue;
            const data = await res.json();

            if (Array.isArray(data?.streams) && data.streams.length > 0) {
                for (const item of data.streams) {
                    if (!item || typeof item !== 'object') continue;
                    const url = String(item.url || '').trim();
                    if (!url || !url.startsWith('http') || seenUrls.has(url)) continue;

                    seenUrls.add(url);

                    const rawTitle = item.title ? String(item.title).trim() : '';
                    const rawName = item.name ? String(item.name).trim() : '111477';
                    const label = rawTitle || rawName;

                    allUrls.push({
                        url,
                        label,
                        headers: defaultHeaders,
                    });
                }

                if (allUrls.length > 0) break;
            }
        } catch { }
    }

    if (allUrls.length === 0) return null;

    return {
        url: allUrls[0].url,
        allUrls,
        headers: allUrls[0].headers,
    };
}