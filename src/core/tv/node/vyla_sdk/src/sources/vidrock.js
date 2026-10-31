import { webcrypto } from 'node:crypto';
import { fetchJson, USER_AGENT } from '../utils/helpers.js';

const GCM_HEX_KEY = '7f3e9c2a8b5d1f4e6a9c3b7d2e5f8a1c4b6d9e2f5a8c1b4d7e9f2a5c8b1d4e7f';
const BASE_URL = 'https://vidrock.ru/';
const PROXY_PREFIX = 'https://proxy.vidrock.store/';

let cryptoKey = null;

function hexToBytes(hex) {
    const bytes = new Uint8Array(hex.length / 2);

    for (let i = 0; i < bytes.length; i++) {
        bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    }

    return bytes;
}

function base64UrlToBytes(value) {
    let base64 = value.replace(/-/g, '+').replace(/_/g, '/');

    const padding = base64.length % 4;

    if (padding === 2) {
        base64 += '==';
    } else if (padding === 3) {
        base64 += '=';
    } else if (padding === 1) {
        throw new Error('Invalid base64url');
    }

    return new Uint8Array(Buffer.from(base64, 'base64'));
}

async function getKey() {
    if (cryptoKey) return cryptoKey;

    const keyBytes = hexToBytes(GCM_HEX_KEY);

    cryptoKey = await webcrypto.subtle.importKey(
        'raw',
        keyBytes,
        'AES-GCM',
        false,
        ['decrypt']
    );

    return cryptoKey;
}

async function decryptStreamUrl(value) {
    try {
        const data = base64UrlToBytes(value);

        if (data.length < 28) {
            throw new Error('Ciphertext too short');
        }

        const iv = data.slice(0, 12);
        const ciphertext = data.slice(12);

        const key = await getKey();

        const decrypted = await webcrypto.subtle.decrypt(
            {
                name: 'AES-GCM',
                iv
            },
            key,
            ciphertext
        );

        return new TextDecoder().decode(decrypted);
    } catch (error) {
        return null;
    }
}

export async function getStream({ id, s, e }) {
    try {
        const path = s
            ? `tv/${id}/${s}/${e || 1}`
            : `movie/${id}`;

        const apiData = await fetchJson(`${BASE_URL}api/${path}`, {
            headers: {
                'User-Agent': USER_AGENT,
                Referer: BASE_URL,
                Origin: BASE_URL
            }
        });

        if (!apiData || typeof apiData !== 'object') {
            return null;
        }

        const streams = [];

        for (const [provider, info] of Object.entries(apiData)) {
            if (!info?.url) {
                continue;
            }

            const decryptedUrl = await decryptStreamUrl(info.url);

            if (!decryptedUrl) {
                continue;
            }

            let url = decryptedUrl;

            if (url.startsWith(PROXY_PREFIX)) {
                url = decodeURIComponent(
                    url.slice(PROXY_PREFIX.length).replace(/^\/+/, '')
                );
            }

            const isXpass = url.includes('xpass.top');

            streams.push({
                name: provider,
                url,
                file: url,
                stream: url,
                src: url,
                quality: info.type || 'auto',
                type: info.type || 'hls',
                headers: {
                    'User-Agent': USER_AGENT,
                    Referer: isXpass
                        ? 'https://play.xpass.top/'
                        : BASE_URL,
                    Origin: isXpass
                        ? 'https://play.xpass.top'
                        : 'https://vidrock.ru'
                }
            });
        }

        return streams.length ? { streams } : null;
    } catch (error) {
        return null;
    }
}