import { USER_AGENT, fetchJson } from '../utils/helpers.js';

const C_ARRAY = [
    '4Z7lUo', 'gwIVSMD', 'PLmz2elE2v', 'Z4OFV0', 'SZ6RZq6Zc', 'zhJEFYxrz8', 'FOm7b0', 'axHS3q4KDq', 'o9zuXQ', '4Aebt',
    'wgjjWwKKx', 'rY4VIxqSN', 'kfjbnSo', '2DyrFA1M', 'YUixDM9B', 'JQvgEj0', 'mcuFx6JIek', 'eoTKe26gL', 'qaI9EVO1rB', '0xl33btZL',
    '1fszuAU', 'a7jnHzst6P', 'wQuJkX', 'cBNhTJlEOf', 'KNcFWhDvgT', 'XipDGjST', 'PCZJlbHoyt', '2AYnMZkqd', 'HIpJh', 'KH0C3iztrG',
    'W81hjts92', 'rJhAT', 'NON7LKoMQ', 'NMdY3nsKzI', 't4En5v', 'Qq5cOQ9H', 'Y9nwrp', 'VX5FYVfsf', 'cE5SJG', 'x1vj1',
    'HegbLe', 'zJ3nmt4OA', 'gt7rxW57dq', 'clIE9b', 'jyJ9g', 'B5jXjMCSx', 'cOzZBZTV', 'FTXGy', 'Dfh1q1', 'ny9jqZ2POI',
    'X2NnMn', 'MBtoyD', 'qz4Ilys7wB', '68lbOMye', '3YUJnmxp', '1fv5Imona', 'PlfvvXD7mA', 'ZarKfHCaPR', 'owORnX', 'dQP1YU',
    'dVdkx', 'qgiK0E', 'cx9wQ', '5F9bGa', '7UjkKrp', 'Yvhrj', 'wYXez5Dg3', 'pG4GMU', 'MwMAu', 'rFRD5wlM'
];

const DEFAULT_SERVICES = [
    'apex', 'pulse', 'solstice', 'quasar', 'horizon', 'primevids',
    'flowcast', 'asiacloud', 'citadel', 'hindicast', 'guru'
];

function generateRiveSecretKey(e) {
    if (e === undefined || e === null) return 'rive';
    try {
        let t, n;
        const r = String(e);
        if (isNaN(Number(e))) {
            const sum = r.split('').reduce((acc, ch) => acc + ch.charCodeAt(0), 0);
            t = C_ARRAY[sum % C_ARRAY.length] || Buffer.from(r).toString('base64');
            n = Math.floor((sum % r.length) / 2);
        } else {
            const i = Number(e);
            t = C_ARRAY[i % C_ARRAY.length] || Buffer.from(r).toString('base64');
            n = Math.floor((i % r.length) / 2);
        }
        const i = r.slice(0, n) + t + r.slice(n);
        const o = (function (str) {
            const t = String(str);
            let n = 3735928559 ^ t.length;
            for (let e = 0; e < t.length; e++) {
                const r = t.charCodeAt(e);
                const rXor = (131 * e + 89 ^ (r << (e % 5))) & 255;
                const r2 = r ^ rXor;
                n = (((n << 7) | (n >>> 25)) >>> 0) ^ r2;
                const idx = (65535 & n) * 60205;
                const o2 = ((n >>> 16) * 60205) << 16;
                n = (idx + o2) >>> 0;
                n ^= n >>> 11;
            }
            n ^= n >>> 15;
            n = ((65535 & n) * 49842 + (((n >>> 16) * 49842) << 16)) >>> 0;
            n ^= n >>> 13;
            n = ((65535 & n) * 40503 + (((n >>> 16) * 40503) << 16)) >>> 0;
            n ^= n >>> 16;
            n = ((65535 & n) * 10196 + (((n >>> 16) * 10196) << 16)) >>> 0;
            return ((n ^= n >>> 15)).toString(16).padStart(8, '0');
        })(
            (function (str) {
                const s = String(str);
                let t = 0;
                for (let n = 0; n < s.length; n++) {
                    const r = s.charCodeAt(n);
                    const i = (((t = (r + (t << 6) + (t << 16) - t) >>> 0) << (n % 5)) | (t >>> (32 - (n % 5)))) >>> 0;
                    t ^= (i ^ ((r << (n % 7)) | (r >>> (8 - (n % 7))))) >>> 0;
                    t = (t + ((t >>> 11) ^ (t << 3))) >>> 0;
                }
                t ^= t >>> 15;
                t = ((65535 & t) * 49842 + ((((t >>> 16) * 49842) & 65535) << 16)) >>> 0;
                t ^= t >>> 13;
                t = ((65535 & t) * 40503 + ((((t >>> 16) * 40503) & 65535) << 16)) >>> 0;
                return ((t ^= t >>> 16)).toString(16).padStart(8, '0');
            })(i)
        );
        return Buffer.from(o, 'utf-8').toString('base64');
    } catch {
        return 'topSecret';
    }
}

