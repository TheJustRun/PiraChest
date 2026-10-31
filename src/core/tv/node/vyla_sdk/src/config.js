export const SOURCES = [
    {
        key: '3eyedraven',
        label: '3eyedraven',
        sourceFile: '3eyedraven',
        proxyParam: '3er',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: false,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /3eyedraven\.watch/i,
            headers: {
                Referer: 'https://3eyedraven.watch/',
                Origin: 'https://3eyedraven.watch',
            },
        }],
    },

    {
        key: '123anime',
        label: '123Anime',
        sourceFile: '123anime',
        proxyParam: 'a1',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: false,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /hlsx\d+cdn\.|burntburst\d+\.store|echovideo\.ru/i,
            headers: {
                Referer: 'https://play2.echovideo.ru/',
                Origin: 'https://play2.echovideo.ru',
            },
        },],
    },

    {
        key: '111477',
        label: '111477',
        sourceFile: '111477',
        proxyParam: 'a11',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /111477\.xyz/i,
            headers: {
                Referer: 'https://st.111477.xyz/',
                Origin: 'https://st.111477.xyz',
            },
        }],
    },

    {
        key: 'cinemaarmy',
        label: 'CinemaArmy',
        sourceFile: 'cinemaarmy',
        proxyParam: 'ca',
        timeout: 30000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /cinema\.army/i,
            headers: {
                Referer: 'https://cinema.army/',
                Origin: 'https://cinema.army',
            },
        }],
    },

    {
        key: 'cinesrc',
        label: 'CineSrc',
        sourceFile: 'cinesrc',
        proxyParam: 'cs',
        timeout: 35000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /cinesrc\.st/i,
            headers: {
                Referer: 'https://cinesrc.st/',
                Origin: 'https://cinesrc.st',
            },
        }],
    },

    {
        key: 'cinezo',
        label: 'Cinezo',
        sourceFile: 'cinezo',
        proxyParam: 'czo',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        cdnHeaders: [{
            pattern: /yenime\.net|flikhub\.net|hakunaymatata\.com/i,
            headers: {
                Referer: 'https://player.cinezo.live/',
                Origin: 'https://player.cinezo.live',
            },
        }],
    },

    {
        key: 'cno',
        label: 'Cno',
        sourceFile: 'cno',
        proxyParam: 'cno',
        timeout: 25000,
        jitter: 300,
        retries: 1,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /cno\.to/i,
            headers: {
                Referer: 'https://cno.to/',
                Origin: 'https://cno.to',
            },
        }],
    },

    {
        key: 'dulo',
        label: 'Dulo',
        sourceFile: 'dulo',
        proxyParam: 'dl',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /dulo\.mov/i,
            headers: {
                Referer: 'https://dulo.mov/',
                Origin: 'https://dulo.mov',
            },
        }],
    },

    {
        key: 'flaxmovies',
        label: 'FlaxMovies',
        sourceFile: 'flaxmovies',
        proxyParam: 'fx',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /themoviebox\.uk|vidrock\.|orbitingdreams\.xyz|fastcloud\.live|deton444eik\.com/i,
            headers: {
                Referer: 'https://themoviebox.uk/',
                Origin: 'https://themoviebox.uk',
            },
        }],
    },

    {
        key: 'frost',
        label: 'FrostStream',
        sourceFile: 'frost',
        proxyParam: 'fst',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
    },

    {
        key: 'fsonline',
        label: 'FSOnline',
        sourceFile: 'fsonline',
        proxyParam: 'fo',
        timeout: 20000,
        retries: 1,
        jitter: 0,
        multiUrl: true,
        skipProxy: true,
    },

    {
        key: 'hdhub',
        label: 'HDHub',
        sourceFile: 'hdhub',
        proxyParam: 'hdh',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
    },

    {
        key: 'hexa',
        label: 'Hexa',
        sourceFile: 'hexa',
        proxyParam: 'hx',
        timeout: 30000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /hexa\.su|flixer\.su/i,
            headers: {
                Referer: 'https://hexa.su/',
                Origin: 'https://hexa.su',
            },
        }],
    },

    {
        key: 'kickassanime',
        label: 'KickAssAnime',
        sourceFile: 'kickassanime',
        proxyParam: 'kaa',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /krussdomi\.com/i,
            headers: {
                Referer: 'https://kaa.lt/',
                Origin: 'https://kaa.lt',
            },
        }],
    },

    {
        key: 'hyper',
        label: 'Hyper',
        sourceFile: 'hyper',
        proxyParam: 'hyp',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        cdnHeaders: [{
            pattern: /hakunaymatata\.com/i,
            headers: {
                Referer: 'https://mzfi.me/',
                Origin: 'https://mzfi.me',
            },
        }],
    },

    {
        key: 'kisskh',
        label: 'KissKH',
        sourceFile: 'kisskh',
        proxyParam: 'kk',
        timeout: 30000,
        jitter: 500,
        retries: 1,
    },

    {
        key: 'lmscript',
        label: 'LMScript',
        sourceFile: 'lmscript',
        proxyParam: 'lmsc',
        timeout: 20000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
    },

    {
        key: 'lookmovie',
        label: 'LookMovie',
        sourceFile: 'lookmovie',
        proxyParam: 'lm',
        timeout: 20000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        verifyHeaders: {
            'Accept-Language': 'en-US,en;q=0.9',
        },
    },

    {
        key: 'movenerds',
        label: 'MoverNerds',
        sourceFile: 'movenerds',
        proxyParam: 'mnr',
        timeout: 30000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /movenerds\.space/i,
            headers: {
                Referer: 'https://movenerds.space/',
                Origin: 'https://movenerds.space',
            },
        }],
    },

    {
        key: 'movy',
        label: 'Movy',
        sourceFile: 'movy',
        proxyParam: 'mv',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /wecollege\.net|movy\.sx|goldenkite\.top/i,
            headers: {
                Referer: 'https://www.movy.sx/',
                Origin: 'https://www.movy.sx',
            },
        }],
    },

    {
        key: 'net79',
        label: 'Net79',
        sourceFile: 'net79',
        proxyParam: 'n79',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
    },

    {
        key: 'pengu',
        label: 'Pengu',
        sourceFile: 'pengu',
        proxyParam: 'pg',
        timeout: 25000,
        jitter: 500,
        retries: 1,
        multiUrl: true,
        skipProxy: true,
        disabled: false, // Enabled, but only returns streams when a manifest is provided
    },

    {
        key: 'pstream',
        label: 'PStream',
        sourceFile: 'pstream',
        proxyParam: 'ps',
        timeout: 35000,
        jitter: 500,
        retries: 1,
        multiUrl: true,
    },

    {
        key: 'purstream',
        sourceFile: 'purstream',
        label: 'Purstream',
        proxyParam: 'ps',
        timeout: 20000,
        jitter: 500,
        retries: 2,
        skipProxy: true
    },

    {
        key: 'rivestream',
        label: 'RiveStream',
        sourceFile: 'rivestream',
        proxyParam: 'rs',
        timeout: 30000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
    },

    {
        key: 'rivo',
        label: 'Rivo',
        sourceFile: 'rivo',
        proxyParam: 'rv',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        cdnHeaders: [{
            pattern: /rivo\.st/i,
            headers: {
                Referer: 'https://f.rivo.st/',
                Origin: 'https://f.rivo.st',
            },
        }],
    },

    {
        key: 'streamaggregator',
        label: 'StreamAggregator',
        sourceFile: 'streamaggregator',
        proxyParam: 'sa',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /streamaggregator\.in/i,
            headers: {
                Referer: 'https://streamaggregator.in/',
                Origin: 'https://streamaggregator.in',
            },
        }],
    },

    {
        key: 'thugtv',
        label: 'ThugTV',
        sourceFile: 'thugtv',
        proxyParam: 'tv',
        timeout: 30000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /thugtv\.online|fcxmb\.com|hscow\.com/i,
            headers: {
                Referer: 'https://thugtv.online/',
                Origin: 'https://thugtv.online',
            },
        }],
    },

    {
        key: 'vidapi',
        label: 'VidAPI',
        sourceFile: 'vidapi',
        proxyParam: 'va',
        timeout: 25000,
        jitter: 500,
        retries: 1,
        multiUrl: true
    },

    {
        key: 'vidcore',
        label: 'VidCore',
        sourceFile: 'vidcore',
        proxyParam: 'vc',
        timeout: 35000,
        jitter: 500,
        retries: 1,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /vidcore\.io/i,
            headers: {
                Referer: 'https://vidcore.io/',
                Origin: 'https://vidcore.io',
            },
        }],
    },
    
    {
        key: 'vidfast',
        label: 'VidFast',
        sourceFile: 'vidfast',
        proxyParam: 'vf',
        timeout: 35000,
        jitter: 500,
        retries: 1,
        multiUrl: true,
    },

    {
        key: 'vidlink',
        label: 'Vidlink',
        sourceFile: 'vidlink',
        proxyParam: 'vl',
        timeout: 20000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
    },

    {
        key: 'vidrock',
        label: 'VidRock',
        sourceFile: 'vidrock',
        proxyParam: 'vr',
        timeout: 20000,
        jitter: 800,
        retries: 3,
        multiUrl: true,
        cdnHeaders: [{
            pattern: /./,
            headers: {
                Accept: '/',
                'Accept-Language': 'en-US,en;q=0.9',
                Referer: 'https://vidrock.ru/',
                Origin: 'https://vidrock.ru',
            },
        },],
    },

    {
        key: 'vidup',
        label: 'VidUp',
        sourceFile: 'vidup',
        proxyParam: 'vu',
        timeout: 35000,
        jitter: 500,
        retries: 1,
        multiUrl: true
    },

    {
        key: 'vidvault',
        label: 'VidVault',
        sourceFile: 'vidvault',
        proxyParam: 'vv',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
    },

    {
        key: 'vidy',
        label: 'Vidy',
        sourceFile: 'vidy',
        proxyParam: 'vy',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /wecollege\.net|vidy\.st/i,
            headers: {
                Referer: 'https://www.vidy.st/',
                Origin: 'https://www.vidy.st',
            },
        }],
    },

    {
        key: 'vidzee',
        label: 'VidZee',
        sourceFile: 'vidzee',
        proxyParam: 'vz',
        timeout: 20000,
        sourcesTimeout: 10000,
        jitter: 400,
        retries: 3,
        verifyHeaders: {
            Accept: '/',
            'Accept-Language': 'en-US,en;q=0.9',
            Referer: 'https://player.vidzee.wtf',
            Origin: 'https://player.vidzee.wtf',
        },
    },

    {
        key: 'vixsrc',
        label: 'VixSrc',
        sourceFile: 'vixsrc',
        proxyParam: 'vx',
        timeout: 35000,
        jitter: 0,
        retries: 2,
        multiUrl: false,
        skipProxy: true,
        verifyHeaders: {
            Accept: 'application/json, text/javascript, /; q=0.01',
            'Accept-Language': 'en-US,en;q=0.9',
            Referer: 'https://vixsrc.to/',
            Origin: 'https://vixsrc.to',
        },
    },

    {
        key: 'watchout',
        label: 'Watchout',
        sourceFile: 'watchout',
        proxyParam: 'wo',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
    },

    {
        key: 'xdownloader',
        label: 'XDownloader',
        sourceFile: 'xdownloader',
        proxyParam: 'xd',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /films365\.org/i,
            headers: {
                Referer: 'https://www.films365.org/',
                Origin: 'https://www.films365.org',
            },
        }],
    },

    {
        key: 'yapgrid',
        label: 'YapGrid',
        sourceFile: 'yapgrid',
        proxyParam: 'yap',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
        skipProxy: true,
        cdnHeaders: [{
            pattern: /yapgrid\.com|embedwave\.cc/i,
            headers: {
                Referer: 'https://yapgrid.com/',
                Origin: 'https://yapgrid.com',
            },
        }],
    },

    {
        key: 'yastream',
        label: 'YaStream',
        sourceFile: 'yastream',
        proxyParam: 'ya',
        timeout: 25000,
        jitter: 500,
        retries: 2,
        multiUrl: true,
    },

];

export const HEALTH_PROBE_ID = '155';
export const SOURCE_MAP = Object.fromEntries(SOURCES.map(s => [s.key, s]));
export default SOURCES;