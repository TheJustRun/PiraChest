from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from src.core.cache import cache
from src.core.worker import submit, Priority
from .models import parse_nyaa_results, to_int

logger = logging.getLogger(__name__)

API_BASE = "https://theblackholebay.lol/api"
_NS = "tbhb"
_TTL = 60
_STALE_TTL = 3600
_TIMEOUT = (6, 15)
_NYAA_WAIT = 8
_MAX_RETRIES = 2
_BACKOFF_BASE = 1.0
_MAX_BACKOFF = 8.0
_RETRY_STATUS = frozenset({500, 502, 503, 504})
_HEADERS = {"Accept": "application/json", "User-Agent": "TBHB-Client/1.3"}


class TBHBError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TBHBRateLimited(TBHBError):
    pass


class TBHBBanned(TBHBError):
    pass


class TBHBNotFound(TBHBError):
    pass


_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(_HEADERS)
        s.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=4))
        _local.session = s
    return s


def _backoff(attempt: int, resp: requests.Response | None) -> float:
    delay = min(_MAX_BACKOFF, _BACKOFF_BASE * 2 ** attempt)
    if resp is not None:
        try:
            delay = min(_MAX_BACKOFF, max(0.0, float(resp.headers.get("Retry-After", ""))))
        except ValueError:
            pass
    return delay + random.uniform(0, 0.25)


def _error_message(resp: requests.Response, default: str) -> str:
    try:
        body = resp.json()
        msg = body.get("error") or body.get("message") if isinstance(body, dict) else None
    except ValueError:
        msg = None
    return f"{default}: {str(msg)[:160]}" if msg else default


def _send(path: str, params: dict | None = None, timeout=_TIMEOUT) -> requests.Response:
    url = f"{API_BASE}{path}"
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = _session().get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            raise TBHBError(f"Network error: {exc}") from exc

        status = resp.status_code
        if status == 403:
            raise TBHBBanned("IP banned by TBHB API (403)", status=403)
        if status == 404:
            raise TBHBNotFound("Not found on TBHB (404)", status=404)
        if status == 429 or status in _RETRY_STATUS:
            if attempt < _MAX_RETRIES:
                time.sleep(_backoff(attempt, resp))
                continue
            if status == 429:
                raise TBHBRateLimited("Rate limited by TBHB API (429)", status=429)
            raise TBHBError(f"TBHB API error {status}", status=status)
        if not resp.ok:
            raise TBHBError(_error_message(resp, f"TBHB API error {status}"), status=status)
        return resp
    raise TBHBError("TBHB request failed")


def _request(path: str, params: dict | None = None, timeout=_TIMEOUT) -> dict:
    resp = _send(path, params, timeout)
    try:
        data = resp.json()
    except ValueError as exc:
        raise TBHBError("Invalid TBHB API response") from exc
    if isinstance(data, list):
        return {"results": data}
    if not isinstance(data, dict):
        raise TBHBError("Unexpected TBHB API response shape")
    return data


def _cache_load(key: str, ttl: int):
    try:
        return cache.load(_NS, key, ttl_seconds=ttl)
    except Exception:
        return None


def _cache_save(key: str, data: dict) -> None:
    try:
        cache.save(_NS, key, data)
    except Exception:
        logger.debug("tbhb cache save failed for %s", key, exc_info=True)


def _cached_call(key: str, fetch, use_cache: bool, normalize=None, ttl: int = _TTL) -> dict:
    if use_cache:
        cached = _cache_load(key, ttl)
        if cached is not None:
            return cached
    try:
        data = fetch()
    except (TBHBBanned, TBHBNotFound):
        raise
    except TBHBError:
        stale = _cache_load(key, _STALE_TTL)
        if isinstance(stale, dict):
            return {**stale, "_stale": True}
        raise
    if normalize is not None:
        data = normalize(data)
    _cache_save(key, data)
    return data


def _cached_get(key: str, path: str, params: dict | None, use_cache: bool, normalize=None,
                ttl: int = _TTL) -> dict:
    return _cached_call(key, lambda: _request(path, params), use_cache, normalize, ttl)


def _normalize_search(data: dict) -> dict:
    if not isinstance(data.get("results"), list):
        data["results"] = []
    return data


