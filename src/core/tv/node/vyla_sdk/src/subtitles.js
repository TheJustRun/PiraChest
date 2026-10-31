import { getUA } from './utils/helpers.js';

const SUBTITLE_BASES = [
    'https://sub.vdrk.site/v1',
    'https://sub.vdrk.site/v2',
    'https://opensubtitles-v3.strem.io'
];

async function resolveImdbId(id, isTv) {
    if (typeof id === 'string' && id.startsWith('tt')) return id;
    const apiKey = process.env.TMDB_API_KEY;
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

export async function fetchSubtitles(paths = []) {
    try {
        const results = await Promise.all(
            paths.map(async ({ base, path }) => {
                try {
                    const res = await fetch(`${base}${path}`, {
                        headers: { 'User-Agent': getUA() },
                        signal: AbortSignal.timeout(6000),
                    });

                    if (!res.ok) {
                        res.body?.cancel();
                        return [];
                    }

                    const data = await res.json();

                    if (base.includes('/v2')) {
                        return Array.isArray(data)
                            ? data.map(x => ({
                                label: x.label,
                                file: x.file || x.url,
                                type: 'vtt',
                                source: 'v2'
                            }))
                            : [];
                    }

                    if (base.includes('opensubtitles-v3.strem.io')) {
                        if (!Array.isArray(data?.subtitles)) return [];
                        return data.subtitles.map(sub => {
                            if (!sub?.url) return null;
                            const isSrt = sub.url.toLowerCase().endsWith('.srt');
                            return {
                                label: sub.lang ? sub.lang.toUpperCase() : 'OpenSubtitles',
                                file: sub.url,
                                type: isSrt ? 'srt' : 'vtt',
                                source: 'opensubtitles'
                            };
                        }).filter(Boolean);
                    }

                    if (base.includes('fed-subs.pstream.mov')) {
                        if (!data?.subtitles || typeof data.subtitles !== 'object') return [];

                        return Object.entries(data.subtitles)
                            .map(([language, sub]) => {
                                if (!sub?.subtitle_link) return null;
                                const ext = sub.subtitle_link.split('.').pop()?.toLowerCase();
                                return {
                                    label: sub.subtitle_name || language,
                                    file: sub.subtitle_link,
                                    type: ext === 'vtt' ? 'vtt' : 'srt',
                                    source: 'febbox'
                                };
                            })
                            .filter(Boolean);
                    }

                    const v1 = Array.isArray(data) ? data : [];
                    return v1.map(x => ({
                        label: x.label,
                        file: x.file || x.url,
                        type: 'vtt',
                        source: 'v1'
                    }));
                } catch {
                    return [];
                }
            })
        );

        return results.flat();
    } catch {
        return [];
    }
}

export async function getSubPathsMovie(id) {
    const imdbId = await resolveImdbId(id, false);
    return [
        { base: SUBTITLE_BASES[0], path: `/movie/${id}` },
        { base: SUBTITLE_BASES[1], path: `/movie/${id}` },
        { base: SUBTITLE_BASES[2], path: `/subtitles/movie/${imdbId}.json` }
    ];
}

export async function getSubPathsTv(id, season, episode) {
    const imdbId = await resolveImdbId(id, true);
    return [
        { base: SUBTITLE_BASES[0], path: `/tv/${id}/${season}/${episode}` },
        { base: SUBTITLE_BASES[1], path: `/tv/${id}/${season}/${episode}` },
        { base: SUBTITLE_BASES[2], path: `/subtitles/series/${imdbId}:${season}:${episode}.json` }
    ];
}

export { SUBTITLE_BASES };