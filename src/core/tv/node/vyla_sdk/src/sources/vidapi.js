import { fetchJson, USER_AGENT } from '../utils/helpers.js';

const API_BASE = 'https://streamdata.vaplayer.ru/api.php';
const EMBED_ORIGIN = 'https://nextgencloudfabric.com';

export const VERIFY_HEADERS = {
    'User-Agent': USER_AGENT,
    'Referer': `${EMBED_ORIGIN}/`,
    'Origin': EMBED_ORIGIN
};

function extractStreamUrls(obj, urls = new Set()) {
    if (!obj) return urls;
    if (typeof obj === 'string') {
        if (obj.startsWith('http') && (obj.includes('.m3u8') || obj.includes('.mp4') || obj.includes('.mpd'))) {
            urls.add(obj);
        }
    } else if (Array.isArray(obj)) {
        for (const item of obj) extractStreamUrls(item, urls);
    } else if (typeof obj === 'object') {
        for (const val of Object.values(obj)) {
            extractStreamUrls(val, urls);
        }
    }
    return urls;
}

export async function getStream({ id, s, e }) {
    try {
        const isTv = s != null && e != null;
        const type = isTv ? 'tv' : 'movie';

        const url = `${API_BASE}?tmdb=${id}&type=${type}${isTv ? `&season=${s}&episode=${e}` : ''}`;

        const data = await fetchJson(url, {
            headers: {
                'Accept': '*/*',
                'Origin': EMBED_ORIGIN,
                'Referer': `${EMBED_ORIGIN}/`,
                'User-Agent': USER_AGENT
            },
            signal: AbortSignal.timeout(10000)
        });

        const streamUrls = Array.from(extractStreamUrls(data));
        if (!streamUrls.length) return null;

        let subs = [];
        if (data.subtitles && Array.isArray(data.subtitles)) {
            subs = data.subtitles;
        } else if (data.captions && Array.isArray(data.captions)) {
            subs = data.captions;
        }

        const subtitles = subs.map(sub => ({
            url: sub.url || sub.file || sub.src,
            lang: sub.display || sub.label || sub.language || sub.lang || 'Unknown'
        })).filter(s => s.url);

        const allUrls = streamUrls.map(streamUrl => {
            let type = 'mp4';
            if (streamUrl.includes('.mpd')) type = 'dash';
            else if (streamUrl.includes('.m3u8')) type = 'hls';

            return {
                url: streamUrl,
                server: 'VidAPI',
                quality: 'Auto',
                type,
                headers: VERIFY_HEADERS,
                subtitles: subtitles.length > 0 ? subtitles : undefined,
                skipProxy: false
            };
        });

        return allUrls.length ? { allUrls } : null;
    } catch {
        return null;
    }
}

export async function getSources() {
    return ['VidAPI'];
}

export async function proxyStream({ url, res, proxyUtils: { fetchUpstream, rewriteM3u8 } }) {
    try {
        const upstream = await fetchUpstream(url, 0, VERIFY_HEADERS);
        if (!upstream) return res.end();

        const ct = (upstream.headers?.['content-type'] || '').toLowerCase();
        const isM3u8 = ct.includes('mpegurl') || ct.includes('m3u8') || /\.m3u8?(\?|$)/i.test(url);

        if (isM3u8) {
            const chunks = [];
            for await (const c of upstream) chunks.push(c);
            let body = Buffer.concat(chunks).toString('utf8');

            res.setHeader('Content-Type', 'application/vnd.apple.mpegurl');
            res.setHeader('Access-Control-Allow-Origin', '*');

            return res.end(rewriteM3u8(body, url, '&va=1&tt=1'));
        }

        res.setHeader('Content-Type', ct || 'application/octet-stream');
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Cache-Control', 'public, max-age=3600');
        upstream.pipe(res);
    } catch {
        if (!res.headersSent) {
            res.statusCode = 502;
            res.end();
        }
    }
}