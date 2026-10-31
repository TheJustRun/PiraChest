import SOURCES, { SOURCE_MAP } from "./config.js";
import { fetchDownloads } from "./downloads/downloads.js";
import { probeSource } from "./health.js";
import { getChapterPages, getMangaDetails, searchManga } from "./manga/atsu.js";
import { fetchSubtitles, getSubPathsMovie, getSubPathsTv } from "./subtitles.js";
import { createStreamArgs } from "./utils/helpers.js";

const ALL_SOURCE_MODULES = Object.fromEntries(
    await Promise.all(SOURCES.map(async cfg => [cfg.key, await import(`./sources/${cfg.sourceFile}.js`)]))
);

const SOURCE_MODULES = Object.fromEntries(
    Object.entries(ALL_SOURCE_MODULES).filter(([key]) => !SOURCE_MAP[key]?.disabled)
);

export default class VylaSDK {
    tmdbApiKey = null;
    penguManifest = null;
    constructor({ tmdbApiKey, penguManifest } = {}) {
        this.tmdbApiKey = tmdbApiKey || null;
        this.penguManifest = penguManifest || null;
    }
    getSources(excludeDisabled = false) {
        if (excludeDisabled) {
            return SOURCES.filter(cfg => !cfg.disabled);
        }
        return SOURCES;
    }
    async probeAllSources() {
        const active = SOURCES.filter(cfg => !cfg.disabled);

        const results = await Promise.allSettled(
            active.map(async cfg => await probeSource(cfg, SOURCE_MODULES[cfg.key]))
        );

        const sources = Object.fromEntries(
            active.map((cfg, i) => [
                cfg.key,
                results[i].status === 'fulfilled' ? results[i].value : { ok: false, ms: null },
            ])
        );

        return sources;
    }
    async probeSource(key) {
        const cfg = SOURCES.find(cfg => cfg.key === key);
        if (!cfg) throw new Error(`Source with key "${key}" not found`);
        const mod = SOURCE_MODULES[key];
        if (!mod) throw new Error(`Source module for key "${key}" not found`);
        return await probeSource(this, cfg, mod);
    }
    async getStream(key, id, s = null, e = null, clientIP = null) {
        const cfg = SOURCES.find(cfg => cfg.key === key);
        if (!cfg) throw new Error(`Source with key "${key}" not found`);
        const mod = SOURCE_MODULES[key];
        if (!mod) throw new Error(`Source module for key "${key}" not found`);
        const streamArgs = await createStreamArgs(cfg, this, id, s, e, clientIP);
        return await mod.getStream(streamArgs);
    }
    async getSubtitles(id, s = null, e = null) {
        const paths = s != null && e != null
            ? await getSubPathsTv(id, s, e)
            : await getSubPathsMovie(id);
        return await fetchSubtitles(paths);
    }
    async getDownloads(id, s = null, e = null) {
        return await fetchDownloads(this, id, s, e);
    }
    async searchManga(query, page = 1, perPage = 12) {
        return await searchManga(query, page, perPage);
    }
    async getManga(id) {
        return await getMangaDetails(id);
    }
    async getMangaChapter(mangaId, chapterId) {
        return await getChapterPages(mangaId, chapterId);
    }
}