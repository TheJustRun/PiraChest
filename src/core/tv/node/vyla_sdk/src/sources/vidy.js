import { getUA } from "../utils/helpers.js";

const API_URL = "https://api.wecollege.net";
const SERVERS = ["miami", "atlanta", "seattle", "denver", "dallas", "tampa"];
const MAGIC = [109, 118, 109, 49];

function imul(a, b) {
    return Math.imul(a | 0, b | 0) >>> 0;
}

function pFn(e) {
    e = e >>> 0;
    e = (e ^ (e >>> 16)) >>> 0;
    e = imul(e, 2246822507);
    e = (e ^ (e >>> 13)) >>> 0;
    e = imul(e, 3266489909);
    e = (e ^ (e >>> 16)) >>> 0;
    return e;
}

function gFn(e, t) {
    e = e >>> 0;
    t = t & 31;
    if (t === 0) return e;
    return ((e << t) | (e >>> (32 - t))) >>> 0;
}

function decryptVidy(ciphertext, seedStr, mediaId) {
    let padded = ciphertext.replace(/-/g, "+").replace(/_/g, "/");
    while (padded.length % 4) padded += "=";

    const raw = new Uint8Array(Buffer.from(padded, "base64"));
    const length = raw.length;

    let tFnv = 2166136261;
    for (let i = 0; i < seedStr.length; i++) {
        tFnv = imul((tFnv ^ seedStr.charCodeAt(i)) >>> 0, 16777619);
    }
    const fnvRes = pFn(tFnv);

    const innerArg = (((mediaId >>> 0) ^ 2654435769) >>> 0);
    const inner = pFn(innerArg);
    let n = pFn(((fnvRes ^ inner) >>> 0));

    const S = {};
    for (let e = 0; e < 8; e++) {
        const t = n % 61;
        n = gFn((n + 2654435769) >>> 0, 7 + (7 & e));
        S[t] = ((n ^ pFn(n)) >>> 0);
        n = pFn((n + t) >>> 0);
    }

    let acc = pFn(((2779096485 ^ n) >>> 0));
    const keystream = new Uint8Array(length);
    let sCnt = 0;
    let idx = 0;

    while (idx < length) {
        const sVal = acc;
        const o = sVal % 61;
        const hasO = Object.prototype.hasOwnProperty.call(S, o);
        const iVal = hasO ? -1 : 0;
        const d = hasO ? S[o] : 0;
        const nTerm = ((d ^ imul(2654435769, sCnt + 1)) >>> 0);
        sCnt += 1;
        const aTerm = sVal;
        let lVal = (((aTerm ^ nTerm) | (aTerm & nTerm & iVal)) >>> 0);
        const term1 = gFn((lVal + sVal) >>> 0, 31 & o);
        const term2 = gFn(sVal, (31 & imul(o, 7)) >>> 0);
        lVal = ((term1 ^ term2) >>> 0);
        const newS = pFn((lVal + 2654435769) >>> 0);
        S[o] = newS;
        acc = newS;
        const tVal = newS;

        keystream[idx++] = tVal & 0xFF;
        if (idx < length) keystream[idx++] = (tVal >>> 8) & 0xFF;
        if (idx < length) keystream[idx++] = (tVal >>> 16) & 0xFF;
        if (idx < length) keystream[idx++] = (tVal >>> 24) & 0xFF;
    }

    for (let i = 0; i < length; i++) {
        raw[i] ^= keystream[i];
    }

    for (let i = 0; i < MAGIC.length; i++) {
        if (raw[i] !== MAGIC[i]) {
            throw new Error("Vidy decrypt failed: magic header mismatch");
        }
    }

    return new TextDecoder().decode(raw.slice(MAGIC.length));
}

export async function getStream(args) {
    const { id, s = null, e = null } = args;
    const isTv = s != null && e != null;
    const mediaId = Number(id);

    const headers = {
        "User-Agent": getUA(),
        Referer: "https://www.vidy.st/",
        Origin: "https://www.vidy.st",
    };

    try {
        const seedUrl = new URL(`${API_URL}/seed`);
        seedUrl.searchParams.set("mediaId", String(id));

        const seedRes = await fetch(seedUrl.toString(), {
            headers,
            signal: AbortSignal.timeout(6000),
        });
        if (!seedRes.ok) return null;

        const seedData = await seedRes.json();
        const seed = seedData?.seed;
        if (!seed) return null;

        for (const server of SERVERS) {
            try {
                const srcUrl = new URL(`${API_URL}/${server}/sources`);
                srcUrl.searchParams.set("mediaType", isTv ? "tv" : "movie");
                srcUrl.searchParams.set("tmdbId", String(id));
                srcUrl.searchParams.set("enc", "2");
                srcUrl.searchParams.set("seed", seed);
                if (isTv) {
                    srcUrl.searchParams.set("seasonId", String(s));
                    srcUrl.searchParams.set("episodeId", String(e));
                }

                const res = await fetch(srcUrl.toString(), {
                    headers,
                    signal: AbortSignal.timeout(6000),
                });
                if (!res.ok) continue;

                const text = await res.text();

                let data;
                try {
                    data = JSON.parse(decryptVidy(text, seed, mediaId));
                } catch {
                    continue;
                }

                const sources = data?.sources || [];
                const playlist = data?.playlist || "";
                if (!sources.length && !playlist) continue;

                let streamUrl = playlist;
                let quality = "1080p";

                for (const src of sources) {
                    if (src?.quality === "1080p" && src?.url) {
                        streamUrl = src.url;
                        quality = "1080p";
                        break;
                    }
                }
                if (!streamUrl && sources.length) {
                    streamUrl = sources[0]?.url || "";
                    quality = sources[0]?.quality || "";
                }
                if (!streamUrl) continue;

                return {
                    url: streamUrl,
                    headers: {
                        "User-Agent": getUA(),
                        Referer: "https://www.vidy.st/",
                        Origin: "https://www.vidy.st",
                    },
                    server: "Vidy",
                    quality,
                    type: streamUrl.includes(".m3u8") ? "hls" : "mp4",
                };
            } catch {
                continue;
            }
        }

        return null;
    } catch {
        return null;
    }
}
