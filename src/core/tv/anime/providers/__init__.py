from typing import Protocol


class EpisodeProvider(Protocol):
    async def get_episodes(self, anilist_id, ctx: dict) -> list[dict]:
        ...


PROVIDER_REGISTRY: dict[str, EpisodeProvider] = {}


def register_provider(name: str, provider: EpisodeProvider) -> None:
    PROVIDER_REGISTRY[name] = provider


def _register_all() -> None:
    from . import anibd, animedunya, animegg, anipub, anizone
    from . import kickassanime, anikoto
    register_provider("anibd", anibd)
    register_provider("animedunya", animedunya)
    register_provider("animegg", animegg)
    register_provider("anipub", anipub)
    register_provider("anizone", anizone)
    register_provider("kaa", kickassanime)
    register_provider("anikoto", anikoto)


_register_all()
