import { fetchJson, USER_AGENT, getTmdbInfo } from '../utils/helpers.js';

export const SKIP_VERIFY = true;
export const MULTI_URL = false;

const ENC_API = "https://enc-dec.app/api";
const BASE = "https://kisskh.do";
const HEADERS = { 'User-Agent': USER_AGENT, Accept: 'application/json' };

function cleanTitle(t) { return t ? t.toLowerCase().replace(/\(\d{4}\)/g, '').replace(/[^a-z0-9]/g, '') : ''; }

async function encKey(episodeId, type) {
    try {
        const data = await fetchJson(`${ENC_API}/enc-kisskh?text=${episodeId}&type=${type}`, { signal: AbortSignal.timeout(6000) });
        return data?.status === 200 ? data.result : null;
    } catch { return null; }
}

export async function getStream({ id, s, e }) {
    try {
        const info = await getTmdbInfo(id, s ? 'tv' : 'movie', s);
        if (!info) return null;
        let drama = null, bestScore = -1;
        for (const title of (info.titles.length > 1 ? [...info.titles].reverse() : info.titles)) {
            const results = await fetchJson(`${BASE}/api/DramaList/Search?q=${encodeURIComponent(title)}`, { headers: HEADERS, signal: AbortSignal.timeout(6000) }).catch(() => []);
            const targetClean = cleanTitle(title);
            for (const item of results) {
                const parts = (item.title || '').split(/\s*-\s*/).map(cleanTitle);
                let score = 0;
                if (parts.some(p => p === targetClean)) score += 5;
                else if (parts.some(p => p.includes(targetClean) || targetClean.includes(p))) score += 3;
                else continue;
                if (score > bestScore) { bestScore = score; drama = item; }
            }
            if (drama && bestScore >= 5) break;
        }
        if (!drama) return null;
        const detail = await fetchJson(`${BASE}/api/DramaList/Drama/${drama.id}`, { headers: HEADERS, signal: AbortSignal.timeout(8000) });
        const episodeId = detail?.episodes?.find(ep => Math.floor(ep.number) === Number(e || 1))?.id;
        if (!episodeId) return null;
        const vidKey = await encKey(episodeId, 'vid');
        if (!vidKey) return null;
        const videoData = await fetchJson(`${BASE}/api/DramaList/Episode/${episodeId}.png?err=false&ts=&time=&kkey=${vidKey}`, { headers: HEADERS, signal: AbortSignal.timeout(10000) });
        if (!videoData?.Video) return null;
        let subtitles = [];
        const subKey = await encKey(episodeId, 'sub');
        if (subKey) {
            try {
                const subList = await fetchJson(`${BASE}/api/Sub/${episodeId}?kkey=${subKey}`, { headers: HEADERS, signal: AbortSignal.timeout(8000) });
                if (Array.isArray(subList)) subtitles = subList.map(sub => ({ url: sub.src, label: sub.label, lang: sub.land, default: !!sub.default }));
            } catch { }
        }
        return { allUrls: [{ url: videoData.Video, type: videoData.Video.includes('.m3u8') ? 'hls' : 'mp4', audio: 'sub', server: 'KissKH', skipProxy: false, subtitles }] };
    } catch { return null; }
}

export async function getSources(args) {
    const stream = await getStream(args);
    return stream?.allUrls ? [...new Set(stream.allUrls.map(u => u.server))] : [];
}