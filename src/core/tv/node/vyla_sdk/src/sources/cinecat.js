import { fetchJson } from '../utils/helpers.js';

export async function getStream({ id, s, e }) {
    const isTv = s != null && e != null;
    const url = isTv
        ? `https://zcdn.bluevelvet.space/cdn?tmdb=${id}&type=tv&season=${s}&episode=${e}`
        : `https://zcdn.bluevelvet.space/cdn?tmdb=${id}&type=movie`;

    const res = await fetchJson(url);
    if (!res || !res.ready || !res.url) return null;

    return {
        url: res.url,
        headers: {
            Referer: 'https://zcdn.bluevelvet.space/',
            Origin: 'https://zcdn.bluevelvet.space',
        }
    };
}