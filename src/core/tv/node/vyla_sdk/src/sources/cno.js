import { fetchText } from '../utils/helpers.js';

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const serverIndices = Array.from({ length: 50 }, (_, i) => i + 1);

    const headers = {
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://cno.to/',
        'Origin': 'https://cno.to',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
    };

    const promises = serverIndices.map(async (serverNum) => {
        const url = isTv
            ? `https://cno.to/api/s/${serverNum}/tv/${id}/${s}/${e}`
            : `https://cno.to/api/s/${serverNum}/movie/${id}?v=3`;

        try {
            const raw = await fetchText(url, { headers });
            if (!raw || raw.includes('"error":')) return null;

            if (raw.includes('#EXTM3U')) {
                const lines = raw.split('\n');
                let streamUrl = null;
                let quality = 'Auto';

                for (let i = 0; i < lines.length; i++) {
                    const line = lines[i].trim();
                    if (line.startsWith('#EXT-X-STREAM-INF:')) {
                        const resMatch = line.match(/RESOLUTION=(\d+x\d+)/);
                        if (resMatch) quality = resMatch[1];
                        const nextLine = lines[i + 1]?.trim();
                        if (nextLine && !nextLine.startsWith('#')) {
                            streamUrl = nextLine;
                            break;
                        }
                    }
                }

                if (!streamUrl) {
                    for (const line of lines) {
                        const trimmed = line.trim();
                        if (trimmed && !trimmed.startsWith('#')) {
                            streamUrl = trimmed;
                            break;
                        }
                    }
                }

                if (streamUrl) {
                    if (streamUrl.startsWith('/')) {
                        streamUrl = `https://cno.to${streamUrl}`;
                    }

                    return {
                        url: streamUrl,
                        server: `Cno - Server ${serverNum} (${quality})`,
                        headers: {
                            Referer: 'https://cno.to/',
                            Origin: 'https://cno.to'
                        }
                    };
                }
            }

            try {
                const json = JSON.parse(raw);
                const jsonUrl = json.url || json.file || json.stream;
                if (jsonUrl && typeof jsonUrl === 'string' && jsonUrl.startsWith('http')) {
                    return {
                        url: jsonUrl,
                        server: `Cno - Server ${serverNum} (${json.quality || 'Auto'})`,
                        headers: {
                            Referer: 'https://cno.to/',
                            Origin: 'https://cno.to'
                        }
                    };
                }
            } catch { }
        } catch { }
        return null;
    });

    const results = await Promise.all(promises);
    const allUrls = results.filter(Boolean);

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}