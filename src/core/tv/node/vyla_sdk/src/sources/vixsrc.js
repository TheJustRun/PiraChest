import { fetchText, USER_AGENT } from '../utils/helpers.js';

const BASE_URL = 'https://vixsrc.to';

const HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': `${BASE_URL}/`,
    'Origin': BASE_URL
};

function extract(regex, text) {
    const m = text?.match(regex);
    if (!m) return null;
    let val = m[1];
    val = val.replace(/\\u0026/gi, '&');
    val = val.replace(/\\\//g, '/');
    val = val.replace(/\\/g, '');
    return val;
}

export async function getStream({ id, s, e, absoluteBase }) {
    try {
        const apiUrl = s && e
            ? `${BASE_URL}/api/tv/${id}/${s}/${e}`
            : `${BASE_URL}/api/movie/${id}`;

        const apiProxy = absoluteBase && absoluteBase !== '/'
            ? `${absoluteBase.replace(/^https:\/\/(localhost|127\.0\.0\.1)/, 'http://$1')}/api?url=${encodeURIComponent(apiUrl)}&proxyHeaders=${encodeURIComponent(JSON.stringify(HEADERS))}`
            : apiUrl;

        const apiText = await fetchText(apiProxy, { headers: HEADERS }).catch(() => null);
        const apiData = JSON.parse(apiText || '{}');
        if (!apiData.src) return null;

        const embedUrl = apiData.src.startsWith('http') ? apiData.src : `${BASE_URL}${apiData.src}`;
        const embedProxy = absoluteBase && absoluteBase !== '/'
            ? `${absoluteBase.replace(/^https:\/\/(localhost|127\.0\.0\.1)/, 'http://$1')}/api?url=${encodeURIComponent(embedUrl)}&proxyHeaders=${encodeURIComponent(JSON.stringify(HEADERS))}`
            : embedUrl;

        const html = await fetchText(embedProxy, { headers: HEADERS }).catch(() => null);
        if (!html) return null;

        const token = extract(/token["']\s*:\s*["']([^"']+)/, html);
        const expires = extract(/expires["']\s*:\s*["']([^"']+)/, html);
        let playlist = extract(/url["']\s*:\s*["']([^"']+)/, html);

        if (!token || !expires || !playlist) return null;

        if (playlist.startsWith('/')) {
            playlist = `${BASE_URL}${playlist}`;
        }

        const defaultLang = extract(/lang(?:uage)?["']\s*:\s*["']([a-z]{2,5})/i, html) || 'en';
        const rawMasterUrl = `${playlist}${playlist.includes('?') ? '&' : '?'}token=${token}&expires=${expires}&h=1&lang=${defaultLang}`;

        const masterProxy = absoluteBase && absoluteBase !== '/'
            ? `${absoluteBase.replace(/^https:\/\/(localhost|127\.0\.0\.1)/, 'http://$1')}/api?url=${encodeURIComponent(rawMasterUrl)}&proxyHeaders=${encodeURIComponent(JSON.stringify({ ...HEADERS, Referer: embedUrl }))}`
            : rawMasterUrl;

        const playlistText = await fetchText(masterProxy, { headers: { ...HEADERS, 'Referer': embedUrl } }).catch(() => null);
        if (!playlistText?.includes('#EXTM3U')) return null;

        const lines = playlistText.split('\n');
        const audioTracks = [];
        const seen = new Set();

        for (const line of lines) {
            if (line.startsWith('#EXT-X-MEDIA') && line.includes('TYPE=AUDIO')) {
                const name = line.match(/NAME=["']([^"']+)["']/)?.[1] || 'Default';
                const rendition = line.match(/rendition=([a-zA-Z0-9_-]+)/)?.[1]
                    || line.match(/LANGUAGE=["']([^"']+)["']/)?.[1]
                    || name;
                const key = rendition.toLowerCase();
                if (!seen.has(key)) {
                    seen.add(key);
                    audioTracks.push({ name, lang: rendition });
                }
            }
        }

        const allUrls = [];

        if (audioTracks.length > 0) {
            for (const track of audioTracks) {
                const streamUrl = `${playlist}${playlist.includes('?') ? '&' : '?'}token=${token}&expires=${expires}&h=1&lang=${track.lang}`;
                allUrls.push({
                    url: streamUrl,
                    server: `VixSrc (${track.name})`,
                    skipProxy: true,
                    headers: {
                        ...HEADERS,
                        'Referer': `${BASE_URL}/`
                    }
                });
            }
        } else {
            allUrls.push({
                url: rawMasterUrl,
                server: 'VixSrc',
                skipProxy: true,
                headers: {
                    ...HEADERS,
                    'Referer': `${BASE_URL}/`
                }
            });
        }

        return { allUrls };
    } catch (err) {
        return null;
    }
}