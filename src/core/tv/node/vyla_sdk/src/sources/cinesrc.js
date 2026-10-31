import { fetchJson, fetchText } from '../utils/helpers.js';

const API = "https://enc-dec.app/api";

async function solveStage1(challenge1, seed) {
    const K = [
        0x46C6C721, 0x4230C311, 0x18223A1D, 0x56389BA7,
        0xD4A87103, 0x07CB9E2F, 0x73085F9D, 0xD2396A1F,
        0x23AE5ADD, 0x4646C48F, 0x0F4E0903, 0x808F5F31,
        0x953D4E07, 0x7E238FFF, 0x634595DF, 0xCB5E4301,
    ];

    const SCHEDULES = [
        [0, 7, 6, 3, 2, 1, 5, 4, 0, 2, 4, 6, 5, 1, 7, 3],
        [5, 2, 1, 3, 0, 4, 6, 7, 4, 1, 0, 7, 3, 6, 5, 2],
        [4, 0, 2, 1, 5, 7, 6, 3, 2, 6, 0, 7, 4, 3, 5, 1],
        [5, 4, 7, 0, 2, 3, 1, 6, 5, 1, 4, 0, 7, 2, 3, 6],
        [4, 7, 0, 2, 1, 6, 5, 3, 6, 1, 3, 2, 0, 7, 4, 5],
        [0, 7, 2, 6, 3, 4, 5, 1, 6, 5, 0, 2, 7, 1, 4, 3],
        [0, 4, 5, 3, 1, 7, 2, 6, 3, 7, 5, 6, 1, 0, 2, 4],
        [7, 6, 0, 4, 2, 5, 1, 3, 7, 5, 3, 4, 2, 6, 0, 1],
    ];

    const PERMUTATIONS = [
        [3, 5, 1, 0, 2, 4],
        [2, 0, 4, 5, 3, 1],
        [1, 4, 5, 3, 0, 2],
        [2, 0, 1, 5, 4, 3],
        [4, 2, 1, 0, 3, 5],
        [5, 0, 2, 1, 3, 4],
        [4, 5, 1, 0, 3, 2],
        [4, 0, 3, 5, 1, 2],
    ];

    const u32 = x => x >>> 0;
    const rotl = (x, n) => u32((x << (n & 31)) | (x >>> (32 - (n & 31))));
    const imul = (a, b) => {
        const ah = (a >>> 16) & 0xffff;
        const al = a & 0xffff;
        const bh = (b >>> 16) & 0xffff;
        const bl = b & 0xffff;
        return u32((ah * bl + al * bh) << 16) + al * bl;
    };

    const b64dec = s => {
        const bin = atob(s.replace(/-/g, '+').replace(/_/g, '/'));
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        return bytes;
    };

    const c = b64dec(challenge1.w);
    const view = new DataView(c.buffer);
    const mode = c[21];
    const diffVal = c[22];
    const var_ = c[23];
    const rnd = c[24];
    const m_b = c[25];
    const s_i = c[26];
    const p_i = c[27];

    const w0 = view.getUint32(0, true);
    const w1 = view.getUint32(4, true);
    const w2 = view.getUint32(8, true);
    const w3 = view.getUint32(12, true);
    const w = [w0, w1, w2, w3];

    const size = 1 << m_b;
    const s = new Uint32Array(size);
    let x = u32(w[var_ & 3] ^ K[s_i] ^ imul(K[8 + s_i], var_ + 1));
    let y = u32(w[(var_ + 1) & 3] ^ K[15 - s_i]);

    for (let i = 0; i < size; i++) {
        x = u32(w[(i + s_i) & 3] + x + imul(K[(i + var_) & 15], i + 1));
        x = u32(x ^ (x << 13));
        x = u32(x ^ (x >>> 17));
        x = u32(x ^ (x << 5));
        y = rotl(imul(K[(i + 9) & 15], i + 3) ^ y ^ x, i + var_ + s_i);
        s[i] = u32(imul(K[(i + 5) & 15], i ^ var_) ^ rotl(w[(i + var_) & 3], i + s_i) ^ x ^ y);
    }

    const seedBytes = new Uint8Array(seed.match(/.{1,2}/g).map(byte => parseInt(byte, 16)));
    const msg = new Uint8Array(88);
    msg.set(c.slice(0, 16), 24);

    const cfg = m_b ^ 0x6B29;
    msg[48] = 4;
    msg[49] = mode;
    msg[50] = var_;
    msg[51] = s_i;
    msg[52] = p_i;
    msg[53] = rnd;
    msg[54] = cfg & 0xff;
    msg[55] = cfg >> 8;
    msg.set(seedBytes, 56);

    const mask = size - 1;
    const sch = SCHEDULES[s_i];
    const perm = PERMUTATIONS[p_i];

    let n = 0n;
    while (true) {
        const lo = Number(n & 0xffffffffn);
        const hi = Number(n >> 32n);

        let ma = u32(w[0] ^ K[var_] ^ lo);
        let mb = u32(rotl(lo, var_ + 7) ^ w[2]);
        let mc = u32(w[1] ^ K[8 + s_i] ^ hi);
        let md = u32(rotl(lo ^ hi, s_i + 11) ^ w[3]);
        let me = u32(K[s_i + var_] ^ lo ^ hi);
        let mf = u32(K[15 - var_] ^ w[s_i & 3]);

        for (let r = 0; r < rnd; r++) {
            const i1 = u32(rotl(mb, r + 1) ^ imul(K[r & 15], mf) ^ me ^ ma) & mask;
            const v1 = s[i1];
            const i2 = u32(rotl(me, var_ + r + 3) ^ imul(K[(r + 7) & 15], md) ^ mc ^ v1) & mask;
            const op = sch[r];

            if (op === 0) {
                ma = rotl(u32(ma + md + v1), 5);
                mc = u32(imul(mc ^ mf ^ ma, 1187432225) + mb);
            } else if (op === 1) {
                mb = rotl(mc ^ me ^ mb ^ v1, 11);
                md = u32(imul(mb ^ ma, 1110491921) + md);
            } else if (op === 2) {
                mc = rotl(u32(mc + mb + v1), 17);
                ma = u32(imul(md ^ mf ^ mc, 404896285) ^ ma);
            } else if (op === 3) {
                md = rotl(ma ^ md ^ v1, 23);
                mb = u32(imul(mc ^ me ^ md, 1446550439) + mb);
            } else if (op === 4) {
                ma = u32(imul(ma ^ me ^ v1, -727158525) + rotl(mb, 7));
                md = u32(rotl(u32(mc + mf + ma), 13) ^ md);
            } else if (op === 5) {
                mc = u32(imul(u32(mc + mf + v1), 130784815) ^ rotl(md, 9));
                mb = u32(rotl(ma ^ me ^ mc, 19) + mb);
            } else if (op === 6) {
                mb = u32(imul(mb ^ mf ^ v1, 1929928605) + rotl(ma, 3));
                mc = u32(rotl(u32(md + me + mb), 15) ^ mc);
            } else {
                md = u32(imul(u32(md + me + v1), -767989217) ^ rotl(mc, 21));
                ma = u32(rotl(mb ^ mf ^ md, 27) + ma);
            }

            const v2 = s[i2];
            if (((mf ^ r ^ v1) & 1) === 0) {
                mf = rotl(u32(v2 + mf + ma), r + 5);
                me = u32(imul(K[8 + op], v2 ^ me ^ mf) + md);
            } else {
                me = rotl(v2 ^ me ^ mb, r + 9);
                mf = u32(imul(K[(op + 11) & 15], u32(v2 + mf + me)) ^ mc);
            }
        }

        const mArr = [ma, mc, mb, md, mf, me];
        const v = perm.map(idx => mArr[idx]);

        const msgView = new DataView(msg.buffer);
        msgView.setUint32(0, v[0], true);
        msgView.setUint32(4, v[1], true);
        msgView.setUint32(8, v[2], true);
        msgView.setUint32(12, v[3], true);
        msgView.setUint32(16, v[4], true);
        msgView.setUint32(20, v[5], true);
        msgView.setBigUint64(40, n, true);

        const hashBuf = await crypto.subtle.digest('SHA-256', msg);
        const hashBytes = new Uint8Array(hashBuf);

        const b = Math.floor(diffVal / 8);
        const rBits = diffVal % 8;
        let ok = true;
        for (let i = 0; i < b; i++) {
            if (hashBytes[i] !== 0) {
                ok = false;
                break;
            }
        }
        if (ok && (rBits === 0 || (hashBytes[b] >> (8 - rBits)) === 0)) {
            return `m4.${n.toString(16)}`;
        }
        n++;
    }
}

