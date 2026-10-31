import { getUA } from '../utils/helpers.js';

const BASE_URL = 'https://streamaggregator.in';

async function resolveDetails(sdk, id, isTv) {
    const apiKey = sdk?.tmdbApiKey || process.env.TMDB_API_KEY;
    if (!apiKey) return null;
    const type = isTv ? 'tv' : 'movie';
    try {
        const res = await fetch(`https://api.themoviedb.org/3/${type}/${id}?api_key=${apiKey}`, {
            signal: AbortSignal.timeout(6000),
        });
        if (!res.ok) return null;
        const data = await res.json();
        const title = isTv ? data.name : data.title;
        const releaseDate = isTv ? data.first_air_date : data.release_date;
        const year = releaseDate ? releaseDate.split('-')[0] : null;
        return { title, year };
    } catch {
        return null;
    }
}

export async function getStream({ id, s, e, sdk }) {
    const isTv = Boolean(s && e);
    const mediaType = isTv ? 'tv' : 'movie';
    const ua = getUA();

    const details = await resolveDetails(sdk, id, isTv);
    const title = details?.title || String(id);
    const year = details?.year || '';

    const formattedTitle = encodeURIComponent(title).replaceAll('%20', '+');
    const params = new URLSearchParams({
        type: mediaType,
        stream: 'true',
    });

    if (year) params.set('year', String(year));
    if (isTv) {
        params.set('season', String(s));
        params.set('episode', String(e));
    }

    const scrapeUrl = `${BASE_URL}/api/scrape?title=${formattedTitle}&${params.toString()}`;

    const res = await fetch(scrapeUrl, {
        headers: {
            'User-Agent': ua,
            Referer: `${BASE_URL}/`,
            Origin: BASE_URL,
            Accept: 'text/event-stream, application/x-ndjson, text/plain, */*',
        },
        signal: AbortSignal.timeout(90000),
    });

    if (!res.ok) return null;

    const lines = [];
    if (res.body?.getReader) {
        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const parts = buffer.split('\n');
                buffer = parts.pop() || '';
                for (const part of parts) {
                    const trimmed = part.trim();
                    if (trimmed) lines.push(trimmed);
                }
            }
        } catch { }
        if (buffer.trim()) lines.push(buffer.trim());
    } else {
        const text = await res.text();
        for (const line of text.split('\n')) {
            const trimmed = line.trim();
            if (trimmed) lines.push(trimmed);
        }
    }

    const seenUrls = new Set();
    const allUrls = [];

    for (const line of lines) {
        try {
            const raw = line.startsWith('data:') ? line.slice(5).trim() : line;
            if (!raw || raw === '[DONE]') continue;
            const item = JSON.parse(raw);
            const streamUrl = String(item?.url || '').trim();
            if (!streamUrl || !streamUrl.startsWith('http') || seenUrls.has(streamUrl)) continue;

            seenUrls.add(streamUrl);

            const serverName = item.server || item.providerKey || 'Stream';
            const quality = item.quality ? ` · ${item.quality}` : '';
            const size = item.size ? ` (${item.size})` : '';
            const label = `${serverName}${quality}${size}`;

            const headers = {
                'User-Agent': ua,
                ...(item.headers && typeof item.headers === 'object' ? item.headers : {}),
            };

            if (item.referer) {
                headers.Referer = item.referer;
            }

            allUrls.push({
                url: streamUrl,
                label,
                headers,
            });
        } catch { }
    }

    if (allUrls.length === 0) return null;

    return {
        url: allUrls[0].url,
        allUrls,
        headers: allUrls[0].headers,
    };
}