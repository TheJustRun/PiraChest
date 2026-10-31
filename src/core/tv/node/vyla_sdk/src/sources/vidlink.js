import { fetchJson, USER_AGENT } from '../utils/helpers.js';

const BASE = "https://vidlink.pro";
const HEADERS = { 'User-Agent': USER_AGENT, Origin: BASE, Referer: `${BASE}/` };

export async function getStream({ id, s, e }) {
    try {
        const encData = await fetchJson(`https://enc-dec.app/api/enc-vidlink?text=${id}`, { signal: AbortSignal.timeout(6000) });
        if (encData?.status !== 200 || !encData?.result) return null;

        const basePath = s ? `${BASE}/api/b/tv/${encData.result}/${s}/${e || 1}` : `${BASE}/api/b/movie/${encData.result}`;
        const responses = await Promise.allSettled([
            fetchJson(basePath, { headers: HEADERS, signal: AbortSignal.timeout(10000) }),
            fetchJson(`${basePath}?multiLang=0`, { headers: HEADERS, signal: AbortSignal.timeout(10000) })
        ]);

        const streamList = [];
        const seenUrls = new Set();
        const seenSubs = new Set();

        for (const res of responses) {
            if (res.status !== 'fulfilled' || !res.value?.stream) continue;
            const stream = res.value.stream;

            if (stream.playlist && !seenUrls.has(stream.playlist)) {
                seenUrls.add(stream.playlist);
                const isDash = stream.type === 'dash' || stream.deliveryType === 'dash' || stream.playlist.includes('.mpd');
                streamList.push({
                    url: stream.playlist,
                    quality: stream.playbackMetadata?.resolutions?.[0] ? `${stream.playbackMetadata.resolutions[0]}p` : 'Auto',
                    label: `Vidlink ${isDash ? 'DASH' : 'HLS'}`,
                    name: `Vidlink ${isDash ? 'DASH' : 'HLS'}`,
                    type: isDash ? 'dash' : 'hls',
                    headers: stream.playlistHeaders || {},
                    server: 'Vidlink',
                });
            }

            if (stream.qualities && typeof stream.qualities === 'object') {
                const qualityOrder = ['1080', '720', '480', '360', ...Object.keys(stream.qualities)];
                const seenQualities = new Set();

                for (const q of qualityOrder) {
                    if (seenQualities.has(q)) continue;
                    seenQualities.add(q);
                    const qData = stream.qualities[q];
                    if (!qData?.url || seenUrls.has(qData.url)) continue;
                    seenUrls.add(qData.url);
                    streamList.push({
                        url: qData.url,
                        quality: `${q}p`,
                        label: `Vidlink ${q}p`,
                        name: `Vidlink ${q}p`,
                        type: qData.type || 'mp4',
                        headers: qData.headers || {},
                        server: 'Vidlink',
                    });
                }
            }
        }

        if (!streamList.length) return null;

        return {
            allUrls: streamList,
            streams: streamList,
        };
    } catch {
        return null;
    }
}

export async function getSources(args) {
    const stream = await getStream(args);
    return stream?.allUrls ? [...new Set(stream.allUrls.map(u => u.server || 'Vidlink'))] : [];
}