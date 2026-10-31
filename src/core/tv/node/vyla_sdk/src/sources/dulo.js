import { getUA } from '../utils/helpers.js';

const BASE_URL = 'https://dulo.mov';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function resolveDetails(sdk, id, isTv) {
    const apiKey = sdk?.tmdbApiKey || process.env.TMDB_API_KEY;
    if (!apiKey) return null;
    const type = isTv ? 'tv' : 'movie';
    try {
        const res = await fetch(`https://api.themoviedb.org/3/${type}/${id}?api_key=${apiKey}`, {
            signal: AbortSignal.timeout(6000),
        });
        if (!res.ok) return null;
        const data = await res.json();
        const title = isTv ? data.name : data.title;
        const releaseDate = isTv ? data.first_air_date : data.release_date;
        const year = releaseDate ? releaseDate.split('-')[0] : '';
        const runtime = isTv ? (data.episode_run_time?.[0] || 24) : (data.runtime || 90);
        return { title, year, runtime };
    } catch {
        return null;
    }
}

async function fetchSource(url, headers) {
    try {
        const res = await fetch(url, {
            headers,
            signal: AbortSignal.timeout(8000),
        });
        if (!res.ok) return null;
        return await res.json();
    } catch {
        return null;
    }
}

export async function getStream({ id, s, e, sdk }) {
    const isTv = Boolean(s && e);
    const mediaType = isTv ? 'tv' : 'movie';
    const ua = getUA();

    const details = await resolveDetails(sdk, id, isTv);
    const title = details?.title || '';
    const year = details?.year || '';
    const runtime = details?.runtime || '';

    const referer = isTv
        ? `${BASE_URL}/watch/tv/${id}/${s}/${e}`
        : `${BASE_URL}/watch/movie/${id}`;

    const defaultHeaders = {
        'User-Agent': ua,
        Referer: referer,
        Origin: BASE_URL,
        Accept: '*/*',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    };

    const baseParams = new URLSearchParams({
        tmdbId: String(id),
        type: mediaType,
    });

    const additionalParams = new URLSearchParams({
        tmdbId: String(id),
        type: mediaType,
        progressive: 'true',
    });

    if (title) additionalParams.set('title', title);
    if (year) additionalParams.set('year', String(year));
    if (runtime) additionalParams.set('runtime', String(runtime));

    if (isTv) {
        baseParams.set('season', String(s));
        baseParams.set('episode', String(e));
        additionalParams.set('season', String(s));
        additionalParams.set('episode', String(e));
    }

    const endpoints = [
        `${BASE_URL}/api/sources?${baseParams.toString()}`,
        `${BASE_URL}/api/sources/willow?${baseParams.toString()}`,
        `${BASE_URL}/api/sources/additional?${additionalParams.toString()}`,
    ];

    const seenUrls = new Set();
    const allUrls = [];

    let pendingUrls = [...endpoints];
    const maxPollTime = Date.now() + 16000;

    while (pendingUrls.length > 0 && Date.now() < maxPollTime) {
        const results = await Promise.all(
            pendingUrls.map((url) => fetchSource(url, defaultHeaders))
        );

        const nextPending = [];

        results.forEach((data, index) => {
            if (!data) return;

            if (Array.isArray(data.sources) && data.sources.length > 0) {
                for (const src of data.sources) {
                    let streamUrl = String(src?.url || '').trim();
                    if (!streamUrl) continue;

                    if (streamUrl.startsWith('/')) {
                        streamUrl = `${BASE_URL}${streamUrl}`;
                    }

                    if (!streamUrl.startsWith('http') || seenUrls.has(streamUrl)) continue;
                    seenUrls.add(streamUrl);

                    allUrls.push({
                        url: streamUrl,
                        label: src.label || 'Dulo',
                        headers: {
                            'User-Agent': ua,
                            Referer: referer,
                        },
                        captions: Array.isArray(src.captions) ? src.captions : [],
                    });
                }
            }

            if (data.pending) {
                nextPending.push(pendingUrls[index]);
            }
        });

        pendingUrls = nextPending;
        if (pendingUrls.length > 0) {
            await sleep(1500);
        }
    }

    if (allUrls.length === 0) return null;

    return {
        url: allUrls[0].url,
        allUrls,
        headers: allUrls[0].headers,
    };
}