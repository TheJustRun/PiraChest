import { fetchText, USER_AGENT, getTmdbInfo } from '../utils/helpers.js';

const ORIGIN = 'https://www3.fsonline.app';
const AJAX_URL = `${ORIGIN}/wp-admin/admin-ajax.php`;
const HEADERS = { 'User-Agent': USER_AGENT, 'Origin': ORIGIN, 'Referer': `${ORIGIN}/` };

export async function getStream({ id, s, e, sdk }) {
    const tmdbApiKey = sdk?.tmdbApiKey || null;
    if (!tmdbApiKey) return null;
    try {
        const isTv = s != null && e != null;
        const info = await getTmdbInfo(tmdbApiKey, id, isTv ? 'tv' : 'movie');
        if (!info?.titles?.length) return null;
        const searchHtml = await fetchText(`${ORIGIN}/?s=${encodeURIComponent(info.titles[0] + (info.year ? ' ' + info.year : ''))}`, { headers: HEADERS });
        const typeFolder = isTv ? 'seriale' : 'film';
        const linkMatch = searchHtml.match(new RegExp(`href=["'](https?://www3\\.fsonline\\.app/${typeFolder}/([^"'/]+)/)["']`, 'i'));
        if (!linkMatch) return null;
        const targetPageUrl = isTv ? `${ORIGIN}/episoade/${linkMatch[2].replace(/-\d{4}$/, '')}-sezonul-${s}-episodul-${e}/` : linkMatch[1];
        const pageHtml = await fetchText(targetPageUrl, { headers: HEADERS });
        const movieId = (pageHtml.match(/movie-id=['"]([^'"]+)['"]/) || pageHtml.match(/movie-id=([^ >]+)/))?.[1];
        if (!movieId) return null;
        const ajaxHtml = await fetchText(AJAX_URL, { method: 'POST', headers: { ...HEADERS, 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest', 'Referer': targetPageUrl }, body: `action=lazy_player&movieID=${movieId}` });
        const results = [];
        let idx = 0;
        while ((idx = ajaxHtml.indexOf('data-vs="', idx)) !== -1) {
            const embedStart = idx + 9;
            const embedEnd = ajaxHtml.indexOf('"', embedStart);
            const embedUrl = ajaxHtml.slice(embedStart, embedEnd);
            const spanStart = ajaxHtml.indexOf('<span>', embedEnd);
            const spanEnd = ajaxHtml.indexOf('</span>', spanStart);
            const serverLabel = ajaxHtml.slice(spanStart + 6, spanEnd).trim().toLowerCase();
            if (serverLabel.includes('filesun')) {
                try {
                    const rHtml = await fetchText(embedUrl, { headers: { 'Referer': ORIGIN, 'User-Agent': USER_AGENT } });
                    const m3u8Match = rHtml.match(/file:\s*["'](https?:\/\/[^"']+\.m3u8[^"']*)["']/) || rHtml.match(/["']?file["']?:\s*["'](https?:\/\/[^"']+)["']/);
                    if (m3u8Match) results.push({ url: m3u8Match[1].replace(/\\\//g, '/'), server: 'FSOnline - FileSuN', headers: { 'Referer': embedUrl, 'Origin': 'https://player.fsonline.app', 'User-Agent': USER_AGENT } });
                } catch { }
            }
            idx = spanEnd;
        }
        return results.length ? { allUrls: results } : null;
    } catch { return null; }
}

export async function getSources(args) {
    const res = await getStream(args);
    return res?.allUrls ? [...new Set(res.allUrls.map(u => u.server))] : [];
}