async function fetchRiveServices() {
    try {
        const secretKey = generateRiveSecretKey();
        const url = `https://www.rivestream.app/api/backendfetch?requestID=VideoProviderServices&secretKey=${encodeURIComponent(secretKey)}`;
        const res = await fetchJson(url, {
            headers: {
                'User-Agent': USER_AGENT,
                'Referer': 'https://www.rivestream.app/',
                'Accept': 'application/json, text/plain, */*'
            },
            signal: AbortSignal.timeout(6000)
        });
        return Array.isArray(res?.data) && res.data.length ? res.data : DEFAULT_SERVICES;
    } catch {
        return DEFAULT_SERVICES;
    }
}

async function fetchServiceStreams(service, isMovie, id, s, e, secretKey, referer) {
    try {
        const cb = (service === 'primevids' || service === 'citadel')
            ? `&cb=${Math.floor(Date.now() / 3e6)}`
            : '';

        const reqID = isMovie ? 'movieVideoProvider' : 'tvVideoProvider';
        const params = isMovie
            ? `requestID=${reqID}&id=${encodeURIComponent(id)}&service=${service}${cb}&secretKey=${encodeURIComponent(secretKey)}`
            : `requestID=${reqID}&id=${encodeURIComponent(id)}&season=${encodeURIComponent(s)}&episode=${encodeURIComponent(e)}&service=${service}${cb}&secretKey=${encodeURIComponent(secretKey)}`;

        const url = `https://www.rivestream.app/api/backendfetch?${params}`;
        const res = await fetchJson(url, {
            headers: {
                'User-Agent': USER_AGENT,
                'Referer': referer,
                'Accept': 'application/json, text/plain, */*'
            },
            signal: AbortSignal.timeout(10000)
        });

        if (!res?.data?.sources || !Array.isArray(res.data.sources)) return [];

        const streams = [];
        for (const item of res.data.sources) {
            if (!item || !item.url) continue;

            const format = String(item.format || '').toLowerCase();
            const urlStr = String(item.url || '').toLowerCase();
            const isM3U8 = format === 'hls' || format === 'm3u8' || urlStr.includes('.m3u8') || urlStr.includes('.txt');

            streams.push({
                url: item.url,
                server: `Rive (${item.source || service})`,
                quality: String(item.quality || 'Auto'),
                type: isM3U8 ? 'hls' : 'mp4',
                headers: {
                    'User-Agent': USER_AGENT,
                    'Referer': referer
                }
            });
        }
        return streams;
    } catch {
        return [];
    }
}

export async function getStream(args) {
    const { id, s, e, server } = args;
    const isTv = s != null && e != null;
    const isMovie = !isTv;

    const secretKey = generateRiveSecretKey(id);
    const referer = isMovie
        ? `https://www.rivestream.app/watch?type=movie&id=${id}`
        : `https://www.rivestream.app/watch?type=tv&id=${id}&season=${s}&episode=${e}`;

    let services = await fetchRiveServices();

    if (server && server !== 'all') {
        const clean = server.toLowerCase().replace('rive (', '').replace(')', '').replace('rive', '').trim();
        services = services.filter(srv => srv.toLowerCase().includes(clean));
        if (!services.length) services = await fetchRiveServices();
    }

    const settled = await Promise.allSettled(
        services.map(srv => fetchServiceStreams(srv, isMovie, id, s, e, secretKey, referer))
    );

    const allUrls = [];
    for (const r of settled) {
        if (r.status === 'fulfilled' && Array.isArray(r.value)) {
            allUrls.push(...r.value);
        }
    }

    return allUrls.length ? { allUrls } : null;
}

export async function getSources() {
    const services = await fetchRiveServices();
    return services.map(s => `Rive (${s})`);
}