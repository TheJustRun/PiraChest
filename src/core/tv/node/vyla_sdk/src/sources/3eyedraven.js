import { getUA } from "../utils/helpers.js";

const BASE_URL = "https://3eyedraven.watch";
const API_BASE = "https://3eyedraven.watch/api";

function getHeaders() {
    return {
        "User-Agent": getUA(),
        Accept: "application/json",
        Referer: `${BASE_URL}/`,
        Origin: BASE_URL,
    };
}

function resolveApiKey(args) {
    return (
        args?.tmdbApiKey ||
        args?.sdk?.tmdbApiKey ||
        process.env.TMDB_API_KEY ||
        null
    );
}

async function getTitle(id, isTv, apiKey) {
    if (!apiKey) return null;
    const type = isTv ? "tv" : "movie";
    try {
        const res = await fetch(
            `https://api.themoviedb.org/3/${type}/${id}?api_key=${apiKey}`,
            { signal: AbortSignal.timeout(5000) }
        );
        if (!res.ok) return null;
        const data = await res.json();
        return isTv ? data.name : data.title;
    } catch {
        return null;
    }
}

function slugify(text) {
    return text
        .toLowerCase()
        .normalize("NFKD")
        .replace(/[\u0300-\u036f]/g, "")
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, "");
}

function absolute(url) {
    if (!url) return "";
    if (/^https?:\/\//.test(url)) return url;
    return `${BASE_URL}${url.startsWith("/") ? "" : "/"}${url}`;
}

export async function getStream(args) {
    const { id, s = null, e = null } = args;
    const isTv = s != null && e != null;

    const apiKey = resolveApiKey(args);
    const title = await getTitle(id, isTv, apiKey);

    if (!title) {
        if (process.env.DEBUG_CAUSE) {
            console.error(
                `[3eyedraven] Could not resolve title for ${isTv ? "tv" : "movie"} ${id} ` +
                `(tmdbApiKey ${apiKey ? "present" : "MISSING"})`
            );
        }
        return null;
    }

    const identifier = `${slugify(title)}-${id}`;

    const url = isTv
        ? `${API_BASE}/stream/${identifier}?season=${s}&episode=${e}`
        : `${API_BASE}/stream/${identifier}`;

    try {
        const res = await fetch(url, {
            headers: getHeaders(),
            signal: AbortSignal.timeout(15000),
        });

        if (!res.ok) {
            if (process.env.DEBUG_CAUSE) {
                console.error(`[3eyedraven] HTTP ${res.status} for ${url}`);
            }
            return null;
        }

        const data = await res.json();
        const stream = data?.stream;
        if (!stream?.url) {
            if (process.env.DEBUG_CAUSE) {
                console.error(`[3eyedraven] No stream.url in response for ${url}`);
            }
            return null;
        }

        const streamUrl = absolute(stream.url);
        if (!/^https?:\/\//.test(streamUrl)) return null;

        const subtitles = (stream.subtitles || [])
            .map(sub => {
                const subUrl = absolute(sub.url);
                if (!subUrl) return null;
                return {
                    label: sub.label || sub.language || "Subtitle",
                    file: subUrl,
                    type: "vtt",
                    source: "3eyedraven",
                };
            })
            .filter(Boolean);

        return {
            url: streamUrl,
            headers: {
                "User-Agent": getUA(),
                Referer: `${BASE_URL}/`,
                Origin: BASE_URL,
            },
            server: "3eyedraven",
            quality: "1080p",
            type: stream.format === "hls" ? "hls" : "mp4",
            subtitles,
            skipProxy: true,
        };
    } catch (err) {
        if (process.env.DEBUG_CAUSE) {
            console.error(`[3eyedraven] fetch error for ${url}:`, err.message);
            if (err.cause) console.error("  cause:", err.cause.code, err.cause.message);
        }
        return null;
    }
}