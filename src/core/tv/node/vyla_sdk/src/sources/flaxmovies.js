import { fetchJson } from '../utils/helpers.js';

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const allUrls = [];

    const endpoints = [
        async () => {
            const url = isTv
                ? `https://themoviebox.uk/api/resolve?type=tv&id=${id}&season=${s}&episode=${e}`
                : `https://themoviebox.uk/api/resolve?type=movie&id=${id}`;
            const res = await fetchJson(url);
            if (res?.success && Array.isArray(res.sources)) {
                for (const item of res.sources) {
                    const streamUrl = item.url || item.original_url;
                    if (streamUrl) {
                        allUrls.push({
                            url: streamUrl,
                            server: `TheMovieBox - ${item.server || item.provider || 'Server'}`,
                            headers: {
                                Referer: 'https://themoviebox.uk/',
                                Origin: 'https://themoviebox.uk'
                            }
                        });
                    }
                }
            }
        },

        async () => {
            const url = isTv
                ? `https://pro2.nabilekson.workers.dev/tv/${id}/${s}/${e}`
                : `https://pro2.nabilekson.workers.dev/movie/${id}`;
            const res = await fetchJson(url);
            if (res?.success && Array.isArray(res.primarySources)) {
                for (const item of res.primarySources) {
                    const streamUrl = item.url || item.originalUrl;
                    if (streamUrl) {
                        allUrls.push({
                            url: streamUrl,
                            server: `CineBros - ${item.name || item.quality || 'Server'}`,
                            headers: item.headers || {
                                Referer: 'https://pro2.nabilekson.workers.dev/',
                                Origin: 'https://pro2.nabilekson.workers.dev'
                            }
                        });
                    }
                }
            }
        },

        async () => {
            const url = isTv
                ? `https://praxi.nabilekson.workers.dev/tv/${id}/${s}/${e}`
                : `https://praxi.nabilekson.workers.dev/movie/${id}`;
            const res = await fetchJson(url);
            if (res?.success && Array.isArray(res.primarySources)) {
                for (const item of res.primarySources) {
                    const streamUrl = item.url || item.originalUrl;
                    if (streamUrl) {
                        allUrls.push({
                            url: streamUrl,
                            server: `Praxi - ${item.name || item.providerName || 'Server'}`,
                            headers: item.headers || {
                                Referer: 'https://praxi.nabilekson.workers.dev/',
                                Origin: 'https://praxi.nabilekson.workers.dev'
                            }
                        });
                    }
                }
            }
        },

        async () => {
            const url = isTv
                ? `https://freakyniki.elaxo.lol/?tmdb_id=${id}&season=${s}&episode=${e}`
                : `https://freakyniki.elaxo.lol/?tmdb_id=${id}`;
            const res = await fetchJson(url);
            if (Array.isArray(res?.streams)) {
                for (const item of res.streams) {
                    const streamUrl = item.url || item.raw_url;
                    if (streamUrl) {
                        allUrls.push({
                            url: streamUrl,
                            server: `FreakyNiki - ${item.server || item.provider || 'Server'}`,
                            headers: {
                                Referer: 'https://freakyniki.elaxo.lol/',
                                Origin: 'https://freakyniki.elaxo.lol'
                            }
                        });
                    }
                }
            }
        }
    ];

    await Promise.allSettled(endpoints.map(fn => fn().catch(() => { })));

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}