async function solveStage2(data) {
    const target = data.pack[0].split('').reverse().join('');
    const salt = data.pack[3].split('').reverse().join('');
    const r = data.pack[4].split('').reverse().join('');

    const b64decStr = s => decodeURIComponent(escape(atob(s.replace(/-/g, '+').replace(/_/g, '/'))));
    const body = b64decStr(r.split('.')[1]);
    const payload = b64decStr(body.split('.')[1]);
    const difficulty = JSON.parse(payload).d;

    const width = Math.floor((difficulty + 3) / 4);
    const limit = 1 << difficulty;

    for (let counter = 0; counter < limit; counter++) {
        const key = counter.toString(16).padStart(width, '0');
        const msgUint8 = new TextEncoder().encode(salt + key);
        const hashBuffer = await crypto.subtle.digest('SHA-256', msgUint8);
        const hashArray = Array.from(new Uint8Array(hashBuffer));
        const digest = hashArray.map(b => b.toString(16).padStart(2, '0')).join('');

        if (digest === target) {
            return key;
        }
    }
    throw new Error('no solution found');
}

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const type = isTv ? 'tv' : 'movie';
    const url = isTv
        ? `https://cinesrc.st/embed/tv/${id}?s=${s}&e=${e}`
        : `https://cinesrc.st/embed/movie/${id}`;

    const headers = {
        "Origin": "https://cinesrc.st",
        "Referer": "https://cinesrc.st/",
        "Content-Type": "text/plain;charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.37 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.37"
    };

    const fields = [type, id, s ? String(s) : null, e ? String(e) : null];
    const encoded = btoa(JSON.stringify(fields).replace(/ /g, '')).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

    const bootstrapHeaders = { ...headers, "x-cs-q": encoded };
    const bootstrap = await fetchJson("https://cinesrc.st/api/c/bootstrap", {
        method: "POST",
        headers: bootstrapHeaders,
        body: "{}"
    });

    const cookies = {
        "x-cs-b": "20261014-a",
        "x-cs-q": encoded,
        "x-cs-r": bootstrap.r,
        "x-cs-p": bootstrap.p,
    };

    const challenge1 = await fetchJson("https://cinesrc.st/api/c/issue", {
        headers: { ...headers, ...cookies }
    });

    const payload1 = {
        data: {
            url,
            user_agent: headers["User-Agent"],
            challenge_data: challenge1
        },
        type: "seed"
    };

    const seedRes = await fetchJson(`${API}/enc-cinesrc`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload1)
    });

    const seedData = seedRes.result;
    const solution1 = await solveStage1(challenge1, seedData.seed);

    const challenge2 = await fetchJson("https://cinesrc.st/api/c/stage2/issue", {
        headers: { ...headers, ...cookies }
    });
    const solution2 = await solveStage2(challenge2);

    const payload2 = {
        data: {
            url,
            user_agent: headers["User-Agent"],
            challenge_data: {
                stage1: { challenge: challenge1, solution: solution1 },
                stage2: { challenge: challenge2, solution: solution2 }
            },
            state: seedData.state
        },
        type: "encrypt"
    };

    const encryptRes = await fetchJson(`${API}/enc-cinesrc`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload2)
    });

    const encData = encryptRes.result;
    const token = `${encData.token}::c3::${cookies["x-cs-r"]}`;
    const nextActions = encData.headers;

    const providersText = await fetchText(url, {
        method: "POST",
        headers: { ...headers, ...cookies, "Next-Action": nextActions.getProviderList },
        body: JSON.stringify([])
    });

    const lines = providersText.split('\n');
    let providers = null;
    for (const rawLine of lines) {
        const colonIndex = rawLine.indexOf(':');
        if (colonIndex !== -1) {
            const candidate = rawLine.slice(colonIndex + 1).trim();
            if (candidate.startsWith('[') && candidate.endsWith(']')) {
                try {
                    providers = JSON.parse(candidate);
                    break;
                } catch { }
            }
        }
    }

    if (!Array.isArray(providers) || providers.length === 0) return null;

    const allUrls = [];

    for (const providerObj of providers) {
        const provider = providerObj.id;
        const streamPayload = [
            id,
            isTv ? "show" : type,
            isTv ? String(s) : "$undefined",
            isTv ? String(e) : "$undefined",
            token,
            provider
        ];

        let streamRes;
        try {
            streamRes = await fetchText(url, {
                method: "POST",
                headers: { ...headers, ...cookies, "Next-Action": nextActions.getStream },
                body: JSON.stringify(streamPayload)
            });
        } catch {
            continue;
        }

        if (!streamRes || streamRes.includes('"status":500') || streamRes.includes('invalid_challenge') || streamRes.length < 10) continue;

        let decryptRes;
        try {
            decryptRes = await fetchJson(`${API}/dec-cinesrc`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text: streamRes, state: encData.state })
            });
        } catch {
            continue;
        }

        if (decryptRes && decryptRes.status === 200 && decryptRes.result) {
            let resultData = decryptRes.result;
            if (typeof resultData === 'string') {
                try {
                    resultData = JSON.parse(resultData);
                } catch { }
            }

            let streamUrl = null;
            if (Array.isArray(resultData?.url)) {
                streamUrl = resultData.url[0]?.url;
            } else if (typeof resultData?.url === 'string') {
                streamUrl = resultData.url;
            } else if (resultData?.file) {
                streamUrl = resultData.file;
            } else if (Array.isArray(resultData?.sources)) {
                streamUrl = resultData.sources[0]?.url;
            }

            if (streamUrl) {
                if (streamUrl.startsWith('/')) {
                    streamUrl = `https://cinesrc.st${streamUrl}`;
                }

                allUrls.push({
                    url: streamUrl,
                    server: `CineSrc - ${providerObj.name || provider}`,
                    headers: resultData.headers || {
                        Referer: "https://cinesrc.st/",
                        Origin: "https://cinesrc.st"
                    }
                });
                break;
            }
        }
    }

    if (!allUrls.length) return null;

    return {
        url: allUrls[0].url,
        headers: allUrls[0].headers,
        allUrls
    };
}