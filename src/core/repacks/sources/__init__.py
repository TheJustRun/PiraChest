from __future__ import annotations

from ..base import RepackSource
from .fitgirl import FitGirlSource

_REGISTRY: dict[str, type[RepackSource]] = {
    FitGirlSource.key: FitGirlSource,
}


def get_source(key: str) -> RepackSource:
    source_cls = _REGISTRY.get(key)
    if source_cls is None:
        raise KeyError(f"Unknown repack source: {key}")
    return source_cls()


def available_sources() -> list[tuple[str, str]]:
    return [(cls.key, cls.display_name) for cls in _REGISTRY.values()]