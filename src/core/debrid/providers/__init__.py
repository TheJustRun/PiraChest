from .alldebrid import AllDebridProvider
from .premiumize import PremiumizeProvider
from .realdebrid import RealDebridProvider
from .torbox import TorBoxProvider

PROVIDERS = {
    RealDebridProvider.key: RealDebridProvider,
    AllDebridProvider.key: AllDebridProvider,
    PremiumizeProvider.key: PremiumizeProvider,
    TorBoxProvider.key: TorBoxProvider,
}

PROVIDER_ORDER = (RealDebridProvider.key, AllDebridProvider.key, PremiumizeProvider.key, TorBoxProvider.key)

PROVIDER_INFO = {
    provider.key: {
        "key": provider.key,
        "label": provider.label,
        "help_url": provider.help_url,
        "token_label": provider.token_label,
    }
    for provider in (RealDebridProvider, AllDebridProvider, PremiumizeProvider, TorBoxProvider)
}

__all__ = [
    "AllDebridProvider",
    "PremiumizeProvider",
    "PROVIDERS",
    "PROVIDER_INFO",
    "PROVIDER_ORDER",
    "RealDebridProvider",
    "TorBoxProvider",
]
