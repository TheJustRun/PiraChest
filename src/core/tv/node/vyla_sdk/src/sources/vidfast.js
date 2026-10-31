import { getUA } from '../utils/helpers.js';

const BASE_URL = 'https://vidfast.vc';
const API = 'https://enc-dec.app/api';

export async function getStream({ id, s, e }) {
    const isTv = Boolean(s && e);
    const targetUrl = isTv
        ? `${BASE_URL}/tv/${id}/${s}/${e}/`
        : `${BASE_URL}/movie/${id}`;

    const ua = getUA();
    const baseHeaders = {
        'User-Agent': ua,
        'Referer': `${BASE_URL}/`,
        'X-Requested-With': 'XMLHttpRequest',
    };

    try {
        const pageRes = await fetch(targetUrl, {
            headers: { 'User-Agent': ua },
            signal: AbortSignal.timeout(10000),
        });
        if (!pageRes.ok) return null;
        const html = await pageRes.text();

        const match = html.match(/\\"(?:en|token)\\":\\"(.*?)\\"/);
        if (!match || !match[1]) return null;
        const enToken = match[1];

        const s1Res = await fetch(`${API}/enc-vidfast?text=${encodeURIComponent(enToken)}&stage=1`, {
            signal: AbortSignal.timeout(8000),
        });
        const s1Data = await s1Res.json();
        if (s1Data?.status !== 200 || !s1Data?.result?.stage1) return null;

        const token1 = s1Data.result.token;
        const stage1Url = s1Data.result.stage1;

        const post1Res = await fetch(stage1Url, {
            method: 'POST',
            headers: { ...baseHeaders, 'X-CSRF-Token': token1 },
            signal: AbortSignal.timeout(10000),
        });
        if (!post1Res.ok) return null;
        const stage1Text = await post1Res.text();

        const s2Res = await fetch(`${API}/enc-vidfast?text=${encodeURIComponent(stage1Text)}&stage=2`, {
            signal: AbortSignal.timeout(8000),
        });
        const s2Data = await s2Res.json();
        if (s2Data?.status !== 200 || !s2Data?.result?.servers) return null;

        const { servers: serversUrl, stream: streamBaseUrl, token: token2 } = s2Data.result;
        const currentHeaders = { ...baseHeaders, 'X-CSRF-Token': token2 };

        const serversRes = await fetch(serversUrl, {
            method: 'POST',
            headers: currentHeaders,
            signal: AbortSignal.timeout(10000),
        });
        if (!serversRes.ok) return null;
        const serversEncrypted = await serversRes.text();

        const decServersRes = await fetch(`${API}/dec-vidfast`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: serversEncrypted }),
            signal: AbortSignal.timeout(8000),
        });
        const decServersData = await decServersRes.json();
        if (decServersData?.status !== 200 || !Array.isArray(decServersData.result)) return null;

        const allUrls = [];

        for (const server of decServersData.result) {
            if (!server?.data) continue;
            try {
                const streamUrl = `${streamBaseUrl}/${server.data}`;
                const streamEncRes = await fetch(streamUrl, {
                    method: 'POST',
                    headers: currentHeaders,
                    signal: AbortSignal.timeout(8000),
                });
                if (!streamEncRes.ok) continue;
                const streamEncText = await streamEncRes.text();

                const decStreamRes = await fetch(`${API}/dec-vidfast`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text: streamEncText }),
                    signal: AbortSignal.timeout(8000),
                });
                const decStreamData = await decStreamRes.json();
                if (decStreamData?.status !== 200 || !decStreamData.result) continue;

                const streamResult = decStreamData.result;
                const resultUrl = typeof streamResult === 'string' ? streamResult : streamResult?.url || streamResult?.file;

                if (typeof resultUrl === 'string' && resultUrl.startsWith('http')) {
                    allUrls.push({
                        url: resultUrl,
                        label: server.name || server.title || 'VidFast',
                        headers: {
                            'User-Agent': ua,
                            'Referer': `${BASE_URL}/`,
                        },
                    });
                }
            } catch { }
        }

        if (allUrls.length === 0) return null;

        return {
            url: allUrls[0].url,
            allUrls,
            headers: allUrls[0].headers,
        };
    } catch {
        return null;
    }
}