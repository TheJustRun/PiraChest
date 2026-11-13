from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _numeric_version(version: str) -> tuple[int, int, int, int]:
    match = re.match(r'^\s*(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?', version)
    if not match:
        return (0, 0, 0, 0)
    return tuple(min(int(part or 0), 65535) for part in match.groups())


def _quote(value: str) -> str:
    return str(value).replace('\\', '\\\\').replace("'", "\\'")


_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={filevers},
    prodvers={prodvers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', '{company}'),
         StringStruct('FileDescription', '{description}'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', '{internal}'),
         StringStruct('LegalCopyright', '{copyright}'),
         StringStruct('OriginalFilename', '{exe}'),
         StringStruct('ProductName', '{product}'),
         StringStruct('ProductVersion', '{version}'),
         StringStruct('Comments', '{comments}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def write_version_file(destination) -> str:
    from src.core.config import (APP_NAME, COMPANY_NAME, EXE_NAME, IS_CANARY,
                                 SLUG, VERSION)
    numeric = _numeric_version(VERSION)
    values = {
        'filevers': numeric,
        'prodvers': numeric,
        'company': _quote(COMPANY_NAME),
        'description': _quote(APP_NAME),
        'version': _quote(VERSION),
        'internal': _quote(SLUG),
        'copyright': _quote(f'Copyright (c) {COMPANY_NAME}'),
        'exe': _quote(EXE_NAME),
        'product': _quote(APP_NAME),
        'comments': 'Canary build' if IS_CANARY else 'Stable build',
    }
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TEMPLATE.format(**values), encoding='utf-8')
    return str(path)
