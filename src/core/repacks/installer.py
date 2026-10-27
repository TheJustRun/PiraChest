from __future__ import annotations
import ctypes
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from ctypes import wintypes

from PySide6.QtCore import QObject, QThread, Signal

from . import (_slugify, delete_lnk, fallback_exe, find_game_shortcut,
               _library_dir, remember_install_dir, upsert_game)

logger = logging.getLogger(__name__)

_SIZE_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*(TB|GB|MB)', re.IGNORECASE)
_UNITS = {'TB': 1 << 40, 'GB': 1 << 30, 'MB': 1 << 20}
_FITGIRL_TASKS = {'directx': 'directx', 'vcredist': 'vcredist', 'startmenu': 'startmenuicon', 'ram2gb': 'ram2gb'}

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NO_CONSOLE = 0x00008000
SW_HIDE = 0
STILL_ACTIVE = 259
ERROR_CANCELLED = 1223


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong), ('hwnd', wintypes.HWND),
        ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
        ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
        ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
        ('dwHotKey', wintypes.DWORD), ('hIcon', wintypes.HANDLE), ('hProcess', wintypes.HANDLE),
    ]


class ElevationCancelled(Exception):
    pass


class _ElevatedProcess:
    def __init__(self, hprocess: int):
        self._h = hprocess
        self.returncode = None
        self.pid = ctypes.windll.kernel32.GetProcessId(hprocess) if hprocess else 0

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if not self._h:
            self.returncode = 0
            return self.returncode
        code = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(self._h, ctypes.byref(code)):
            return None
        if code.value == STILL_ACTIVE:
            return None
        self.returncode = code.value
        return self.returncode

    def kill(self):
        if self._h:
            try:
                ctypes.windll.kernel32.TerminateProcess(self._h, 1)
            except OSError:
                pass

    def close(self):
        if self._h:
            try:
                ctypes.windll.kernel32.CloseHandle(self._h)
            except OSError:
                pass
            self._h = None


def _launch_elevated(exe_path: str, args: list, cwd: str = None) -> _ElevatedProcess:
    params = subprocess.list2cmdline(args)
    sei = _SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    sei.hwnd = None
    sei.lpVerb = 'runas'
    sei.lpFile = exe_path
    sei.lpParameters = params
    sei.lpDirectory = cwd or os.path.dirname(exe_path) or None
    sei.nShow = SW_HIDE
    sei.hInstApp = None
    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
    if not ok:
        err = ctypes.GetLastError()
        if err == ERROR_CANCELLED:
            raise ElevationCancelled()
        raise OSError(f'ShellExecuteEx failed (WinError {err})')
    return _ElevatedProcess(sei.hProcess)


def _popen_kwargs() -> dict:
    return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {}


def parse_size_bytes(text: str) -> int:
    total = 0
    for m in _SIZE_RE.finditer(text or ''):
        try:
            v = float(m.group(1).replace(',', '.'))
        except ValueError:
            continue
        total = max(total, int(v * _UNITS[m.group(2).upper()]))
    return total


def build_fitgirl_switches(options: dict, install_dir: str) -> list:
    tasks = ['desktopicon']
    if options.get('startmenu'):
        tasks.append(_FITGIRL_TASKS['startmenu'])
    if options.get('directx'):
        tasks.append(_FITGIRL_TASKS['directx'])
    if options.get('vcredist'):
        tasks.append(_FITGIRL_TASKS['vcredist'])
    if options.get('ram2gb'):
        tasks.append(_FITGIRL_TASKS['ram2gb'])
    args = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
            f'/DIR={install_dir}', '/TASKS=' + ','.join(tasks)]


    inno = options.get('inno') or {}
    if inno.get('types'):
        args.append('/TYPE=' + inno['types'])
    if inno.get('components'):
        args.append('/COMPONENTS=' + inno['components'])
    return args


_INNOUNP_CANDIDATES = ('innounp.exe', 'innounp')

_SECTION_RE = re.compile(r'^\[(\w+)\]\s*$')
_KV_RE = re.compile(r'^\s*([A-Za-z]+)\s*:\s*(.*?)\s*$')


