import { fetchJson } from '../utils/helpers.js';

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const path = isTv ? `/stream/tv/${id}/${s}/${e}` : `/stream/movie/${id}`;
    const mainUrl = `https://api.thugtv.online/api${path}`;

    const headers = {
        'Accept': 'application/json',
        'Referer': 'https://thugtv.online/',
        'Origin': 'https://thugtv.online',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
    };

    const res = await fetchJson(mainUrl, { headers }).catch(() => null);
    if (!res || !Array.isArray(res.sources) || !res.sources.length) return null;

    const allUrls = [];

    for (const src of res.sources) {
        if (src.type === 'embed') continue;

        if (src.url && src.url.startsWith('http')) {
            allUrls.push({
                url: src.url,
                server: `ThugTV - ${src.name || src.label || 'Direct'} (${src.quality || 'Auto'})`,
                headers: {
                    Referer: 'https://thugtv.online/',
                    Origin: 'https://thugtv.online'
                }
            });
            continue;
        }

        if (src.resolvePath) {
            const cleanPath = src.resolvePath.startsWith('/') ? src.resolvePath : `/${src.resolvePath}`;
            const resolveUrl = `https://thugtv.online/api${cleanPath}`;

            try {
                const resolved = await fetchJson(resolveUrl, { headers });
                const directUrl = resolved?.source?.url;
                if (directUrl && directUrl.startsWith('http')) {
                    allUrls.push({
                        url: directUrl,
                        server: `ThugTV - ${resolved.source.name || resolved.source.label || src.name || 'Direct'}`,
                        headers: {
                            Referer: 'https://thugtv.online/',
                            Origin: 'https://thugtv.online'
                        }
                    });
                }
            } catch { }
        }
    }

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}