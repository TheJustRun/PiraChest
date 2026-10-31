import { getUA } from "../utils/helpers.js";

const API_BASE = "https://api.movenerds.space/api";

function getHeaders() {
    return {
        "User-Agent": getUA(),
        Accept: "application/json",
        Referer: "https://movenerds.space/",
        Origin: "https://movenerds.space",
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

async function getMeta(id, isTv, apiKey) {
    if (!apiKey) return null;
    const type = isTv ? "tv" : "movie";
    try {
        const res = await fetch(
            `https://api.themoviedb.org/3/${type}/${id}?api_key=${apiKey}`,
            { signal: AbortSignal.timeout(5000) }
        );
        if (!res.ok) return null;
        const data = await res.json();
        const title = isTv ? data.name : data.title;
        const date = isTv ? data.first_air_date : data.release_date;
        const year = date ? parseInt(date.slice(0, 4), 10) : null;
        return { title, year };
    } catch {
        return null;
    }
}

function mapSource(source) {
    if (!source || !source.url) return null;
    if (source.type !== "hls" && source.type !== "mp4") return null;
    if (!/^https?:\/\//.test(source.url)) return null;

    return {
        url: source.url,
        headers: {
            "User-Agent": source.headers?.["User-Agent"] || getUA(),
            Referer: source.headers?.Referer || "https://movenerds.space/",
            Origin: source.headers?.Origin || "https://movenerds.space",
        },
        server: source.name || source.providerName || "MoverNerds",
        quality: source.quality || "1080p",
        type: source.type,
        skipProxy: true,
    };
}

export async function getStream(args) {
    const { id, s = null, e = null } = args;
    const isTv = s != null && e != null;

    const apiKey = resolveApiKey(args);
    const meta = await getMeta(id, isTv, apiKey);

    if (!meta?.title) {
        if (process.env.DEBUG_CAUSE) {
            console.error(
                `[movenerds] Could not resolve title for ${isTv ? "tv" : "movie"} ${id} ` +
                `(tmdbApiKey ${apiKey ? "present" : "MISSING"})`
            );
        }
        return null;
    }

    const params = new URLSearchParams({
        tmdbId: String(id),
        type: isTv ? "tv" : "movie",
        title: meta.title,
    });
    if (meta.year) params.set("year", String(meta.year));
    if (isTv) {
        params.set("season", String(s));
        params.set("episode", String(e));
        params.set("language", "en");
    }

    const url = `${API_BASE}/stream?${params.toString()}`;

    try {
        const res = await fetch(url, {
            headers: getHeaders(),
            signal: AbortSignal.timeout(20000),
        });

        if (!res.ok) {
            if (process.env.DEBUG_CAUSE) {
                console.error(`[movenerds] HTTP ${res.status} for ${url}`);
            }
            return null;
        }

        const data = await res.json();
        const primary = Array.isArray(data?.primarySources) ? data.primarySources : [];
        if (!primary.length) {
            if (process.env.DEBUG_CAUSE) {
                console.error(`[movenerds] No primarySources in response for ${url}`);
            }
            return null;
        }

        const allUrls = primary.map(mapSource).filter(Boolean);
        if (!allUrls.length) return null;

        return { allUrls };
    } catch (err) {
        if (process.env.DEBUG_CAUSE) {
            console.error(`[movenerds] fetch error for ${url}:`, err.message);
            if (err.cause) console.error("  cause:", err.cause.code, err.cause.message);
        }
        return null;
    }
}