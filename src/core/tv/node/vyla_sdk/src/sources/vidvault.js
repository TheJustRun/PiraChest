import { USER_AGENT } from '../utils/helpers.js';

const BASE_URL = 'https://vidvault.to';
const API_BASE = `${BASE_URL}/api`;
const DL_PROXY_BASE = 'https://dl.gemlelispe.workers.dev';
const SUB_PROXY_BASE = 'https://sub.k5s7sjozpn.workers.dev';

const HEADERS = {
    'Referer': `${BASE_URL}/`,
    'Origin': BASE_URL,
    'User-Agent': USER_AGENT,
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
};

function formatBytes(bytes, decimals = 2) {
    if (!bytes || bytes <= 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), sizes.length - 1);
    return `${(bytes / Math.pow(k, i)).toFixed(decimals)} ${sizes[i]}`;
}

async function getToken() {
    const res = await fetch(`${API_BASE}/get-token`, {
        method: 'GET',
        headers: HEADERS,
        signal: AbortSignal.timeout(10000),
    });
    if (!res.ok) throw new Error(`Token fetch failed: ${res.status}`);
    const data = await res.json();
    return data?.t;
}

function getCaptionType(url) {
    return String(url).toLowerCase().includes('.vtt') ? 'vtt' : 'srt';
}

export async function getStream(args) {
    const { id, s, e } = args;
    const isTv = s != null && e != null;
    const type = isTv ? 'tv' : 'movie';

    try {
        const token = await getToken();
        if (!token) return null;

        const payload = {
            type,
            tmdbId: String(id),
        };

        if (isTv) {
            payload.season = Number(s);
            payload.episode = Number(e);
        }

        const res = await fetch(`${API_BASE}/download-proxy`, {
            method: 'POST',
            headers: {
                ...HEADERS,
                'Content-Type': 'application/json',
                'x-request-token': token,
            },
            body: JSON.stringify(payload),
            signal: AbortSignal.timeout(15000),
        });

        if (!res.ok) return null;
        const data = await res.json();

        const allUrls = [];
        const subtitles = [];
        const titleTag = encodeURIComponent(isTv ? `Show_${id}_S${s}E${e}` : `Movie_${id}`);

        const mp4 = data?.mp4Data;
        const downloadData = mp4?.downloadInfo?.data || mp4?.data?.data || mp4?.data || mp4;

        if (downloadData) {
            const streams = downloadData.streams || downloadData.downloads || [];
            for (const item of streams) {
                if (!item?.url) continue;

                const res = item.resolutions || item.resolution || 'Auto';
                const sizeStr = item.size ? ` (${typeof item.size === 'number' ? formatBytes(item.size) : item.size})` : '';
                const proxiedUrl = `${DL_PROXY_BASE}/${encodeURIComponent(item.url)}?n=${titleTag}`;

                allUrls.push({
                    url: proxiedUrl,
                    server: `VidVault - MP4 ${res}p${sizeStr}`.trim(),
                    quality: typeof res === 'number' ? `${res}p` : res,
                    type: 'mp4',
                    headers: HEADERS,
                });
            }

            const captions = downloadData.captions || [];
            for (const cap of captions) {
                if (!cap?.url) continue;
                const subUrl = `${SUB_PROXY_BASE}/?url=${encodeURIComponent(cap.url)}&title=${titleTag}`;
                subtitles.push({
                    label: cap.lanName || cap.lan || 'Unknown',
                    type: getCaptionType(cap.url),
                    language: cap.lan || '',
                    url: subUrl,
                });
            }
        }

        const mkvFiles = data?.mkvData?.files || [];
        for (const file of mkvFiles) {
            if (!file?.url) continue;
            const sizeStr = file.size ? ` (${typeof file.size === 'number' ? formatBytes(file.size) : file.size})` : '';
            allUrls.push({
                url: file.url,
                server: `VidVault - MKV 480p${sizeStr}`.trim(),
                quality: '480p',
                type: 'mp4',
                headers: HEADERS,
            });
        }

        const mkvV2 = data?.mkvV2Data;
        const mkvV2List = Array.isArray(mkvV2)
            ? mkvV2
            : Array.isArray(mkvV2?.files)
                ? mkvV2.files
                : (mkvV2?.url ? [mkvV2] : []);

        for (const file of mkvV2List) {
            if (!file?.url) continue;
            const quality = file.quality ? String(file.quality).replace(/p$/i, '') : 'Auto';
            const sizeStr = file.size ? ` (${typeof file.size === 'number' ? formatBytes(file.size) : file.size})` : '';
            const meta = [file.country, file.language].filter(Boolean).join(' • ');
            const metaStr = meta ? ` [${meta}]` : '';

            allUrls.push({
                url: file.url,
                server: `VidVault - MKV v2 ${quality}p${sizeStr}${metaStr}`.trim(),
                quality: `${quality}p`,
                type: 'mp4',
                headers: HEADERS,
            });
        }

        const mkvV3 = data?.mkvV3Data;
        if (mkvV3?.downloads && Array.isArray(mkvV3.downloads)) {
            const country = mkvV3.country || '';
            const language = mkvV3.language || '';

            for (const down of mkvV3.downloads) {
                if (Array.isArray(down?.qualities)) {
                    for (const qObj of down.qualities) {
                        for (const ep of qObj?.episodes || []) {
                            if (!ep?.url) continue;
                            const quality = qObj.quality ? String(qObj.quality).replace(/p$/i, '') : 'Auto';
                            const sizeStr = ep.size ? ` (${typeof ep.size === 'number' ? formatBytes(ep.size) : ep.size})` : '';
                            const meta = [ep.country || country, ep.language || language].filter(Boolean).join(' • ');
                            const metaStr = meta ? ` [${meta}]` : '';

                            allUrls.push({
                                url: ep.url,
                                server: `VidVault - MKV v3 ${quality}p${sizeStr}${metaStr}`.trim(),
                                quality: `${quality}p`,
                                type: 'mp4',
                                headers: HEADERS,
                            });
                        }
                    }
                } else if (down?.url) {
                    const quality = down.quality ? String(down.quality).replace(/p$/i, '') : 'Auto';
                    const sizeStr = down.size ? ` (${typeof down.size === 'number' ? formatBytes(down.size) : down.size})` : '';
                    allUrls.push({
                        url: down.url,
                        server: `VidVault - MKV v3 ${quality}p${sizeStr}`.trim(),
                        quality: `${quality}p`,
                        type: 'mp4',
                        headers: HEADERS,
                    });
                }
            }
        }

        return allUrls.length ? { allUrls, subtitles: subtitles.length ? subtitles : undefined } : null;
    } catch (err) {
        return null;
    }
}