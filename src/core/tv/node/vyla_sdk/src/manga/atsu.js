import { Session, ClientIdentifier, initTLS, destroyTLS } from "node-tls-client";

const BASE_URL = "https://atsu.moe";
const CDN_URL = "https://cdn.atsu.moe";

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

const HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": `${BASE_URL}/`,
    "Origin": BASE_URL,
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Priority": "u=1, i"
};

let tlsInitialized = false;
let session = null;

async function ensureSession() {
    if (!tlsInitialized) {
        await initTLS();
        tlsInitialized = true;
    }
    if (!session) {
        session = new Session({
            clientIdentifier: ClientIdentifier.chrome_131,
            timeout: 15000,
            insecureSkipVerify: false,
        });
    }
    return session;
}

const formatAssetUrl = (url) => {
    if (!url) return null;
    if (url.startsWith("http://") || url.startsWith("https://")) return url;
    if (url.startsWith("/static/")) return `${CDN_URL}${url}`;
    if (url.startsWith("/")) return `${CDN_URL}${url}`;
    return `${CDN_URL}/static/${url}`;
};

async function atsuFetch(url) {
    const sess = await ensureSession();
    const res = await sess.get(url, { headers: HEADERS });
    if (res.status !== 200) {
        throw new Error(`Atsu request failed: ${res.status}`);
    }
    const text = await res.text();
    return JSON.parse(text);
}

export async function searchManga(query, page = 1, perPage = 12) {
    const params = new URLSearchParams({
        q: query,
        query_by: "title,englishTitle,otherNames,authors,acronyms",
        query_by_weights: "4,3,2,2,1",
        num_typos: "4,3,2,1,0",
        prefix: "true,true,true,true,false",
        include_fields: "id,title,englishTitle,authors,poster,posterSmall,posterMedium,type,medium,isAdult,status,year,mbRating,popularity,dateAdded",
        filter_by: "isAdult:=false && views:>0 && hidden:!=true",
        page: String(page),
        per_page: String(perPage),
        infix: "off,off,fallback,off,off"
    });

    const data = await atsuFetch(`${BASE_URL}/collections/manga/documents/search?${params.toString()}`);
    const results = (data?.hits || []).map(hit => {
        const doc = hit.document || {};
        return {
            id: doc.id,
            title: doc.title,
            englishTitle: doc.englishTitle || null,
            authors: Array.isArray(doc.authors) ? doc.authors : [],
            type: doc.type,
            medium: doc.medium,
            status: doc.status,
            year: doc.year,
            rating: doc.mbRating ?? null,
            popularity: doc.popularity ?? null,
            poster: formatAssetUrl(doc.poster),
            posterSmall: formatAssetUrl(doc.posterSmall),
            posterMedium: formatAssetUrl(doc.posterMedium)
        };
    });

    return {
        page: data.page,
        total: data.found,
        results
    };
}

export async function getMangaDetails(id) {
    const data = await atsuFetch(`${BASE_URL}/api/manga/page?id=${encodeURIComponent(id)}`);
    const page = data?.mangaPage;
    if (!page) throw new Error(`Manga with ID "${id}" not found`);

    return {
        id: page.id,
        title: page.title,
        englishTitle: page.englishTitle || null,
        synopsis: page.synopsis || null,
        status: page.status,
        type: page.type,
        medium: page.medium,
        rating: page.avgRating ?? null,
        views: page.views ?? null,
        authors: Array.isArray(page.authors) ? page.authors.map(a => a.name) : [],
        genres: Array.isArray(page.genres) ? page.genres.map(g => g.name) : [],
        tags: Array.isArray(page.tags) ? page.tags.map(t => t.name) : [],
        poster: formatAssetUrl(page.poster?.largeImage || page.poster?.image),
        totalChapters: page.totalChapterCount || page.chapters?.length || 0,
        chapters: (page.chapters || []).map(ch => ({
            id: ch.id,
            number: ch.number,
            title: ch.title,
            pageCount: ch.pageCount,
            createdAt: ch.createdAt ? new Date(ch.createdAt).toISOString() : null
        }))
    };
}

export async function getChapterPages(mangaId, chapterId) {
    const data = await atsuFetch(
        `${BASE_URL}/api/read/chapter?mangaId=${encodeURIComponent(mangaId)}&chapterId=${encodeURIComponent(chapterId)}`
    );
    const chapter = data?.readChapter;
    if (!chapter) throw new Error(`Chapter "${chapterId}" not found for manga "${mangaId}"`);

    return {
        id: chapter.id,
        number: chapter.number,
        title: chapter.title,
        scanlatorId: chapter.scanlationMangaId || null,
        pages: (chapter.pages || []).map(p => ({
            number: p.number,
            url: formatAssetUrl(p.image),
            width: p.width,
            height: p.height,
            aspectRatio: p.aspectRatio
        }))
    };
}

export async function cleanup() {
    if (session) {
        try {
            await session.close();
        } catch { }
        session = null;
    }
    if (tlsInitialized) {
        try {
            await destroyTLS();
        } catch { }
        tlsInitialized = false;
    }
}