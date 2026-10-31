import crypto from "crypto";
import { Session, ClientIdentifier, initTLS, destroyTLS } from "node-tls-client";

const BASE_URL = "https://cinema.army";
const API_BASE = "https://cinema.army/api";

const XOR_KEY = "f16a6b92fcd6eed3e9476297f893f2505c32f79bc6efef9b6c95496aa5914548";
const HMAC_KEY = "a2cfc9eaa2a3690b5d4f6b5f958dbdeae52a097283d375bd253ee7c7062524f5";

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

const SERVERS = [
    { key: "cookiebakers", method: "POST", label: "CookieBakers" },
    { key: "tcloud", method: "GET", label: "TCloud" },
    { key: "ipcloud", method: "GET", label: "IPcloud" },
    { key: "dcloud", method: "GET", label: "Dcloud" },
];

let tlsInitialized = false;
let session = null;

async function ensureSession() {
    if (!tlsInitialized) {
        await initTLS();
        tlsInitialized = true;
    }
    if (!session) {
        session = new Session({
            clientIdentifier: ClientIdentifier.chrome_131,
            timeout: 15000,
            insecureSkipVerify: false,
        });
    }
    return session;
}

function getHeaders(token, id, isTv, s, e) {
    const referer = isTv
        ? `${BASE_URL}/watch?id=${id}&type=tv&season=${s}&episode=${e}`
        : `${BASE_URL}/watch?id=${id}&type=movie`;

    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": referer,
        "Origin": BASE_URL,
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Priority": "u=1, i",
        "X-Stream-Token": token,
    };
}

function generateToken(id) {
    try {
        const minute = Math.floor(Date.now() / 1000 / 60);
        return crypto
            .createHmac("sha256", HMAC_KEY)
            .update(`${id}:${minute}`)
            .digest("hex");
    } catch {
        return "";
    }
}

function decryptStreamUrl(encoded) {
    if (!encoded) return "";
    if (encoded.startsWith("http://") || encoded.startsWith("https://")) return encoded;
    try {
        const decoded = Buffer.from(encoded, "base64").toString("binary");
        let out = "";
        for (let i = 0; i < decoded.length; i++) {
            out += String.fromCharCode(
                decoded.charCodeAt(i) ^ XOR_KEY.charCodeAt(i % XOR_KEY.length)
            );
        }
        return out;
    } catch {
        return encoded;
    }
}

export async function getStream(args) {
    const { id, s = null, e = null } = args;
    const isTv = s != null && e != null;

    const token = generateToken(id);
    const headers = getHeaders(token, id, isTv, s, e);

    const sess = await ensureSession();

    const requests = SERVERS.map(server => {
        const path = isTv
            ? `/${server.key}/tv/${id}/${s}/${e}`
            : `/${server.key}/movie/${id}`;
        return { url: `${API_BASE}${path}`, server };
    });

    const results = await Promise.allSettled(
        requests.map(async ({ url, server }) => {
            try {
                const res = server.method === "POST"
                    ? await sess.post(url, { headers })
                    : await sess.get(url, { headers });

                if (res.status !== 200) {
                    if (process.env.DEBUG_CAUSE) {
                        console.error(`[CinemaArmy ${server.key}] HTTP ${res.status}`);
                    }
                    return null;
                }

                const text = await res.text();
                let data;
                try {
                    data = JSON.parse(text);
                } catch {
                    return null;
                }

                const encrypted = data?.stream?.url;
                if (!encrypted) return null;

                const streamUrl = decryptStreamUrl(encrypted);
                if (!streamUrl || !/^https?:\/\//.test(streamUrl)) return null;

                return {
                    url: streamUrl,
                    headers: {
                        "User-Agent": UA,
                        Referer: `${BASE_URL}/`,
                        Origin: BASE_URL,
                    },
                    server: `CinemaArmy (${server.label})`,
                    quality: "1080p",
                    type: streamUrl.includes(".m3u8") ? "hls" : "mp4",
                };
            } catch (err) {
                if (process.env.DEBUG_CAUSE) {
                    console.error(`[CinemaArmy ${server.key}] ${err.message}`);
                    if (err.cause) {
                        console.error(`  cause.code:    ${err.cause.code || "n/a"}`);
                        console.error(`  cause.message: ${err.cause.message || err.cause}`);
                    }
                }
                return null;
            }
        })
    );

    const allUrls = results
        .filter(r => r.status === "fulfilled" && r.value)
        .map(r => r.value);

    if (!allUrls.length) return null;

    return { allUrls };
}

export async function cleanup() {
    if (session) {
        try {
            await session.close();
        } catch { }
        session = null;
    }
    if (tlsInitialized) {
        try {
            await destroyTLS();
        } catch { }
        tlsInitialized = false;
    }
}