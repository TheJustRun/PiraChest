import { USER_AGENT, fetchJson, getTmdbInfo } from '../utils/helpers.js';

const BASE_URL = 'https://providers.peestream.in';

const PROVIDERS = {
    purstream: 'Athena',
    vidrock: 'Thor',
    vaplayer: 'Poseidon',
    netmirror: 'Ra'
};

export async function getStream(args) {
    const { id, s, e, server, tmdbApiKey } = args;
    const isTv = s != null && e != null;
    const type = isTv ? 'tv' : 'movie';

    let title = args.title || '';
    let releaseYear = args.year || '';
    let imdbId = args.imdbId || '';
    let seasonTmdbId = args.seasonTmdbId || '';
    let episodeTmdbId = args.episodeTmdbId || '';

    try {
        const info = await getTmdbInfo(id, type, s, tmdbApiKey);
        if (info) {
            title = title || info.name || info.title || '';

            if (!releaseYear) {
                const date = info.release_date || info.first_air_date || '';
                releaseYear = date ? date.split('-')[0] : (info.year || '');
            }

            if (!imdbId && info.external_ids?.imdb_id) {
                imdbId = info.external_ids.imdb_id;
            } else if (!imdbId && info.imdb_id) {
                imdbId = info.imdb_id;
            }

            if (isTv) {
                if (!seasonTmdbId && Array.isArray(info.seasons)) {
                    const matchedSeason = info.seasons.find(
                        item => item.season_number === Number(s)
                    );
                    if (matchedSeason) {
                        seasonTmdbId = String(matchedSeason.id);
                    }
                }

                if (!episodeTmdbId && tmdbApiKey) {
                    try {
                        const seasonData = await fetchJson(
                            `https://api.themoviedb.org/3/tv/${id}/season/${s}?api_key=${tmdbApiKey}`
                        );
                        if (seasonData?.episodes) {
                            const matchedEp = seasonData.episodes.find(
                                ep => ep.episode_number === Number(e)
                            );
                            if (matchedEp) {
                                episodeTmdbId = String(matchedEp.id);
                            }
                        }
                    } catch { }
                }
            }
        }
    } catch { }

    const queryParams = new URLSearchParams({
        type,
        tmdbId: String(id),
        title: title || String(id)
    });

    if (releaseYear) queryParams.set('releaseYear', String(releaseYear));
    if (imdbId) queryParams.set('imdbId', String(imdbId));

    if (isTv) {
        queryParams.set('seasonNumber', String(s));
        queryParams.set('episodeNumber', String(e));
        if (seasonTmdbId) queryParams.set('seasonTmdbId', String(seasonTmdbId));
        if (episodeTmdbId) queryParams.set('episodeTmdbId', String(episodeTmdbId));
    }

    const allUrls = [];

    try {
        const scrapeUrl = `${BASE_URL}/scrape?${queryParams.toString()}`;
        const res = await fetch(scrapeUrl, {
            headers: {
                'User-Agent': USER_AGENT,
                'Accept': 'text/event-stream',
                'Referer': `${BASE_URL}/`
            },
            signal: AbortSignal.timeout(20000)
        });

        if (res.ok) {
            const text = await res.text();
            const events = text.split('\n\n');

            for (const ev of events) {
                if (ev.includes('event: completed')) {
                    const dataMatch = ev.match(/data:\s*(.+)/);
                    if (dataMatch) {
                        try {
                            const parsed = JSON.parse(dataMatch[1].trim());
                            const stream = parsed.stream;
                            const streamUrl = stream?.playlist || stream?.url || stream?.file;
                            if (streamUrl) {
                                const sourceName = PROVIDERS[parsed.sourceId] || parsed.sourceId || 'Poseidon';
                                allUrls.push({
                                    url: streamUrl,
                                    server: `PeeStream - ${sourceName}`,
                                    quality: 'Auto',
                                    type: stream.type === 'hls' || streamUrl.includes('.m3u8') ? 'hls' : 'mp4',
                                    headers: stream.headers || {
                                        'User-Agent': USER_AGENT
                                    }
                                });
                            }
                        } catch { }
                    }
                }
            }
        }
    } catch { }

    if (server && server !== 'all') {
        const clean = server.toLowerCase().replace('peestream - ', '').trim();
        const filtered = allUrls.filter(u => u.server.toLowerCase().includes(clean));
        if (filtered.length) return { allUrls: filtered };
    }

    return allUrls.length ? { allUrls } : null;
}

export async function getSources() {
    return Object.values(PROVIDERS).map(name => `PeeStream - ${name}`);
}