def search(query: str, category: int = 0, page: int = 0, use_cache: bool = True) -> dict:
    query = " ".join((query or "").split())
    category = to_int(category)
    page = max(0, to_int(page))
    key = f"search:{query.lower()}:{category}:{page}"
    params = {"q": query, "category": category, "page": page}
    return _cached_get(key, "/search", params, use_cache, _normalize_search)


def torrent(torrent_id: str, use_cache: bool = True) -> dict:
    tid = quote(str(torrent_id), safe="")
    return _cached_get(f"torrent:{tid}", f"/torrent/{tid}", None, use_cache)


def files(torrent_id: str, use_cache: bool = True) -> dict:
    tid = quote(str(torrent_id), safe="")
    return _cached_get(f"files:{tid}", f"/files/{tid}", None, use_cache)


def track_download() -> dict:
    return _request("/track/download")


def torrent_extras(torrent_id: str, use_cache: bool = True) -> dict:
    tid = quote(str(torrent_id), safe="")
    return _cached_get(f"torrent-details:{tid}", f"/torrent/{tid}/details", None, use_cache)


def nyaa_rss(query: str = "", category: str = "0_0", use_cache: bool = True) -> dict:
    query = " ".join((query or "").split())
    category = str(category or "0_0")
    params = {}
    if query:
        params["query"] = query
    if category != "0_0":
        params["category"] = category

    def fetch() -> dict:
        return {"results": parse_nyaa_results(_request("/nyaa/rss", params)), "source": "nyaa"}

    return _cached_call(f"nyaa:{query.lower()}:{category}", fetch, use_cache)


def _name(username: str) -> str:
    return " ".join((username or "").split())


def _lookup(key: str, path: str, params: dict | None = None, use_cache: bool = True) -> dict:
    def fetch() -> dict:
        try:
            return _request(path, params)
        except TBHBNotFound:
            return {"_missing": True}

    data = _cached_call(key, fetch, use_cache, ttl=300)
    return {} if data.get("_missing") else data


def user_reputation(username: str, use_cache: bool = True) -> dict:
    n = quote(_name(username), safe="")
    return _lookup(f"user-rep:{n.lower()}", f"/users/{n}/reputation", None, use_cache)


def user_ratings(username: str, use_cache: bool = True) -> dict:
    n = quote(_name(username), safe="")
    return _lookup(f"user-ratings:{n.lower()}", f"/users/{n}/ratings", None, use_cache)


def uploader_reputation(username: str, use_cache: bool = True) -> dict:
    name = _name(username)
    rep = _lookup(f"uploader-rep:{name.lower()}", "/uploader-reputation", {"username": name}, use_cache)
    return rep or user_reputation(name, use_cache)


def trending_media(kind: str = "movie", use_cache: bool = True) -> dict:
    kind = (kind or "movie").lower()
    path = {"tv": "/trending_tv", "game": "/trending_games", "games": "/trending_games"}.get(kind, "/trending")
    return _cached_get(f"trending:{kind}", path, None, use_cache)


def movie_list(list_name: str = "popular", page: int = 1, use_cache: bool = True) -> dict:
    name = (list_name or "popular").lower()
    page = max(1, to_int(page, 1))
    return _cached_get(f"movies:{name}:{page}", "/movies", {"list": name, "page": page}, use_cache)


def tv_list(list_name: str = "popular", page: int = 1, use_cache: bool = True) -> dict:
    name = (list_name or "popular").lower()
    page = max(1, to_int(page, 1))
    return _cached_get(f"tv:{name}:{page}", "/tv", {"list": name, "page": page}, use_cache)


def discover(kind: str = "movie", genre: int | str = 0, page: int = 1, use_cache: bool = True) -> dict:
    media_type = "tv" if (kind or "").lower() == "tv" else "movie"
    page = max(1, to_int(page, 1))
    params = {"type": media_type, "page": page}
    if to_int(genre):
        params["genre"] = to_int(genre)
    return _cached_get(f"discover:{media_type}:{params.get('genre', 0)}:{page}", "/discover", params, use_cache)


def tmdb_search(kind: str, query: str, page: int = 1, use_cache: bool = True) -> dict:
    media_type = "tv" if (kind or "").lower() == "tv" else "movie"
    text = " ".join((query or "").split())
    page = max(1, to_int(page, 1))
    return _cached_get(f"tmdb-search:{media_type}:{text.lower()}:{page}", "/tmdb/search",
                       {"type": media_type, "q": text, "page": page}, use_cache)


