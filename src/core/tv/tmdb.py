import logging
import time
import requests
from src.core.config import network
from src.core.cache import cache
from src.core import worker

_TTL_SHORT = 3600
_TTL_LONG = 21600
logger = logging.getLogger(__name__)

_session = requests.Session()
_session.trust_env = False
_adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


def _key(path: str, params: dict | None) -> str:
    if not params:
        return path
    return path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))


def _tbhb_fallback(path: str, params: dict | None) -> dict | None:
    try:
        from src.core.tbhb import api as tbhb
    except Exception:
        return None
    p = (path or "").strip("/")
    parts = p.split("/")
    params = params or {}
    page = params.get("page", 1)
    if p.startswith("trending/") and len(parts) >= 2:
        return tbhb.trending_media("tv" if parts[1] == "tv" else "movie")
    if p in ("search/movie", "search/tv"):
        return tbhb.tmdb_search(parts[1], str(params.get("query") or ""), page)
    lists = {
        "movie/popular": ("movie_list", "popular"),
        "movie/top_rated": ("movie_list", "top_rated"),
        "movie/now_playing": ("movie_list", "now_playing"),
        "tv/popular": ("tv_list", "popular"),
        "tv/top_rated": ("tv_list", "top_rated"),
        "tv/on_the_air": ("tv_list", "on_the_air"),
    }
    if p in lists:
        method, name = lists[p]
        return getattr(tbhb, method)(name, page)
    if len(parts) == 2 and parts[0] in ("movie", "tv"):
        return tbhb.movie_details(parts[1]) if parts[0] == "movie" else tbhb.tv_details(parts[1])
    if len(parts) == 4 and parts[0] == "tv" and parts[2] == "season":
        return tbhb.tv_season(parts[1], parts[3])
    if len(parts) == 3 and parts[0] in ("movie", "tv") and parts[1].isdigit():
        if parts[2] == "credits":
            data = tbhb.movie_details(parts[1]) if parts[0] == "movie" else tbhb.tv_details(parts[1])
            return data.get("credits") if isinstance(data.get("credits"), dict) else {"cast": [], "crew": []}
        if parts[2] == "external_ids":
            data = tbhb.movie_details(parts[1]) if parts[0] == "movie" else tbhb.tv_details(parts[1])
            return {"id": data.get("id", parts[1]), "imdb_id": data.get("imdb_id")}
    return None


