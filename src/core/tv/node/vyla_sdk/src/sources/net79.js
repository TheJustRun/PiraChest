import { fetchJson } from '../utils/helpers.js';

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const url = isTv
        ? `https://net79.cc/api/embed-tmdb/${id}?type=tv&se=${s}&ep=${e}`
        : `https://net79.cc/api/embed-tmdb/${id}?type=movie`;

    const headers = {
        'Accept': 'application/json',
        'Referer': 'https://net79.cc/',
        'Origin': 'https://net79.cc',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
    };

    const res = await fetchJson(url, { headers }).catch(() => null);
    if (!res || !res.ok || res.noSource) return null;

    const allUrls = [];

    if (Array.isArray(res.streams) && res.streams.length > 0) {
        for (const stream of res.streams) {
            if (stream?.url) {
                allUrls.push({
                    url: stream.url,
                    quality: stream.resolution ? `${stream.resolution}p` : 'Auto',
                    server: `Net79 - ${stream.resolution ? `${stream.resolution}p` : 'MP4'}`,
                    type: stream.url.includes('.m3u8') ? 'hls' : 'mp4',
                    headers: {
                        Referer: 'https://net79.cc/',
                        Origin: 'https://net79.cc'
                    }
                });
            }
        }
    } else if (res.mp4) {
        allUrls.push({
            url: res.mp4,
            quality: res.resolution ? `${res.resolution}p` : 'Auto',
            server: `Net79 - ${res.resolution ? `${res.resolution}p` : 'MP4'}`,
            type: res.mp4.includes('.m3u8') ? 'hls' : 'mp4',
            headers: {
                Referer: 'https://net79.cc/',
                Origin: 'https://net79.cc'
            }
        });
    }

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}