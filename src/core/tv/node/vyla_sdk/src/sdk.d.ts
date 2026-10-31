export interface SourceConfig {
    key: string;
    label: string;
    sourceFile: string;
    proxyParam: string;
    timeout: number;
    jitter: number;
    retries: number;
    multiUrl: boolean;
    skipProxy?: boolean;
    cdnHeaders?: {
        pattern: RegExp;
        headers: {
            Referer: string;
            Origin: string;
        };
    }[];
    skipVerify?: boolean;
    verifyHeaders?: boolean;
    disabled?: boolean;
    sourcesTimeout?: number;
}
export interface ProbeResult {
    ok: boolean;
    ms: number | null;
}
export interface StreamResult {
    url: string;
    headers: {
        "User-Agent": string;
        Referer: string;
        Origin: string;
        [key: string]: string;
    };
    server: string;
    quality: string;
    type: "mp4" | "hls";
    skipVerify?: boolean;
    skipProxy?: boolean;
}

export interface DownloadResult {
    url: string;
    quality: string;
    size: number | null;
    type: "mkv";
    active: boolean;
}
export interface SubtitleResult {
    label: string;
    file: string;
    type: "vtt" | "srt";
    source: string;
}

export type ProbeAllResult = Record<string, ProbeResult>;

export interface VylaSDKOptions {
    tmdbApiKey?: string;
}

export default class VylaSDK {
    tmdbApiKey?: string;

    constructor(options?: VylaSDKOptions);

    /**
     * Get the list of configured sources.
     * @param excludeDisabled If true, filters out disabled sources.
     */
    getSources(excludeDisabled?: boolean): SourceConfig[];

    /**
     * Probe all active (non-disabled) sources for availability.
     */
    probeAllSources(): Promise<ProbeAllResult>;

    /**
     * Probe a single source by key.
     */
    probeSource(key: string): Promise<ProbeResult>;

    /**
     * Get a playable stream for a given source/media id.
     * @param key Source key
     * @param id Media id (e.g. TMDB id)
     * @param s Season number (for TV), or null for movies
     * @param e Episode number (for TV), or null for movies
     * @param clientIP Optional client IP for source-specific logic
     */
    getStream(
        key: string,
        id: string | number,
        s?: number | null,
        e?: number | null,
        clientIP?: string | null
    ): Promise<StreamResult | { allUrls: StreamResult[] }>;

    /**
     * Get subtitles for a movie or TV episode.
     * @param id Media id
     * @param s Season number (for TV), or null for movies
     * @param e Episode number (for TV), or null for movies
     */
    getSubtitles(
        id: string | number,
        s?: number | null,
        e?: number | null
    ): Promise<SubtitleResult[]>;

    /**
     * Get download links for a movie or TV episode.
     * @param id Media id
     * @param s Season number (for TV), or null for movies
     * @param e Episode number (for TV), or null for movies
     */
    getDownloads(
        id: string | number,
        s?: number | null,
        e?: number | null
    ): Promise<DownloadResult[]>;
}