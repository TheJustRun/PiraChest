import difflib
import json
import logging
import os
import re
import struct
import time
from typing import Optional

logger = logging.getLogger(__name__)

_ANSI = 'mbcs' if os.name == 'nt' else 'cp1252'


def _library_dir() -> str:
    from src.core.config import paths
    d = os.path.join(paths.app_data_dir, 'library')
    os.makedirs(d, exist_ok=True)
    return d


def _slugify(title: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (title or 'game').lower()).strip('-') or 'game'


def list_games() -> list:
    try:
        with open(os.path.join(_library_dir(), 'games.json'), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_games(games: list) -> None:
    tmp = os.path.join(_library_dir(), 'games.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(games, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(_library_dir(), 'games.json'))


def upsert_game(record: dict) -> dict:
    games = list_games()
    gid = record.get('id') or _slugify(record.get('title', ''))
    record['id'] = gid
    for i, g in enumerate(games):
        if g.get('id') == gid:
            games[i] = {**g, **record}
            _save_games(games)
            return games[i]
    games.insert(0, record)
    _save_games(games)
    return record


def default_install_dir() -> str:
    try:
        from src.core.config import settings
        last = getattr(settings, 'last_install_dir', '')
        if last and os.path.isdir(last):
            return last
    except Exception:
        pass
    try:
        from src.core.config import settings as s, paths
        return getattr(s, 'download_dir_repacks', '') or paths.download_root
    except Exception:
        return os.path.expanduser('~')


def remember_install_dir(path: str) -> None:
    try:
        from src.core.config import settings as s, apply_settings, save_settings
        s.last_install_dir = path
        apply_settings(last_install_dir=path)
        save_settings(s)
    except Exception:
        logger.debug('Could not persist last_install_dir', exc_info=True)


def _read_cstr(buf: bytes, off: int) -> str:
    end = buf.find(b'\x00', off)
    return buf[off: end if end != -1 else len(buf)].decode(_ANSI, 'replace')


def _read_cwstr(buf: bytes, off: int) -> str:
    end = off
    while end + 2 <= len(buf) and buf[end:end + 2] != b'\x00\x00':
        end += 2
    return buf[off:end].decode('utf-16-le', 'replace')


def parse_lnk(path: str) -> Optional[dict]:
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError:
        return None
    if len(data) < 76 or data[:4] != b'L\x00\x00\x00':
        return None
    try:
        flags = struct.unpack_from('<I', data, 20)[0]
        uni = bool(flags & 0x80)
        pos = 76
        if flags & 0x1:
            pos += 2 + struct.unpack_from('<H', data, pos)[0]
        target = workdir = args = ''
        if flags & 0x2:
            li_size, hdr_size, li_flags = struct.unpack_from('<III', data, pos)
            if li_flags & 0x1:
                if uni and hdr_size >= 0x24:
                    off = struct.unpack_from('<I', data, pos + 0x1C)[0]
                    target = _read_cwstr(data, pos + off)
                else:
                    off = struct.unpack_from('<I', data, pos + 0x10)[0]
                    target = _read_cstr(data, pos + off)
            pos += li_size

        def _strdata(p: int):
            if p + 2 > len(data):
                return p, ''
            n = struct.unpack_from('<H', data, p)[0]
            p += 2
            if uni:
                s, p = data[p:p + n * 2].decode('utf-16-le', 'replace'), p + n * 2
            else:
                s, p = data[p:p + n].decode(_ANSI, 'replace'), p + n
            return p, s.rstrip('\x00')

        if flags & 0x4:
            pos, _ = _strdata(pos)
        if flags & 0x8:
            pos, _ = _strdata(pos)
        if flags & 0x10:
            pos, workdir = _strdata(pos)
        if flags & 0x20:
            pos, args = _strdata(pos)
    except Exception:
        logger.debug('Failed to parse .lnk %s', path, exc_info=True)
        return None
    return {'target': target, 'workdir': workdir, 'args': args, 'lnk': path}


def _shortcut_roots() -> list:
    home = os.path.expanduser('~')
    roots = [
        os.path.join(home, 'Desktop'),
        os.path.join(os.environ.get('OneDrive', ''), 'Desktop'),
        os.path.join(os.environ.get('PUBLIC', 'C:/Users/Public'), 'Desktop'),
    ]
    for env in ('APPDATA', 'PROGRAMDATA'):
        base = os.environ.get(env)
        if base:
            roots.append(os.path.join(base, 'Microsoft', 'Windows', 'Start Menu', 'Programs'))
    return [r for r in roots if r and os.path.isdir(r)]


def _lnk_candidates(since_ts: float):
    for root in _shortcut_roots():
        for dp, _dirs, files in os.walk(root):
            for f in files:
                if not f.lower().endswith('.lnk'):
                    continue
                p = os.path.join(dp, f)
                try:
                    if os.path.getmtime(p) >= since_ts:
                        yield p
                except OSError:
                    continue


def find_game_shortcut(install_dir: str, title: str, since_ts: float, timeout_s: float = 15.0) -> Optional[dict]:
    install_dir_n = os.path.normcase(os.path.abspath(install_dir))
    title_slug = _slugify(title or '')
    deadline, seen, best = time.time() + timeout_s, set(), None
    while time.time() < deadline:
        for p in _lnk_candidates(since_ts):
            if p in seen:
                continue
            seen.add(p)
            info = parse_lnk(p)
            if not info:
                continue
            tgt = info.get('target') or ''
            if not tgt.lower().endswith('.exe'):
                continue
            if not os.path.normcase(tgt).startswith(install_dir_n):
                continue
            base = os.path.splitext(os.path.basename(tgt))[0]
            score = difflib.SequenceMatcher(None, title_slug, _slugify(base)).ratio()
            if 'desktop' in os.path.dirname(p).lower():
                score += 0.05
            if best is None or score > best[0]:
                best = (score, info)
        if best:
            break
        time.sleep(0.5)
    return best[1] if best else None


def delete_lnk(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        logger.debug('Could not delete shortcut %s', path, exc_info=True)


_REDIST_HINTS = ('redist', 'directx', 'vcredist', '_commonredist', 'unins', 'support',
                 'engine\\extras', 'dotnet', 'crack')


def fallback_exe(install_dir: str) -> str:
    best = None
    for dp, dirs, files in os.walk(install_dir):
        rel = os.path.relpath(dp, install_dir).lower()
        if rel != '.' and any(h in rel for h in _REDIST_HINTS):
            dirs[:] = []
            continue
        depth = 0 if rel == '.' else rel.count(os.sep)
        for f in files:
            low = f.lower()
            if not low.endswith('.exe') or low.startswith(('unins', 'setup', 'update')):
                continue
            p = os.path.join(dp, f)
            try:
                w = os.path.getsize(p) / (1 + depth * 10)
            except OSError:
                continue
            if best is None or w > best[0]:
                best = (w, p)
    return best[1] if best else ''
