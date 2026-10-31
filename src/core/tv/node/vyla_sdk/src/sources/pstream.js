import { USER_AGENT } from '../utils/helpers.js';

async function safeImport(moduleName) {
    try {
        return await import(`./${moduleName}.js`);
    } catch {
        return null;
    }
}

export async function getStream(args) {
    const { id, s, e, clientIP, sdk } = args;
    const isTv = s != null && e != null;
    const type = isTv ? 'tv' : 'movie';
    const allUrls = [];

    const providers = [
        { name: 'CineSu', key: 'cinesu', fnName: 'fetchCineSuStreams' },
        { name: 'ZxcStream', key: 'zxcstream', fnName: 'fetchZxcStreams' },
        { name: 'VidFast', key: 'vidfast', fnName: 'fetchVidFastStreams' },
        { name: '1Embed', key: '1embed', fnName: 'fetchOneEmbedStreams' },
    ];

    await Promise.all(
        providers.map(async ({ name, key, fnName }) => {
            try {
                let streamResult = null;

                if (sdk) {
                    try {
                        streamResult = await sdk.getStream(key, id, s, e, clientIP);
                    } catch { }
                }

                if (!streamResult) {
                    const mod = await safeImport(key);
                    if (mod) {
                        if (typeof mod.getStream === 'function') {
                            streamResult = await mod.getStream(args);
                        } else if (typeof mod[fnName] === 'function') {
                            const raw = await mod[fnName]({ type, id, season: s, episode: e });
                            if (Array.isArray(raw)) {
                                streamResult = { allUrls: raw };
                            }
                        }
                    }
                }

                if (!streamResult) return;

                const streams = streamResult.allUrls || (streamResult.url ? [streamResult] : []);
                for (const item of streams) {
                    allUrls.push({
                        url: item.url,
                        server: `FlyStream (${item.source || item.server || name})`,
                        quality: item.quality || 'Auto',
                        type: item.type || item.format || (item.url?.includes('.m3u8') ? 'hls' : 'mp4'),
                        headers: item.headers || {
                            'User-Agent': USER_AGENT,
                        },
                    });
                }
            } catch { }
        })
    );

    return allUrls.length ? { allUrls } : null;
}