def movie_details(movie_id: int | str, use_cache: bool = True) -> dict:
    mid = quote(str(movie_id), safe="")
    return _cached_get(f"movie:{mid}", f"/movie/{mid}", None, use_cache)


def tv_details(tv_id: int | str, use_cache: bool = True) -> dict:
    tid = quote(str(tv_id), safe="")
    return _cached_get(f"tv-details:{tid}", f"/tv/{tid}", None, use_cache)


def tv_season(tv_id: int | str, season_number: int | str, use_cache: bool = True) -> dict:
    tid = quote(str(tv_id), safe="")
    number = max(0, to_int(season_number))
    return _cached_get(f"tv-season:{tid}:{number}", f"/tv/{tid}/season/{number}", None, use_cache)


def release_info(current: str = "", channel: str = "stable") -> dict:
    params = {"channel": channel or "stable"}
    if current:
        params["current"] = current
    return _cached_get(f"update:{params.get('current', '')}:{params['channel']}", "/update", params, False)


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tbhb-api")


def _files_or_empty(torrent_id: str, use_cache: bool) -> tuple[list, bool]:
    try:
        return files(torrent_id, use_cache=use_cache).get("files") or [], False
    except TBHBError:
        return [], True


def _extras_or_empty(torrent_id: str, use_cache: bool) -> dict:
    try:
        d = torrent_extras(torrent_id, use_cache=use_cache)
    except TBHBError:
        return {}
    inner = d.get("torrent") if isinstance(d.get("torrent"), dict) else d
    return {k: inner[k] for k in ("description", "pageUrl") if inner.get(k)}


def details(torrent_id: str, use_cache: bool = True) -> dict:
    files_future = _executor.submit(_files_or_empty, torrent_id, use_cache)
    extras_future = _executor.submit(_extras_or_empty, torrent_id, use_cache)
    try:
        data = torrent(torrent_id, use_cache=use_cache)
    except Exception:
        files_future.cancel()
        extras_future.cancel()
        raise
    file_list, files_failed = files_future.result()
    inner = data.get("torrent") if isinstance(data.get("torrent"), dict) else data
    inner = {**inner, **extras_future.result()}
    return {"torrent": inner, "files": file_list, "files_error": files_failed, "_stale": bool(data.get("_stale"))}


def _or_empty(fn, *args) -> dict:
    try:
        return fn(*args)
    except TBHBError:
        return {}


def uploader_profile(username: str, use_cache: bool = True) -> dict:
    name = _name(username)
    if not name:
        return {}
    ratings_future = _executor.submit(_or_empty, user_ratings, name, use_cache)
    return {"reputation": _or_empty(uploader_reputation, name, use_cache), "ratings": ratings_future.result()}


def _nyaa_or_empty(query: str, use_cache: bool) -> list:
    try:
        return nyaa_rss(query, "0_0", use_cache=use_cache).get("results") or []
    except TBHBError:
        return []


def search_with_nyaa(query: str, category: int = 0, page: int = 0, use_cache: bool = True) -> dict:
    category = to_int(category)
    want_nyaa = page == 0 and category in (0, 200)
    nyaa_future = _executor.submit(_nyaa_or_empty, query, use_cache) if want_nyaa else None
    error = None
    try:
        data = search(query, category, page, use_cache)
    except TBHBError as exc:
        data, error = {"results": []}, exc
    extra: list = []
    if nyaa_future is not None:
        try:
            extra = nyaa_future.result(timeout=_NYAA_WAIT)
        except Exception:
            extra = []
    if error is not None and not extra:
        raise error
    if extra:
        seen = {str(r.get("info_hash") or "").lower() for r in data["results"] if isinstance(r, dict)}
        data = {**data, "results": [*data["results"],
                                    *[r for r in extra if (r.get("info_hash") or "").lower() not in seen]]}
    return data


def search_async(query: str, category: int, page: int, on_done=None, on_error=None, use_cache: bool = True) -> str:
    return submit(search_with_nyaa, args=(query, category, page), kwargs={"use_cache": use_cache}, on_done=on_done, on_error=on_error, priority=Priority.HIGH)


def details_async(torrent_id: str, on_done=None, on_error=None, use_cache: bool = True) -> str:
    return submit(details, args=(torrent_id,), kwargs={"use_cache": use_cache}, on_done=on_done, on_error=on_error, priority=Priority.HIGH)
