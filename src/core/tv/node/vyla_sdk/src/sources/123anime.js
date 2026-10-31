import { getTmdbInfo, fetchJson, USER_AGENT } from '../utils/helpers.js';

async function resolveSlug(id, s, e, info) {
    const titles = info?.titles || [];
    const keywords = [];
    for (const title of titles) {
        const t = title.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
        if (!keywords.includes(t)) keywords.push(t);
        const words = title.split(/[\s:]+/).filter(w => w.length > 2);
        if (words.length >= 2) {
            const short = words.slice(0, 3).join(' ').toLowerCase();
            if (!keywords.includes(short)) keywords.push(short);
        }
    }
    const slugCandidates = [];
    for (const keyword of keywords) {
        try {
            const data = await fetchJson(`https://123animehub.cc/ajax/film/search?keyword=${encodeURIComponent(keyword)}&_=${Date.now()}`, { headers: { 'X-Requested-With': 'XMLHttpRequest', 'Referer': 'https://123animehub.cc', 'Accept': 'application/json', 'User-Agent': USER_AGENT }, signal: AbortSignal.timeout(8000) });
            let html = data.html || '', idx = 0;
            while ((idx = html.indexOf('href="/anime/', idx)) !== -1) {
                const start = idx + 13;
                const end = html.indexOf('"', start);
                const slug = html.slice(start, end);
                if (!slugCandidates.includes(slug)) slugCandidates.push(slug);
                idx = end;
            }
        } catch { }
        if (slugCandidates.length) break;
    }
    for (const slug of slugCandidates) {
        try {
            const data = await fetchJson(`https://123animehub.cc/ajax/episode/info?epr=${encodeURIComponent(s ? `${slug}/${s}/${e}` : `${slug}/1`)}&ts=1&_=${Date.now()}`, { headers: { 'Referer': `https://123animehub.cc/anime/${slug}`, 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json, text/javascript, */*; q=0.01', 'User-Agent': USER_AGENT }, signal: AbortSignal.timeout(10000) });
            if (data?.target) return data.target;
        } catch { }
    }
    return null;
}

export async function getStream({ id, s, e }) {
    if (!s) return null;
    const info = await getTmdbInfo(id, 'tv', s);
    if (!info.isAnime) return null;
    const target = await resolveSlug(id, s, e, info);
    if (!target) return null;
    const embedMatch = target.match(/\/embed-[^/]+\/([A-Za-z0-9+/=]+)$/);
    if (!embedMatch) return null;
    const origin = new URL(target).origin;
    try {
        const embedRes = await fetch(target, { headers: { 'User-Agent': USER_AGENT }, signal: AbortSignal.timeout(10000) });
        if (!embedRes.ok) return null;
        const setCookie = embedRes.headers.get('set-cookie');
        const data = await fetchJson(`${origin}/hs/getSources?id=${embedMatch[1]}`, { headers: { 'Referer': target, 'Accept': '*/*', 'User-Agent': USER_AGENT, ...(setCookie ? { 'Cookie': setCookie.split(';')[0].trim() } : {}) }, signal: AbortSignal.timeout(10000) });
        const src = typeof data.sources === 'string' && data.sources.length ? data.sources : (Array.isArray(data.sources) && data.sources.length ? (data.sources[0].file || data.sources[0].src || data.sources[0].url) : null);
        if (src) return { url: src, headers: { 'Referer': 'https://play2.echovideo.ru/', 'Origin': 'https://play2.echovideo.ru' } };
    } catch { }
    return null;
}