def _get(path: str, params: dict | None = None, ttl: int = _TTL_SHORT, use_cache: bool = True) -> dict:
    key = _key(path, params)
    if use_cache:
        cached = cache.load("tmdb", key, ttl_seconds=ttl)
        if cached is not None:
            return cached
    try:
        resp = _session.get(f"{network.tmdb_proxy_base}{path}", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        fallback = _tbhb_fallback(path, params)
        if fallback is None:
            raise
        logger.warning("TMDB proxy failed (%s); using TBHB metadata for %s", exc, path)
        data = fallback
    cache.save("tmdb", key, data)
    return data


def image_url(path: str | None, size: str = "w342") -> str | None:
    if not path:
        return None
    return f"{network.tmdb_image_base}/{size}{path}"


def trending(media_type: str = "all", time_window: str = "day", page: int = 1, use_cache: bool = True) -> dict:
    return _get(f"/trending/{media_type}/{time_window}", {"page": page}, _TTL_SHORT, use_cache)


def popular(media_type: str = "movie", page: int = 1, use_cache: bool = True) -> dict:
    return _get(f"/{media_type}/popular", {"page": page}, _TTL_LONG, use_cache)


def top_rated(media_type: str = "movie", page: int = 1, use_cache: bool = True) -> dict:
    return _get(f"/{media_type}/top_rated", {"page": page}, _TTL_LONG, use_cache)


def now_playing(page: int = 1, use_cache: bool = True) -> dict:
    return _get("/movie/now_playing", {"page": page}, _TTL_SHORT, use_cache)


def on_the_air(page: int = 1, use_cache: bool = True) -> dict:
    return _get("/tv/on_the_air", {"page": page}, _TTL_SHORT, use_cache)


def search(media_type: str, query: str, page: int = 1, use_cache: bool = True) -> dict:
    return _get(f"/search/{media_type}", {"query": query, "page": page}, _TTL_SHORT, use_cache)


def details(media_type: str, tmdb_id: int, use_cache: bool = True) -> dict:
    return _get(f"/{media_type}/{tmdb_id}", None, _TTL_LONG, use_cache)


def credits(media_type: str, tmdb_id: int, use_cache: bool = True) -> dict:
    return _get(f"/{media_type}/{tmdb_id}/credits", None, _TTL_LONG, use_cache)


def external_ids(media_type: str, tmdb_id: int, use_cache: bool = True) -> dict:
    return _get(f"/{media_type}/{tmdb_id}/external_ids", None, _TTL_LONG, use_cache)


def season(tv_id: int, season_number: int, use_cache: bool = True) -> dict:
    return _get(f"/tv/{tv_id}/season/{season_number}", None, _TTL_LONG, use_cache)


_TTL_SKIP = 7 * 86400
_TTL_SKIP_MISS = 6 * 3600


def _skip_fetch(url: str, params: dict) -> dict | None:
    try:
        resp = _session.get(url, params=params, timeout=8)
    except requests.RequestException as exc:
        logger.warning("Skip lookup failed (%s): %s", url, exc)
        return None
    if resp.status_code == 404:
        return {}
    if resp.status_code != 200:
        logger.warning("Skip lookup %s returned HTTP %s", url, resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else {}


def _seg(start, end) -> list | None:
    try:
        s = max(0, int(start or 0))
        e = None if end is None else int(end)
    except (TypeError, ValueError):
        return None
    return [s, e] if e is None or e > s else None


def _theintrodb(tmdb_id: int, season, episode) -> dict | None:
    params: dict = {"tmdb_id": tmdb_id}
    if season is not None:
        params.update(season=season, episode=episode)
    data = _skip_fetch("https://api.theintrodb.org/v2/media", params)
    if data is None:
        return None
    out = {"intro": [], "credits": []}
    for kind in ("intro", "credits"):
        for item in data.get(kind) or []:
            seg = _seg(item.get("start_ms"), item.get("end_ms")) if isinstance(item, dict) else None
            if seg and (kind == "credits" or seg[1] is not None):
                out[kind].append(seg)
    return out


def _skipdb(imdb_id: str, season, episode) -> dict | None:
    params: dict = {"imdb_id": imdb_id}
    if season is None:
        params["is_movie"] = "true"
    else:
        params.update(season=season, episode=episode)
    data = _skip_fetch("https://api.introdb.app/segments", params)
    if data is None:
        return None
    out = {"intro": [], "credits": []}
    for kind, name in (("intro", "intro"), ("credits", "outro")):
        item = data.get(name)
        seg = _seg(item.get("start_ms"), item.get("end_ms")) if isinstance(item, dict) else None
        if seg and (kind == "credits" or seg[1] is not None):
            out[kind].append(seg)
    return out


def skip_segments(media_type: str, tmdb_id: int, imdb_id: str | None = None, season: int | None = None,
                  episode: int | None = None, use_cache: bool = True) -> dict:
    if media_type != "tv":
        season = episode = None
    key = f"{media_type}:{tmdb_id}:{season}:{episode}"
    if use_cache:
        hit = cache.load("skip", key, ttl_seconds=_TTL_SKIP)
        if isinstance(hit, dict) and (hit.get("intro") or hit.get("credits")
                                      or time.time() - hit.get("at", 0) < _TTL_SKIP_MISS):
            return hit
    result = {"intro": [], "credits": []}
    failed = False
    sources = [lambda: _theintrodb(tmdb_id, season, episode)]
    if imdb_id:
        sources.append(lambda: _skipdb(imdb_id, season, episode))
    for fetch in sources:
        if result["intro"] and result["credits"]:
            break
        found = fetch()
        if found is None:
            failed = True
            continue
        for kind in ("intro", "credits"):
            if not result[kind]:
                result[kind] = found[kind]
    result["at"] = time.time()
    logger.info("Skip segments %s: intro=%s credits=%s", key, result["intro"], result["credits"])
    if not failed or result["intro"] or result["credits"]:
        cache.save("skip", key, result)
    return result


def anime_skip_segments(anilist_id, number, fallback: dict | None = None) -> dict:
    key = str(anilist_id)
    info = cache.load("skip_map", key, ttl_seconds=_TTL_SKIP)
    if not isinstance(info, dict):
        info = {}
        try:
            resp = _session.get("https://api.ani.zip/mappings", params={"anilist_id": anilist_id}, timeout=8)
            data = resp.json() if resp.status_code == 200 else {}
            mp = data.get("mappings") or {}
            info = {"tmdb": mp.get("themoviedb_id"), "imdb": mp.get("imdb_id"), "type": mp.get("type"),
                    "eps": {k: [v.get("seasonNumber"), v.get("episodeNumber")]
                            for k, v in (data.get("episodes") or {}).items() if isinstance(v, dict)}}
            if info["tmdb"] or info["imdb"]:
                cache.save("skip_map", key, info)
        except (requests.RequestException, ValueError, AttributeError):
            logger.warning("ani.zip mapping lookup failed for %s", anilist_id)
    fb = fallback or {}
    tid = info.get("tmdb") or fb.get("themoviedbId")
    tid = tid[0] if isinstance(tid, (list, tuple)) and tid else tid
    imdb = info.get("imdb") or fb.get("imdbId")
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        logger.info("No TMDB id for anime %s; skip data unavailable", anilist_id)
        return {}
    movie = str(info.get("type") or fb.get("format") or fb.get("type") or "").upper() == "MOVIE"
    season, ep = fb.get("tmdbSeason") or fb.get("defaultTvdbSeason") or 1, number
    pair = (info.get("eps") or {}).get(str(number)) or []
    if len(pair) == 2 and all(isinstance(x, int) and x > 0 for x in pair):
        season, ep = pair
    try:
        season = int(season)
    except (TypeError, ValueError):
        season = 1
    return skip_segments("movie" if movie else "tv", tid, imdb or None, season, ep)


def _async(fn):
    def wrapper(*args, on_done=None, on_error=None, **kwargs):
        return worker.submit(fn, args=args, kwargs=kwargs, on_done=on_done, on_error=on_error)
    return wrapper


trending_async = _async(trending)
popular_async = _async(popular)
top_rated_async = _async(top_rated)
now_playing_async = _async(now_playing)
on_the_air_async = _async(on_the_air)
search_async = _async(search)
details_async = _async(details)
credits_async = _async(credits)
external_ids_async = _async(external_ids)
season_async = _async(season)
skip_segments_async = _async(skip_segments)
