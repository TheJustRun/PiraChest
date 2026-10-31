const BASE_URL = 'https://www.films365.org';
const HEADERS = {
    Authorization: 'Bearer 79a02956be35835728a044b11e2ae793149d45fb2c89cb6d029ec01aac19bfdb',
    'Content-Type': 'application/json',
    'User-Agent': 'MovieDownloader/1.0',
};

async function resolveTitle(sdk, id, isTv) {
    const apiKey = sdk?.tmdbApiKey || process.env.TMDB_API_KEY;
    if (!apiKey) return null;
    const type = isTv ? 'tv' : 'movie';
    try {
        const res = await fetch(`https://api.themoviedb.org/3/${type}/${id}?api_key=${apiKey}`, {
            signal: AbortSignal.timeout(6000),
        });
        if (!res.ok) return null;
        const data = await res.json();
        return isTv ? data.name : data.title;
    } catch {
        return null;
    }
}

export async function getStream({ id, s, e, sdk }) {
    const isTv = Boolean(s && e);
    const targetType = isTv ? 'tv' : 'movie';

    const title = await resolveTitle(sdk, id, isTv);
    const query = title || String(id);

    const searchRes = await fetch(`${BASE_URL}/api/mobile/search`, {
        method: 'POST',
        headers: HEADERS,
        body: JSON.stringify({ query }),
        signal: AbortSignal.timeout(8000),
    });

    if (!searchRes.ok) return null;
    const searchJson = await searchRes.json();
    const resultsObj = searchJson?.results;
    if (!resultsObj) return null;

    const items = resultsObj.all || (targetType === 'tv' ? resultsObj.tvs : resultsObj.movies) || [];
    if (!Array.isArray(items) || items.length === 0) return null;

    const cleanSearchTitle = query.toLowerCase().replace(/[^a-z0-9]/g, '');
    let matchedItem = null;

    for (const item of items) {
        const itemType = String(item?.type || '').toLowerCase();
        if (itemType && itemType !== targetType) continue;

        const itemTitle = String(item?.title || '');
        const cleanItemTitle = itemTitle.toLowerCase().replace(/[^a-z0-9]/g, '');

        if (cleanItemTitle === cleanSearchTitle || cleanItemTitle.includes(cleanSearchTitle)) {
            matchedItem = item;
            break;
        }
    }

    if (!matchedItem) {
        matchedItem = items.find(i => String(i?.type || '').toLowerCase() === targetType) || items[0];
    }

    const itemId = matchedItem?.id || matchedItem?.tmdbId;
    if (!itemId) return null;

    const detailsRes = await fetch(`${BASE_URL}/api/mobile/details?id=${itemId}&type=${targetType}`, {
        headers: HEADERS,
        signal: AbortSignal.timeout(8000),
    });

    if (!detailsRes.ok) return null;
    const detailsJson = await detailsRes.json();
    const data = detailsJson?.data;
    if (!data) return null;

    const spoken = Array.isArray(data.spokenLanguages)
        ? data.spokenLanguages.map(l => String(l).trim()).filter(Boolean)
        : [];
    const langSuffix = spoken.length > 0 ? ` · ${spoken.join(', ')}` : '';

    const allUrls = [];

    if (!isTv) {
        const streamUrl = data.downloadUrl || data.videoUrl;
        if (streamUrl && typeof streamUrl === 'string') {
            allUrls.push({
                url: streamUrl.trim(),
                label: `X-Downloader${langSuffix}`,
                headers: {
                    'User-Agent': HEADERS['User-Agent'],
                },
            });
        }
    } else {
        const seasonNum = Number(s);
        const episodeNum = Number(e);
        const seasons = Array.isArray(data.seasons) ? data.seasons : [];
        const targetSeason = seasons.find(sn => Number(sn.seasonNumber) === seasonNum);

        if (targetSeason) {
            const episodes = Array.isArray(targetSeason.episodes) ? targetSeason.episodes : [];
            const targetEp = episodes.find(ep => Number(ep.episodeNumber) === episodeNum);

            if (targetEp) {
                const streamUrl = targetEp.downloadUrl || targetEp.videoUrl;
                if (streamUrl && typeof streamUrl === 'string') {
                    allUrls.push({
                        url: streamUrl.trim(),
                        label: `X-Downloader (S${seasonNum}E${episodeNum})${langSuffix}`,
                        headers: {
                            'User-Agent': HEADERS['User-Agent'],
                        },
                    });
                }
            }
        }
    }

    if (allUrls.length === 0) return null;

    return {
        url: allUrls[0].url,
        allUrls,
        headers: allUrls[0].headers,
    };
}