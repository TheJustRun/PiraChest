import { fetchJson, USER_AGENT, getTmdbInfo } from '../utils/helpers.js';

const DOMAIN = 'https://purstream.ad';
const API_BASE = 'https://api.purstream.ad/api/v1';

const HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'application/json, text/plain, */*',
    'Referer': `${DOMAIN}/`,
    'Origin': DOMAIN,
    'X-Requested-With': 'XMLHttpRequest',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-site'
};

export async function getStream({ id, s, e }) {
    try {
        const isTv = s != null;

        const info = await getTmdbInfo(id, isTv ? 'tv' : 'movie');


        if (!info?.titles?.length) {
            return null;
        }

        const title = info.titles[0];

        const searchUrl = `${API_BASE}/search-bar/search/${encodeURIComponent(title)}`;

        const searchData = await fetchJson(searchUrl, {
            headers: HEADERS,
            signal: AbortSignal.timeout(10000)
        });

        const items = searchData?.data?.items?.movies?.items || [];

        const type = isTv ? 'tv' : 'movie';
        const lowerTitle = title.toLowerCase();

        let match = items.find(item =>
            item.type === type &&
            item.title?.toLowerCase() === lowerTitle &&
            (!info.year || item.release_date?.startsWith(String(info.year)))
        );

        if (!match) {
            match = items.find(item => item.type === type);
        }

        if (!match?.id) {
            return null;
        }

        const streamUrl = isTv
            ? `${API_BASE}/stream/${match.id}/episode?season=${s}&episode=${e}`
            : `${API_BASE}/stream/${match.id}`;

        const json = await fetchJson(streamUrl, {
            headers: HEADERS,
            signal: AbortSignal.timeout(10000)
        });

        const sources = json?.data?.items?.sources;

        if (json?.type !== 'success' || !Array.isArray(sources) || !sources.length) {
            return null;
        }

        const chosen = sources.find(src => src.stream_url);

        if (!chosen?.stream_url) {
            return null;
        }

        const result = {
            url: chosen.stream_url,
            headers: {
                'User-Agent': USER_AGENT,
                'Referer': `${DOMAIN}/`
            },
            server: 'purstream',
            quality: chosen.source_name || 'Auto',
            type: chosen.format || 'hls',
            skipProxy: true
        };

        return result;

    } catch (err) {
        return null;
    }
}

export async function getSources(args) {
    const stream = await getStream(args);

    return stream ? [stream] : [];
}

export const VERIFY_HEADERS = {
    ...HEADERS
};