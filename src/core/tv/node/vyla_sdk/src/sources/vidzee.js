import { USER_AGENT, fetchJson } from '../utils/helpers.js';

const BASE_URL = 'https://core.vidzee.wtf';
const PLAYER_URL = 'https://player.vidzee.wtf';
const SERVICES = ['dcloud', 'ipcloud', 'tik', 'v6:Hindi', '3AHindi'];
const WASM_B64 = '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';

let wasmInstanceCache = null;

async function getWasmInstance() {
    if (wasmInstanceCache) return wasmInstanceCache;
    try {
        const wasmBuffer = Buffer.from(WASM_B64, 'base64');
        const wasmModule = await WebAssembly.compile(wasmBuffer);

        let memory;
        let exports;

        const env = {
            abort() {
                throw new Error('WASM abort');
            }
        };

        const res = await WebAssembly.instantiate(wasmModule, { env });
        exports = res.exports;
        memory = exports.memory;

        const refMap = new Map();
        function pin(ptr) {
            if (ptr) {
                const count = refMap.get(ptr);
                if (count) refMap.set(ptr, count + 1);
                else refMap.set(exports.__pin(ptr), 1);
            }
            return ptr;
        }
        function unpin(ptr) {
            if (ptr) {
                const count = refMap.get(ptr);
                if (count === 1) {
                    exports.__unpin(ptr);
                    refMap.delete(ptr);
                } else if (count) {
                    refMap.set(ptr, count - 1);
                }
            }
        }

        wasmInstanceCache = {
            decrypt(encryptedBytes, keyStr) {
                let dataPtr = 0;
                let keyPtr = 0;
                try {
                    const len = encryptedBytes.length;
                    const pinPtr = exports.__pin(exports.__new(len, 1)) >>> 0;
                    dataPtr = exports.__new(12, 6) >>> 0;

                    const view = new DataView(memory.buffer);
                    view.setUint32(dataPtr, pinPtr, true);
                    view.setUint32(dataPtr + 4, pinPtr, true);
                    view.setUint32(dataPtr + 8, len, true);
                    new Uint8Array(memory.buffer, pinPtr, len).set(encryptedBytes);
                    exports.__unpin(pinPtr);

                    pin(dataPtr);

                    const kLen = keyStr.length;
                    keyPtr = exports.__new(kLen << 1, 2) >>> 0;
                    const u16View = new Uint16Array(memory.buffer);
                    for (let i = 0; i < kLen; i++) {
                        u16View[(keyPtr >>> 1) + i] = keyStr.charCodeAt(i);
                    }

                    const resPtr = exports.decrypt(dataPtr, keyPtr) >>> 0;
                    if (!resPtr) return new Uint8Array(0);

                    const resView = new DataView(memory.buffer);
                    const outPtr = resView.getUint32(resPtr + 4, true);
                    const outLen = resView.getUint32(resPtr + 8, true);
                    return new Uint8Array(memory.buffer, outPtr, outLen).slice();
                } finally {
                    unpin(dataPtr);
                }
            }
        };
        return wasmInstanceCache;
    } catch {
        return null;
    }
}

async function requestEndpoint(url, headers) {
    try {
        return await fetchJson(url, {
            headers,
            signal: AbortSignal.timeout(6000)
        });
    } catch {
        return null;
    }
}

async function resolveServiceStream(service, isMovie, id, s, e, headers) {
    const directEndpoint = isMovie
        ? `${BASE_URL}/streams/movie/${id}?s=${service}`
        : `${BASE_URL}/streams/tv/${id}/${s || 1}/${e || 1}?s=${service}`;

    let res = await requestEndpoint(directEndpoint, headers);

    if (!res || !res.url) {
        const encEndpoint = `${directEndpoint}&e=1`;
        const encRes = await requestEndpoint(encEndpoint, headers);

        if (encRes && encRes.c) {
            const instance = await getWasmInstance();
            if (instance) {
                const encryptedBytes = new Uint8Array(Buffer.from(encRes.c, 'base64'));
                const decryptedBytes = instance.decrypt(encryptedBytes, 'player.vidzee.wtf');
                if (decryptedBytes.length > 0) {
                    const str = new TextDecoder().decode(decryptedBytes);
                    try {
                        res = JSON.parse(str);
                    } catch { }
                }
            }
        }
    }

    if (res && res.url) {
        const format = res.url.includes('.m3u8') ? 'hls' : 'mp4';
        return {
            url: res.url,
            server: `VidZee (${service})`,
            quality: res.language || 'Auto',
            type: format,
            headers: {
                'User-Agent': USER_AGENT,
                'Referer': `${PLAYER_URL}/`,
                'Origin': PLAYER_URL,
                ...(res.headers || {})
            }
        };
    }
    return null;
}

export async function getStream(args) {
    const { id, s, e, server, clientIP } = args;
    const isTv = s != null && e != null;
    const isMovie = !isTv;

    const headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'application/json, text/plain, */*',
        'Referer': `${PLAYER_URL}/`,
        'Origin': PLAYER_URL,
        ...(clientIP && { 'X-Forwarded-For': clientIP })
    };

    let targets = SERVICES;
    if (server && server !== 'all') {
        const clean = server.toLowerCase().replace('vidzee (', '').replace(')', '').replace('vidzee', '').trim();
        targets = SERVICES.filter(srv => srv.toLowerCase().includes(clean));
        if (!targets.length) targets = SERVICES;
    }

    const settled = await Promise.allSettled(
        targets.map(service => resolveServiceStream(service, isMovie, id, s, e, headers))
    );

    const allUrls = [];
    for (const r of settled) {
        if (r.status === 'fulfilled' && r.value) {
            allUrls.push(r.value);
        }
    }

    return allUrls.length ? { allUrls } : null;
}

export async function getSources() {
    return SERVICES.map(s => `VidZee (${s})`);
}