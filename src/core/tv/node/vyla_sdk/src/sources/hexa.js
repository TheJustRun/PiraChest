import { fetchJson, fetchText } from '../utils/helpers.js';

const API = "https://enc-dec.app/api";

function getRandomHex(size = 32) {
    const array = new Uint8Array(size);
    crypto.getRandomValues(array);
    return Array.from(array).map(b => b.toString(16).padStart(2, '0')).join('');
}

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const key = getRandomHex(32);

    const headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Referer": "https://hexa.su/",
        "Accept": "text/plain",
        "X-Fingerprint-Lite": "e9136c41504646444",
        "X-Api-Key": key
    };

    const encHexaRes = await fetchJson(`${API}/enc-hexa`);
    if (!encHexaRes || encHexaRes.status !== 200) return null;
    headers["X-Cap-Token"] = encHexaRes.result.token;

    const url = isTv
        ? `https://theemoviedb.hexa.su/api/tmdb/tv/${id}/season/${s}/episode/${e}/images`
        : `https://theemoviedb.hexa.su/api/tmdb/movie/${id}/images`;

    const encrypted = await fetchText(url, { headers });
    if (!encrypted) return null;

    const decRes = await fetchJson(`${API}/dec-hexa`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: encrypted, key })
    });

    if (!decRes || decRes.status !== 200 || !decRes.result) return null;

    const data = decRes.result;
    const streamUrl = data.url || data.file || (Array.isArray(data.sources) ? data.sources[0]?.url : null);

    if (!streamUrl) return null;

    return {
        url: streamUrl,
        headers: {
            Referer: "https://hexa.su/",
            Origin: "https://hexa.su"
        }
    };
}