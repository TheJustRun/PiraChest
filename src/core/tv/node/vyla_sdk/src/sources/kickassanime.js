import { fetchJson, USER_AGENT } from '../utils/helpers.js';

const BASE = "https://kaa.lt";
const HLS_BASE = "https://hls.krussdomi.com/manifest";
const HEADERS = { "User-Agent": USER_AGENT, Accept: "application/json" };

async function kaaSearch(query) {
    const res = await fetchJson(`${BASE}/api/fsearch`, {
        method: "POST",
        headers: { ...HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify({ page: 1, query }),
    }).catch(() => null);
    return Array.isArray(res?.result) ? res.result : [];
}

async function kaaShowInfo(showSlug) {
    return fetchJson(`${BASE}/api/show/${showSlug}`, { headers: HEADERS }).catch(() => null);
}

async function kaaEpisodePage(showSlug, ep) {
    return fetchJson(`${BASE}/api/show/${showSlug}/episodes?ep=${ep}&lang=ja-JP`, { headers: HEADERS }).catch(() => null);
}

async function kaaAllEpisodes(showSlug) {
    const first = await kaaEpisodePage(showSlug, 1);
    if (!first) return [];
    const pages = Array.isArray(first.pages) ? first.pages : [];
    const all = Array.isArray(first.result) ? [...first.result] : [];

    if (pages.length > 1) {
        const rest = await Promise.all(
            pages.slice(1).map(async (pg) => {
                const startEp = pg.eps?.[0];
                if (!startEp) return [];
                const d = await kaaEpisodePage(showSlug, startEp);
                return Array.isArray(d?.result) ? d.result : [];
            })
        );
        for (const batch of rest) all.push(...batch);
    }

    return all;
}

async function kaaEpisodeServers(showSlug, fullEpSlug) {
    return fetchJson(`${BASE}/api/show/${showSlug}/episode/${fullEpSlug}`, { headers: HEADERS }).catch(() => null);
}

export async function getStream({ id, s, e, tmdbApiKey, sdk }) {
    try {
        const apiKey = tmdbApiKey || sdk?.tmdbApiKey || process.env.TMDB_API_KEY || '45dbdd5e714da212424bda0336167447';
        const isTv = !!s;
        const mediaType = isTv ? 'tv' : 'movie';
        const epNum = e ? Number(e) : 1;

        let titles = [];
        try {
            const tmdbRes = await fetchJson(`https://api.themoviedb.org/3/${mediaType}/${id}?api_key=${apiKey}&append_to_response=alternative_titles`);
            if (tmdbRes) {
                const primary = tmdbRes.title || tmdbRes.name;
                const original = tmdbRes.original_title || tmdbRes.original_name;
                if (primary) titles.push(primary);
                if (original && !titles.includes(original)) titles.push(original);

                const alts = tmdbRes.alternative_titles?.titles || tmdbRes.alternative_titles?.results || [];
                for (const alt of alts) {
                    const candidate = alt.title || alt.name;
                    if (candidate && !titles.includes(candidate)) {
                        titles.push(candidate);
                    }
                }
            }
        } catch { }

        if (!titles.length) return null;

        let showSlug = null;
        for (const title of titles) {
            const results = await kaaSearch(title);
            if (results.length) {
                showSlug = results[0].slug;
                break;
            }
        }

        if (!showSlug) return null;

        const showInfo = await kaaShowInfo(showSlug);
        let fullEpSlug = null;

        if (showInfo?.type === "movie") {
            const m = (showInfo.watch_uri || "").match(/\/(ep-(\d+)-([a-f0-9]+))$/i);
            if (m) fullEpSlug = m[1];
        } else {
            const episodes = await kaaAllEpisodes(showSlug);
            const epItem = episodes.find((item) => item.episode_number === epNum);
            if (epItem) fullEpSlug = `ep-${epItem.episode_number}-${epItem.slug}`;
        }

        if (!fullEpSlug) return null;

        const episodeData = await kaaEpisodeServers(showSlug, fullEpSlug);
        const servers = Array.isArray(episodeData?.servers) ? episodeData.servers : [];

        const urls = [];
        for (const srv of servers) {
            if (!srv.src) continue;
            const m = srv.src.match(/[?&]id=([^&]+)/);
            if (m) {
                urls.push({
                    url: `${HLS_BASE}/${m[1]}/master.m3u8`,
                    server: `KickAssAnime (${srv.name || 'Server'})`,
                });
            }
        }

        return urls.length ? { allUrls: urls } : null;
    } catch (err) {
        return null;
    }
}

export async function getSources() {
    return ['KickAssAnime'];
}