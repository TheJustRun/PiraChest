import { fetchJson, fetchText, getTmdbInfo } from '../utils/helpers.js';

const K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174
];
const MAGIC = [109, 118, 109, 49];

const isEven = e => ((e * (e + 1)) & 1) === 0;

function scramble(e) {
    e >>>= 0;
    e ^= e >>> 16;
    e = Math.imul(e, 0x85ebca6b) >>> 0;
    e ^= e >>> 13;
    e = Math.imul(e, 0xc2b2ae35) >>> 0;
    return (e ^ (e >>> 16)) >>> 0;
}

function rotl(e, a) {
    e >>>= 0;
    a &= 31;
    return a === 0 ? e >>> 0 : ((e << a) | (e >>> (32 - a))) >>> 0;
}

function initCipherState(keyStr, seedStr) {
    let keyLen = keyStr.length;
    if (((keyLen * (keyLen + 1)) & 1) === 1) {
        const S = new Array(256);
        for (let i = 0; i < 256; i++) S[i] = i;
        let j = 0;
        for (let i = 0; i < 256; i++) {
            j = (j + S[i] + keyStr.charCodeAt(i % keyStr.length)) & 255;
            const tmp = S[i];
            S[i] = S[j];
            S[j] = tmp;
        }
        let acc = 0x67452301;
        for (let i = 0; i < keyStr.length; i++) {
            acc = rotl((acc ^ Math.imul(keyStr.charCodeAt(i), K[15 & i])) >>> 0, 5);
        }
        return { S, acc: scramble(acc) };
    }

    const S = new Array(61);
    let hash = 0x811c9dc5;
    for (let i = 0; i < keyStr.length; i++) {
        hash = Math.imul(hash ^ keyStr.charCodeAt(i), 0x1000193) >>> 0;
    }
    let n = scramble(scramble(hash) ^ scramble((seedStr >>> 0) ^ 0x9e3779b9)) >>> 0;
    for (let i = 0; i < 8; i++) {
        if (isEven(i)) {
            const idx = n % 61;
            n = rotl((n + 0x9e3779b9) >>> 0, 7 + (7 & i));
            S[idx] = (n ^ scramble(n)) >>> 0;
            n = scramble((n + idx) >>> 0);
        } else {
            S[i] = K[15 & i];
        }
    }
    return {
        S,
        acc: scramble(0xa5a5a5a5 ^ n) >>> 0
    };
}

function nextKeystreamWord(state, step) {
    const S = state.S;
    let acc = state.acc;
    const idx = acc % 61;
    const hasIdx = 0 - Number(idx in S);
    const sVal = S[idx] >>> 0;
    const c = Math.imul(0x9e3779b9, step + 1) >>> 0;
    let b = ((acc ^ ((sVal ^ c) >>> 0)) >>> 0 | (acc & (sVal ^ c) & hasIdx) >>> 0) >>> 0;
    b = (rotl((b + acc) >>> 0, 31 & idx) ^ rotl(acc, 31 & Math.imul(idx, 7))) >>> 0;
    acc = scramble((b + 0x9e3779b9) >>> 0);
    S[idx] = acc >>> 0;
    state.acc = acc;
    return acc >>> 0;
}

function decodeBase64Url(str) {
    const padded = str.replace(/-/g, "+").replace(/_/g, "/").padEnd(4 * Math.ceil(str.length / 4), "=");
    const bin = atob(padded);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
}

function decryptPayload(ciphertext, seed, mediaId) {
    const rawBytes = decodeBase64Url(ciphertext);
    const totalLen = rawBytes.length;
    const state = initCipherState(seed, mediaId);
    const keystream = new Uint8Array(totalLen);

    let byteIdx = 0;
    let step = 0;
    while (byteIdx < totalLen) {
        const word = nextKeystreamWord(state, step++);
        keystream[byteIdx++] = 255 & word;
        if (byteIdx < totalLen) keystream[byteIdx++] = (word >>> 8) & 255;
        if (byteIdx < totalLen) keystream[byteIdx++] = (word >>> 16) & 255;
        if (byteIdx < totalLen) keystream[byteIdx++] = (word >>> 24) & 255;
    }

    for (let i = 0; i < totalLen; i++) {
        rawBytes[i] ^= keystream[i];
    }

    for (let i = 0; i < MAGIC.length; i++) {
        if (rawBytes[i] !== MAGIC[i]) {
            throw new Error("decrypt failed: bad seed or tampered payload");
        }
    }

    const payload = rawBytes.subarray(MAGIC.length);
    return new TextDecoder("utf-8").decode(payload);
}

export async function getStream({ id, s, e, tmdbApiKey, sdk }) {
    const isTv = s != null && e != null;
    const type = isTv ? 'tv' : 'movie';
    const apiKey = tmdbApiKey || sdk?.tmdbApiKey || process.env.TMDB_API_KEY || '45dbdd5e714da212424bda0336167447';

    const headers = {
        'Accept': '*/*',
        'Origin': 'https://www.movy.sx',
        'Referer': 'https://www.movy.sx/',
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1'
    };

    let title = '';
    let year = '';
    let imdbId = '';

    try {
        const info = await getTmdbInfo(apiKey, id, type, s);
        title = info?.titles?.[0] || '';
        year = info?.year || '';
        imdbId = info?.imdbId || '';
    } catch { }

    if (!title) {
        try {
            const tmdbRes = await fetchJson(`https://api.themoviedb.org/3/${type}/${id}?api_key=${apiKey}&append_to_response=external_ids`);
            title = tmdbRes?.title || tmdbRes?.name || '';
            year = (tmdbRes?.release_date || tmdbRes?.first_air_date || '').split('-')[0] || '';
            imdbId = tmdbRes?.imdb_id || tmdbRes?.external_ids?.imdb_id || '';
        } catch { }
    }

    if (!title) return null;

    const seedRes = await fetchJson(`https://api.wecollege.net/seed?mediaId=${id}`, { headers }).catch(() => null);
    const seed = seedRes?.seed;
    if (!seed) return null;

    const servers = ['miami', 'boise', 'vegas', 'chicago', 'reno', 'seattle'];
    const allUrls = [];

    for (const srv of servers) {
        const params = new URLSearchParams({
            title,
            mediaType: type,
            year: String(year),
            episodeId: String(e || 1),
            seasonId: String(s || 1),
            tmdbId: String(id),
            imdbId: String(imdbId || ''),
            enc: '2',
            seed
        });

        const url = `https://api.wecollege.net/${srv}/sources?${params.toString()}`;
        const rawEnc = await fetchText(url, { headers }).catch(() => null);
        if (!rawEnc || rawEnc.length < 20 || rawEnc.startsWith('{') || rawEnc.startsWith('<')) continue;

        try {
            const dec = decryptPayload(rawEnc.trim(), seed, id);
            const parsed = JSON.parse(dec);

            if (Array.isArray(parsed?.sources)) {
                for (const src of parsed.sources) {
                    if (src?.url) {
                        allUrls.push({
                            url: src.url,
                            server: `Movy - ${srv.toUpperCase()} (${src.quality || 'Auto'})`,
                            headers: {
                                Referer: 'https://www.movy.sx/',
                                Origin: 'https://www.movy.sx'
                            }
                        });
                    }
                }
            }
        } catch { }

        if (allUrls.length) break;
    }

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}