_REDIST_TOKENS = ('directx', 'vc_2022_x86', 'vc_2022_x64', 'vcredist', 'vc_redist', 'vc_20', 'msvc', 'visualcplusplus')
_VERIFY_TOKENS = ('quicksfv', 'verify', 'checksum', 'md5')
_LOCAL_TOKEN = 'local'
_LANGUAGE_HINTS = ('speech', 'voice', 'voiceover', 'audio', 'dub', 'lang', 'text', 'subtitle',
                   'sub', 'localization', 'localisation', 'translat')


def _has_local_children(name: str, names: set) -> bool:
    return any(other != name and (other.startswith(name + '\\') or other.startswith(name + '/')) for other in names)


def _local_parent(name: str) -> str | None:
    for sep in ('\\', '/'):
        if sep in name:
            return name.rsplit(sep, 1)[0]
    return None


def _nearest_named_local_ancestor(name: str, names: set) -> str | None:
    parent = _local_parent(name)
    while parent is not None:
        if parent in names:
            return parent
        parent = _local_parent(parent)
    return None


def _is_language_related(name: str, description: str) -> bool:
    blob = f'{name} {description}'.lower()
    return any(tok in blob for tok in _LANGUAGE_HINTS)


def _find_innounp() -> str:
    try:
        from src.core.config import paths
        bundled = os.path.join(getattr(paths, 'tools_dir', ''), 'innounp.exe')
        if os.path.isfile(bundled):
            return bundled
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ('tools', os.path.join('..', '..', 'tools'), os.path.join('..', 'tools')):
        for name in _INNOUNP_CANDIDATES:
            p = os.path.join(here, rel, name)
            if os.path.isfile(p):
                return p
    from shutil import which
    return which('innounp') or ''


def _parse_inno_kv_block(lines: list, start: int) -> tuple:
    records = []
    i = start
    while i < len(lines):
        line = lines[i]
        if _SECTION_RE.match(line.strip()):
            break
        stripped = line.strip()
        if not stripped or stripped.startswith(';'):
            i += 1
            continue
        while stripped.endswith('\\') and i + 1 < len(lines):
            i += 1
            stripped = stripped[:-1].rstrip() + ' ' + lines[i].strip()
        record: dict = {}
        for part in stripped.split(';'):
            part = part.strip()
            if not part:
                continue
            m = _KV_RE.match(part)
            if m:
                record[m.group(1).lower()] = m.group(2).strip('"')
        if record:
            records.append(record)
        i += 1
    return records, i


def _parse_inno_script(text: str) -> dict:
    lines = text.replace('\r\n', '\n').split('\n')
    sections: dict = {'Types': [], 'Components': [], 'Run': [], 'Files': []}
    i = 0
    while i < len(lines):
        m = _SECTION_RE.match(lines[i].strip())
        if m and m.group(1) in sections:
            records, i = _parse_inno_kv_block(lines, i + 1)
            sections[m.group(1)].extend(records)
        else:
            i += 1
    return sections


