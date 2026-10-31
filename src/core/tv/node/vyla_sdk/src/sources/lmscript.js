import { getTmdbInfo } from '../utils/helpers.js';

export async function getStream(args) {
    const { id, s, e, tmdbApiKey, sdk } = args;
    const type = (s != null && e != null) ? 'tv' : 'movie';
    const apiKey = tmdbApiKey || sdk?.tmdbApiKey || process.env.TMDB_API_KEY || '45dbdd5e714da212424bda0336167447';

    try {
        if (type === 'tv') {
            return null;
        }

        const tmdbInfo = await getTmdbInfo(apiKey, id, type, s);
        const titles = tmdbInfo?.titles || [];
        if (!titles.length) return null;

        let movie = null;
        for (const title of titles) {
            const url = `https://lmscript.xyz/v1/movies?filters[q]=${encodeURIComponent(title)}&expand=streams`;
            const response = await fetch(url);
            if (!response.ok) continue;
            const data = await response.json();

            const found = data.items?.find(item => {
                const tmdbPrefixMatch = `${item.tmdb_prefix}` === `${id}`;
                const tmdbIdMatch = `${item.tmdb_id}` === `${id}`;
                const titleMatch = item.title?.toLowerCase() === title.toLowerCase();
                return tmdbPrefixMatch || tmdbIdMatch || titleMatch;
            });

            if (found) {
                movie = found;
                break;
            }
        }

        if (!movie) return null;

        const streams = movie.streams || {};
        const SOURCE_ORDER = ["1080p", "720p", "480p", "360p"];
        const sortedStreams = Object.keys(streams).sort((a, b) => {
            const indexA = SOURCE_ORDER.indexOf(a);
            const indexB = SOURCE_ORDER.indexOf(b);
            return (indexA === -1 ? 99 : indexA) - (indexB === -1 ? 99 : indexB);
        });

        if (!sortedStreams.length) return null;

        const allUrls = sortedStreams.map(quality => {
            const streamUrl = streams[quality];
            return {
                url: streamUrl,
                quality,
                server: `LMScript - ${quality}`,
                type: streamUrl.includes('.m3u8') ? 'hls' : 'mp4',
                headers: {
                    Referer: 'https://lookmovie2.to/',
                    Origin: 'https://lookmovie2.to'
                }
            };
        });

        return {
            url: allUrls[0].url,
            headers: allUrls[0].headers,
            allUrls
        };
    } catch {
        return null;
    }
}