def _run_innounp_extract(setup_path: str, out_dir: str) -> bool:
    exe = _find_innounp()
    if not exe:
        logger.warning('innounp not found; cannot inspect Inno Setup script')
        return False


    attempts = [
        [exe, '-x', '-y', '-b', f'-d{out_dir}', setup_path, 'install_script.iss'],
        [exe, '-x', '-y', '-b', f'-d{out_dir}', setup_path, '*.iss'],
    ]
    last_output = ''
    last_code = None
    for args in attempts:
        try:
            proc = subprocess.run(args, **_popen_kwargs(), stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=20, text=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning('innounp extraction failed to launch: %s', exc)
            return False
        last_output, last_code = (proc.stdout or ''), proc.returncode
        extracted_any = os.path.isdir(out_dir) and any(os.scandir(out_dir))
        if proc.returncode == 0 and extracted_any:
            logger.debug('innounp extracted to %s: %s', out_dir, os.listdir(out_dir))
            return True
    logger.warning('innounp exited with code %s for %s: %s', last_code, setup_path, last_output.strip()[-2000:])
    return False


def _find_extracted_script(out_dir: str) -> str:
    candidates = []
    for dp, _dirs, files in os.walk(out_dir):
        for f in files:
            low = f.lower()
            p = os.path.join(dp, f)
            if low.endswith('.iss') or low == 'install_script.iss':
                return p
            candidates.append(p)


    scored = []
    for p in candidates:
        try:
            with open(p, 'rb') as fh:
                head = fh.read(65536)
        except OSError:
            continue
        try:
            text_head = head.decode('utf-8')
        except UnicodeDecodeError:
            try:
                text_head = head.decode('cp1252')
            except UnicodeDecodeError:
                continue
        if '[' not in text_head:
            continue
        hits = sum(text_head.count(f'[{name}]') for name in ('Types', 'Components', 'Run', 'Files', 'Setup'))
        if hits:
            try:
                sz = os.path.getsize(p)
            except OSError:
                sz = 0
            scored.append((hits, sz, p))
    if scored:
        scored.sort(reverse=True)
        return scored[0][2]
    return ''


def _classify_component(name: str) -> str:
    low = (name or '').lower()
    if any(tok in low for tok in _REDIST_TOKENS):
        return 'redist'
    if any(tok in low for tok in _VERIFY_TOKENS):
        return 'verify'
    if low == _LOCAL_TOKEN or low.startswith(_LOCAL_TOKEN + '/') or low.startswith(_LOCAL_TOKEN + '\\'):
        return 'local'
    return 'other'


def inspect_inno_setup(setup_path: str) -> dict:
    result = {'available': False, 'types': [], 'redist': None, 'verify': None,
              'local': None, 'other': [], 'raw': {}}
    if not setup_path or not os.path.isfile(setup_path):
        logger.warning('inspect_inno_setup: setup_path missing or not a file: %r', setup_path)
        return result
    import tempfile
    with tempfile.TemporaryDirectory(prefix='inno_inspect_') as tmp:
        if not _run_innounp_extract(setup_path, tmp):
            return result
        script_path = _find_extracted_script(tmp)
        if not script_path:
            logger.warning('inspect_inno_setup: innounp extracted files but no Inno script found among them (setup=%s)', setup_path)
            return result
        try:
            with open(script_path, 'r', encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        except OSError as exc:
            logger.warning('inspect_inno_setup: failed reading extracted script %s: %s', script_path, exc)
            return result
    sections = _parse_inno_script(text)
    if not any(sections.values()):
        logger.warning('inspect_inno_setup: script found (%s) but no [Types]/[Components]/[Run]/[Files] sections parsed', script_path)
        return result
    result['available'] = True
    result['raw'] = sections
    result['types'] = [r.get('name', '') for r in sections['Types'] if r.get('name')]
    default_type = result['types'][0] if result['types'] else ''

    def _default_checked(rec: dict) -> bool:
        flags = (rec.get('flags', '') or '').lower()
        if 'unchecked' in flags:
            return False
        types_field = (rec.get('types') or '').strip()
        if not types_field:
            return False
        rec_types = {t.strip() for t in types_field.split() if t.strip()}
        return bool(default_type) and default_type in rec_types

    redist_names, verify_names = [], []
    local_by_name: dict = {}
    other_by_name: dict = {}
    type_names = set(result['types'])
    for rec in sections['Components']:
        name = rec.get('name', '')
        if not name:
            continue
        kind = _classify_component(name)
        lang = (rec.get('languages') or '').lower()
        if kind == 'redist':
            redist_names.append(name)
        elif kind == 'verify':
            verify_names.append(name)
        elif kind == 'local':
            prev = local_by_name.get(name)
            if prev is None or lang in ('', 'en'):
                local_by_name[name] = {'name': name, 'description': rec.get('description', name),
                                       'default_checked': _default_checked(rec)}
        else:
            rec_types = {t.strip() for t in (rec.get('types') or '').split() if t.strip()}
            is_core = bool(type_names) and rec_types >= type_names
            flags = (rec.get('flags', '') or '').lower()
            entry = {'name': name, 'description': rec.get('description', name),
                     'required': flags == 'fixed' or is_core,
                     'default_checked': _default_checked(rec)}
            prev = other_by_name.get(name)
            if prev is None or lang in ('', 'en'):
                other_by_name[name] = entry


    local_names = set(local_by_name.keys())
    lang_groups, lang_items = [], []
    for name in sorted(local_names):
        if name.lower() == _LOCAL_TOKEN:
            continue
        rec = local_by_name[name]
        is_group = _has_local_children(name, local_names)
        ancestor = _nearest_named_local_ancestor(name, local_names)
        ancestor_rec = local_by_name.get(ancestor) if ancestor and ancestor.lower() != _LOCAL_TOKEN else None
        lang_related = (_is_language_related(name, rec['description'])
                        or (ancestor_rec is not None
                            and _is_language_related(ancestor, ancestor_rec['description'])))
        if is_group:
            if lang_related:
                lang_groups.append({'name': name, 'label': rec['description']})
            continue
        if lang_related:
            group_name = ancestor if (ancestor and ancestor.lower() != _LOCAL_TOKEN) else None
            lang_items.append({**rec, 'group': group_name})
        else:
            other_by_name.setdefault(name, {'name': name, 'description': rec['description'],
                                            'required': False, 'default_checked': rec['default_checked']})
    other = list(other_by_name.values())


    verify_ungated = False
    for rec in sections['Run']:
        if rec.get('components'):
            continue
        desc = (rec.get('description') or rec.get('filename') or '')
        if any(tok in desc.lower() for tok in _VERIFY_TOKENS):
            verify_ungated = True

    if redist_names:
        result['redist'] = {'names': sorted(set(redist_names))}
    if verify_names:
        result['verify'] = {'names': sorted(set(verify_names)), 'ungated': False}
    elif verify_ungated:
        result['verify'] = {'names': [], 'ungated': True}
    if lang_items:
        result['local'] = {'children': [it['name'] for it in lang_items],
                           'items': lang_items, 'groups': lang_groups}
    result['other'] = other
    return result


def build_inno_selection(inspected: dict, *, redist: bool, verify: bool,
                          local_children: list, other_selected: list) -> dict:
    if not inspected or not inspected.get('available'):
        return {}
    components = [o['name'] for o in (inspected.get('other') or []) if o.get('required')]
    if redist and inspected.get('redist'):
        components.extend(inspected['redist']['names'])
    if verify and inspected.get('verify'):
        components.extend(inspected['verify']['names'])
    if local_children:
        components.extend(local_children)
    if other_selected:
        components.extend(other_selected)
    sel = {}
    if components:
        sel['components'] = ','.join(sorted(set(components)))
    return sel


def find_setup_exe(root: str, max_depth: int = 6, min_mtime: float = 0.0):
    root = os.path.abspath(root)
    best = None
    for dp, dirs, files in os.walk(root):
        if os.path.relpath(dp, root).count(os.sep) >= max_depth:
            dirs[:] = []
        for f in files:
            if f.lower() != 'setup.exe':
                continue
            p = os.path.join(dp, f)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt < min_mtime:
                continue
            if best is None or mt > best[1]:
                best = (p, mt)
    return best[0] if best else None


def find_setup_exe_polling(roots: list, min_mtime: float, timeout_s: float, should_cancel=None) -> str:
    timeout_s = min(timeout_s, 30.0)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for root in roots:
            if root and os.path.isdir(root):
                found = find_setup_exe(root, min_mtime=min_mtime)
                if found:
                    return found
        if should_cancel and should_cancel():
            return ''
        time.sleep(0.5)
    for root in roots:
        if root and os.path.isdir(root):
            found = find_setup_exe(root, min_mtime=0.0)
            if found:
                return found
    return ''


def installed_size(path: str) -> int:
    total = 0
    for dp, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


_MD5_LINE_RE = re.compile(r'^\s*([0-9a-fA-F]{32})\s+\*?(.+?)\s*$')


def find_md5_manifest(root: str):
    for name in ('fitgirl.md5', 'FitGirl.md5', 'FITGIRL.MD5'):
        p = os.path.join(root, '_redist', name)
        if os.path.isfile(p):
            return p
    for dp, _dirs, files in os.walk(root):
        for f in files:
            if f.lower() == 'fitgirl.md5':
                return os.path.join(dp, f)
    return None


def _manifest_rel_ok(rel: str) -> bool:
    rel = (rel or '').strip().replace('\\', '/')
    if not rel or rel.startswith('/') or re.match(r'^[a-zA-Z]:', rel):
        return False
    return not any(part == '..' for part in rel.split('/'))


def _safe_manifest_path(base: str, rel: str) -> str | None:
    if not _manifest_rel_ok(rel):
        return None
    parts = [part for part in rel.replace('\\', '/').split('/') if part not in ('', '.')]
    full = os.path.join(base, *parts)
    try:
        if os.path.commonpath([os.path.realpath(base), os.path.realpath(full)]) != os.path.realpath(base):
            return None
    except (OSError, ValueError):
        return None
    return full


def parse_md5_manifest(path: str) -> dict:
    entries = {}
    manifest_dir = os.path.dirname(path)
    base = os.path.dirname(manifest_dir)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                m = _MD5_LINE_RE.match(line)
                if not m:
                    continue
                digest, rel = m.group(1).lower(), m.group(2).strip().replace('\\', '/')
                if not _manifest_rel_ok(rel) or _safe_manifest_path(base, rel) is None:
                    logger.warning("Ignoring path outside the install folder in %s: %r", path, rel)
                    continue
                entries[rel] = digest
    except OSError:
        return {}
    return {'base': base, 'files': entries}


def _md5_of(path: str, chunk_size: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class VerifyResult:
    def __init__(self):
        self.ok: list = []
        self.mismatched: list = []
        self.missing: list = []

    def summary(self) -> str:
        return f'{len(self.ok)} OK, {len(self.mismatched)} errors, {len(self.missing)} warnings'


def verify_repack(root: str, progress_cb=None, should_cancel=None) -> VerifyResult:
    result = VerifyResult()
    manifest_path = find_md5_manifest(root)
    if not manifest_path:
        return result
    manifest = parse_md5_manifest(manifest_path)
    base, files = manifest.get('base', root), manifest.get('files', {})
    total = len(files)
    for i, (rel, expected) in enumerate(files.items()):
        if should_cancel and should_cancel():
            break
        full = _safe_manifest_path(base, rel)
        if full is None:
            continue
        if progress_cb:
            progress_cb(i + 1, total, rel)
        if not os.path.isfile(full):
            result.missing.append(rel)
            continue
        try:
            actual = _md5_of(full)
        except OSError:
            result.missing.append(rel)
            continue
        if actual == expected:
            result.ok.append(rel)
        else:
            result.mismatched.append(rel)
    return result


def repair_mismatched(root: str, result: VerifyResult) -> None:
    base = os.path.dirname(find_md5_manifest(root) or root) or root
    base = os.path.dirname(base) if os.path.basename(base).lower() == '_redist' else base
    for rel in result.mismatched:
        full = _safe_manifest_path(base, rel)
        if full is None:
            continue
        try:
            os.remove(full)
        except OSError:
            pass


def _kill_bundled_verifier(install_dir: str, duration_s: float = 5.0) -> None:
    if os.name != 'nt':
        return
    names = ('fg-md5.exe', 'verifyinstall.exe', 'checkfiles.exe', 'quicksfv.exe')
    deadline = time.time() + duration_s
    while time.time() < deadline:
        for name in names:
            try:
                subprocess.run(['taskkill', '/F', '/IM', name], **_popen_kwargs(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
        time.sleep(1.0)


_CLSID_MMDeviceEnumerator = '{BCDE0395-E52F-467C-8E3D-C4579291692E}'
_IID_IMMDeviceEnumerator = '{A95664D2-9614-4F35-A746-DE8DB63617E6}'
_IID_IAudioEndpointVolume = '{5CDF2C82-841E-4546-9722-0CF74078229A}'
CLSCTX_ALL = 23
eRender, eConsole = 0, 0


class _GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_ulong), ('Data2', ctypes.c_ushort), ('Data3', ctypes.c_ushort),
                ('Data4', ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    g = _GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


def _com_call(ptr, index, restype, argtypes, *args):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.cast(vtbl[index], ctypes.CFUNCTYPE(restype, ctypes.c_void_p, *argtypes))
    return fn(ptr, *args)


_IID_IAudioSessionManager2 = '{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}'
_IID_IAudioSessionControl2 = '{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}'
_IID_ISimpleAudioVolume = '{87CE5498-68D6-44E5-9215-6DA47EF883D8}'


def _get_child_pids(root_pid: int) -> set:
    pids = {root_pid}
    try:
        out = subprocess.run(['wmic', 'process', 'get', 'ProcessId,ParentProcessId'],
                             **_popen_kwargs(), capture_output=True, text=True, timeout=5)
        rows = [line.split() for line in out.stdout.splitlines()[1:] if line.strip()]
        by_parent: dict = {}
        for row in rows:
            if len(row) != 2:
                continue
            try:
                ppid, pid = int(row[0]), int(row[1])
            except ValueError:
                continue
            by_parent.setdefault(ppid, []).append(pid)
        frontier = [root_pid]
        while frontier:
            p = frontier.pop()
            for child in by_parent.get(p, []):
                if child not in pids:
                    pids.add(child)
                    frontier.append(child)
    except Exception:
        logger.debug('Failed to enumerate child processes of %s', root_pid, exc_info=True)
    return pids


def _mute_process_tree_sessions(root_pid: int, mute: bool) -> None:
    ctypes.windll.ole32.CoInitializeEx(None, 2)
    try:
        pids = _get_child_pids(root_pid)
        clsid = _guid(_CLSID_MMDeviceEnumerator)
        iid_enum = _guid(_IID_IMMDeviceEnumerator)
        enumerator = ctypes.c_void_p()
        hr = ctypes.windll.ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid_enum), ctypes.byref(enumerator))
        if hr != 0 or not enumerator:
            return
        device = ctypes.c_void_p()
        hr = _com_call(enumerator, 4, ctypes.c_long, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                        eRender, eConsole, ctypes.byref(device))
        if hr != 0 or not device:
            return
        iid_mgr = _guid(_IID_IAudioSessionManager2)
        mgr = ctypes.c_void_p()
        hr = _com_call(device, 3, ctypes.c_long,
                        [ctypes.POINTER(_GUID), ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                        ctypes.byref(iid_mgr), CLSCTX_ALL, None, ctypes.byref(mgr))
        if hr != 0 or not mgr:
            return
        session_enum = ctypes.c_void_p()
        hr = _com_call(mgr, 5, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(session_enum))
        if hr != 0 or not session_enum:
            return
        count = ctypes.c_int(0)
        _com_call(session_enum, 3, ctypes.c_long, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(count))
        iid_ctl2 = _guid(_IID_IAudioSessionControl2)
        iid_vol = _guid(_IID_ISimpleAudioVolume)
        for i in range(count.value):
            ctl = ctypes.c_void_p()
            hr = _com_call(session_enum, 4, ctypes.c_long, [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], i, ctypes.byref(ctl))
            if hr != 0 or not ctl:
                continue
            ctl2 = ctypes.c_void_p()
            hr = _com_call(ctl, 0, ctypes.c_long, [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                            ctypes.byref(iid_ctl2), ctypes.byref(ctl2))
            if hr != 0 or not ctl2:
                continue
            pid = ctypes.c_ulong(0)
            hr = _com_call(ctl2, 14, ctypes.c_long, [ctypes.POINTER(ctypes.c_ulong)], ctypes.byref(pid))
            if hr == 0 and pid.value in pids:
                vol = ctypes.c_void_p()
                hr = _com_call(ctl, 0, ctypes.c_long, [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                                ctypes.byref(iid_vol), ctypes.byref(vol))
                if hr == 0 and vol:
                    _com_call(vol, 9, ctypes.c_long, [ctypes.c_int, ctypes.c_void_p], int(bool(mute)), None)
    except Exception:
        logger.debug('Failed to (un)mute install process tree', exc_info=True)
    finally:
        ctypes.windll.ole32.CoUninitialize()


def _set_process_tree_mute(root_pid: int, mute: bool) -> None:
    if os.name != 'nt' or not root_pid:
        return
    import threading
    t = threading.Thread(target=_mute_process_tree_sessions, args=(root_pid, mute), daemon=True)
    t.start()
    t.join(timeout=1.0)


class FitGirlInstallWorker(QThread):
    progress = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, spec: dict, roots: list, parent=None):
        super().__init__(parent)
        self._spec, self._roots = spec, roots
        self._cancel = False
        self._t_start = time.time()

    def cancel(self):
        self._cancel = True

    def _minimal(self, install_dir: str) -> list:
        return ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/LANG=english', f'/DIR={install_dir}']

    def _start(self, setup_path: str, args: list, install_dir: str):
        if os.name == 'nt':
            return _launch_elevated(setup_path, args, cwd=install_dir)
        return subprocess.Popen([setup_path] + args, cwd=os.path.dirname(setup_path) or None, **_popen_kwargs())

    def _run_scan(self, root: str, label: str) -> bool:
        self.progress.emit(-1, f'{label}: scanning…')
        result = verify_repack(
            root,
            progress_cb=lambda i, n, rel: self.progress.emit(
                int(i / n * 100) if n else -1, f'{label}: {rel}'),
            should_cancel=lambda: self._cancel,
        )
        if self._cancel:
            return False
        if not result.ok and not result.mismatched and not result.missing:
            return True
        if result.mismatched:
            repair_mismatched(root, result)
            self.progress.emit(-1, f'{label}: {result.summary()} — removed corrupted files')
        else:
            self.progress.emit(-1, f'{label}: {result.summary()}')
        if result.missing:
            self.failed.emit(f'{label} found {len(result.missing)} missing file(s); the download is incomplete')
            return False
        return True

    def run(self):
        spec = self._spec
        install_dir = os.path.abspath(spec['install_dir'])
        self.progress.emit(-1, 'Locating installer…')
        min_mtime = spec.get('queued_at', 0.0) - 10
        setup_path = find_setup_exe_polling(self._roots, min_mtime, 120.0, should_cancel=lambda: self._cancel)
        if self._cancel:
            self.failed.emit('Installation cancelled')
            return
        if not setup_path:
            self.failed.emit('setup.exe was not found in the download folder')
            return
        repack_root = os.path.dirname(setup_path)

        if spec.get('scan_before'):
            if not self._run_scan(repack_root, 'Pre-install scan'):
                return

        self.progress.emit(-1, 'Preparing installer…')
        try:
            os.makedirs(install_dir, exist_ok=True)
        except OSError as exc:
            self.failed.emit(f'Cannot create install directory: {exc}')
            return
        minimal = self._minimal(install_dir)
        attempts = [minimal + build_fitgirl_switches(spec.get('options', {}), install_dir)[3:], minimal]
        code = None
        muted_pid = 0
        try:
            for i, args in enumerate(attempts):
                try:
                    proc = self._start(setup_path, args, install_dir)
                except ElevationCancelled:
                    self.failed.emit('Installation cancelled: administrator permission was not granted')
                    return
                except OSError as exc:
                    self.failed.emit(f'Failed to start setup.exe: {exc}')
                    return
                if getattr(proc, 'pid', 0):
                    muted_pid = proc.pid
                    _set_process_tree_mute(muted_pid, True)
                t0, last = time.time(), 0.0
                while proc.poll() is None:
                    if self._cancel:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        self.failed.emit('Installation cancelled')
                        return
                    if time.time() - last >= 3.0:
                        last = time.time()
                        done, expected = installed_size(install_dir), spec.get('size_bytes') or 0
                        pct = int(min(96, done / expected * 94)) + 2 if expected > 0 else -1
                        self.progress.emit(pct, f'Extracting game files…   {done / (1 << 30):.1f} GB')
                        if muted_pid:
                            _set_process_tree_mute(muted_pid, True)
                    time.sleep(0.5)
                if hasattr(proc, 'close'):
                    proc.close()
                code = proc.returncode
                if code in (0, 3010):
                    break
                if time.time() - t0 < 12 and i + 1 < len(attempts):
                    logger.info('FitGirl silent install failed fast (code %s); retrying with defaults', code)
                    continue
                self.failed.emit(f'Installer exited with code {code}')
                return
        finally:
            if muted_pid:
                _set_process_tree_mute(muted_pid, False)

        if not spec.get('verify_installer', True):
            _kill_bundled_verifier(install_dir)

        if spec.get('scan_after'):
            self._run_scan(install_dir, 'Post-install scan')

        self.progress.emit(97, 'Detecting game shortcut…')
        shortcut = find_game_shortcut(install_dir, spec['title'], since_ts=self._t_start - 5, timeout_s=15)
        exe = cwd = args = ''
        via, lnk = 'none', ''
        if shortcut:
            exe = shortcut.get('target') or ''
            cwd = shortcut.get('workdir') or ''
            args = shortcut.get('args') or ''
            lnk = shortcut.get('lnk') or ''
            via = 'shortcut'
        if not exe:
            exe = fallback_exe(install_dir)
            via = 'fallback' if exe else 'none'
        if exe and not cwd:
            cwd = os.path.dirname(exe)
        if lnk:
            delete_lnk(lnk)

        record = {
            'id': _slugify(spec['title']), 'title': spec['title'], 'source': 'fitgirl',
            'install_dir': install_dir, 'exe': exe, 'cwd': cwd, 'args': args,
            'state': 'installed' if exe else 'no-exe', 'detected_via': via,
            'installed_at': int(time.time()), 'size_bytes': spec.get('size_bytes') or 0,
            'cover_url': spec.get('cover_url', ''), 'cover_path': '',
            'description': spec.get('description', ''),
            'metadata': spec.get('metadata') or {},
            'screenshots': spec.get('screenshots') or [], 'backgrounds': [],
        }
        upsert_game(record)
        self.progress.emit(100, 'Done')
        self.succeeded.emit(record)


class InstallCoordinator(QObject):
    install_started = Signal(dict)
    install_progress = Signal(str, int, str)
    install_succeeded = Signal(object)
    install_failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending: dict = {}
        self._worker: FitGirlInstallWorker | None = None
        self._manager = None

    def _pending_file(self) -> str:
        return os.path.join(_library_dir(), 'pending.json')

    def _save_pending(self):
        try:
            with open(self._pending_file(), 'w', encoding='utf-8') as fh:
                json.dump(self._pending, fh)
        except OSError:
            pass

    def bind(self, manager):
        if self._manager is not None:
            return
        self._manager = manager
        manager.download_completed.connect(self.on_download_completed)
        try:
            manager.item_removed.connect(self._on_item_removed)
        except Exception:
            pass
        try:
            with open(self._pending_file(), 'r', encoding='utf-8') as fh:
                self._pending = json.load(fh) or {}
        except Exception:
            self._pending = {}

    def register_pending(self, item_id: str, spec: dict):
        spec = dict(spec)
        spec.setdefault('queued_at', time.time())
        self._pending[item_id] = spec
        self._save_pending()

    def _on_item_removed(self, item_id: str):
        if self._pending.pop(item_id, None) is not None:
            self._save_pending()

    def on_download_completed(self, item_id: str, game_name: str):
        spec = self._pending.pop(item_id, None)
        self._save_pending()
        if not spec or not spec.get('enabled'):
            return
        roots = self._collect_roots(item_id)
        self.start_install(spec, roots)

    def _collect_roots(self, item_id: str) -> list:
        roots = []
        if self._manager is not None:
            item = self._manager.get(item_id)
            objs = [item]
            sub = getattr(item, 'svc', None) or getattr(item, 'service', None)
            if sub is not None:
                objs.append(sub)
            for obj in objs:
                for attr in ('save_path', 'save_dir', 'path', 'dest_path', 'download_path',
                             'output_dir', 'output_path', 'folder', 'directory'):
                    v = getattr(obj, attr, None)
                    if isinstance(v, str) and v:
                        roots.append(v)
                        roots.append(os.path.dirname(v))
        try:
            from src.core.config import settings
            roots.append(getattr(settings, 'download_dir_repacks', ''))
        except Exception:
            pass
        seen, unique = set(), []
        for r in roots:
            r = os.path.abspath(r) if r else r
            if r and r not in seen and os.path.isdir(r):
                seen.add(r)
                unique.append(r)
        return unique

    def is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def cancel(self):
        if self._worker is not None:
            self._worker.cancel()

    def shutdown(self, wait_ms: int = 3000):
        worker = self._worker
        if worker is None:
            return
        try:
            worker.cancel()
        except Exception:
            pass
        try:
            if worker.isRunning():
                worker.wait(wait_ms)
        except RuntimeError:
            return
        if not worker.isRunning():
            self._worker = None

    def start_install(self, spec: dict, roots: list):
        if self.is_busy():
            self.install_failed.emit(spec['title'], 'Another installation is already running')
            return
        try:
            remember_install_dir(os.path.dirname(spec['install_dir']))
        except Exception:
            pass
        self._worker = FitGirlInstallWorker(spec, roots)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.finished.connect(lambda w=self._worker: self._on_worker_finished(w))
        self._worker.progress.connect(lambda p, s, t=spec['title']: self.install_progress.emit(t, p, s))
        self._worker.succeeded.connect(self._on_done)
        self._worker.failed.connect(lambda msg, t=spec['title']: self.install_failed.emit(t, msg))
        self.install_started.emit(spec)
        self._worker.start()

    def _on_worker_finished(self, worker):
        if self._worker is worker:
            self._worker = None

    def _on_done(self, record):
        self._worker = None
        self.install_succeeded.emit(record)


_coordinator: InstallCoordinator | None = None


def get_install_coordinator(parent=None) -> InstallCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = InstallCoordinator(parent)
    return _coordinator
