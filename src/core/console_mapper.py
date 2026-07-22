from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_CONSOLE_CANONICAL: dict[str, str] = {
    "nes": "Nintendo Entertainment System",
    "nintendo_entertainment_system": "Nintendo Entertainment System",
    "family_computer": "Nintendo Family Computer",
    "famicom": "Nintendo Family Computer",
    "fds": "Nintendo Family Computer Disk System",
    "family_computer_disk_system": "Nintendo Family Computer Disk System",
    "snes": "Super Nintendo Entertainment System",
    "super_nintendo": "Super Nintendo Entertainment System",
    "super_nintendo_entertainment_system": "Super Nintendo Entertainment System",
    "super_famicom": "Nintendo Super Famicom",
    "n64": "Nintendo 64",
    "nintendo_64": "Nintendo 64",
    "n64dd": "Nintendo 64DD",
    "nintendo_64dd": "Nintendo 64DD",
    "gamecube": "Nintendo GameCube",
    "nintendo_gamecube": "Nintendo GameCube",
    "gc": "Nintendo GameCube",
    "wii": "Nintendo Wii",
    "wii_u": "Nintendo Wii U",
    "game_boy": "Nintendo Game Boy",
    "gameboy": "Nintendo Game Boy",
    "gb": "Nintendo Game Boy",
    "game_boy_color": "Nintendo Game Boy Color",
    "gameboy_color": "Nintendo Game Boy Color",
    "gbc": "Nintendo Game Boy Color",
    "game_boy_advance": "Nintendo Game Boy Advance",
    "gameboy_advance": "Nintendo Game Boy Advance",
    "gba": "Nintendo Game Boy Advance",
    "nds": "Nintendo DS",
    "nintendo_ds": "Nintendo DS",
    "ds": "Nintendo DS",
    "dsi": "Nintendo DSi",
    "nintendo_dsi": "Nintendo DSi",
    "3ds": "Nintendo 3DS",
    "nintendo_3ds": "Nintendo 3DS",
    "new_nintendo_3ds": "New Nintendo 3DS",
    "virtual_boy": "Nintendo Virtual Boy",
    "virtualboy": "Nintendo Virtual Boy",
    "vb": "Nintendo Virtual Boy",
    "satellaview": "Satellaview",
    "st": "Satellaview",
    "sufami_turbo": "Sufami Turbo",
    "stg": "Sufami Turbo",
    "pokemon_mini": "Pokemon Mini",
    "pkmn_mini": "Pokemon Mini",
    "game_and_watch": "Nintendo Game & Watch",
    "gb_micro": "Nintendo Game Boy Micro",
    "gbm": "Nintendo Game Boy Micro",
    "famiteb": "Famicom Terebikko",
    "famicom_teb": "Famicom Terebikko",
    "playstation": "Sony PlayStation",
    "sony_playstation": "Sony PlayStation",
    "playstation_1": "Sony PlayStation",
    "ps1": "Sony PlayStation",
    "ps_one": "Sony PlayStation",
    "psone": "Sony PlayStation",
    "playstation_2": "Sony PlayStation 2",
    "ps2": "Sony PlayStation 2",
    "sony_playstation_2": "Sony PlayStation 2",
    "playstation_3": "Sony PlayStation 3",
    "ps3": "Sony PlayStation 3",
    "sony_ps3": "Sony PlayStation 3",
    "psp": "Sony PlayStation Portable",
    "sony_psp": "Sony PlayStation Portable",
    "playstation_portable": "Sony PlayStation Portable",
    "ps_vita": "Sony PlayStation Vita",
    "sony_playstation_vita": "Sony PlayStation Vita",
    "psvita": "Sony PlayStation Vita",
    "xbox": "Microsoft Xbox",
    "microsoft_xbox": "Microsoft Xbox",
    "xbox_360": "Microsoft Xbox 360",
    "xbox360": "Microsoft Xbox 360",
    "microsoft_xbox_360": "Microsoft Xbox 360",
    "xbox_one": "Microsoft Xbox One",
    "xboxseries": "Microsoft Xbox Series X and Series S",
    "xbox_series_x": "Microsoft Xbox Series X and Series S",
    "xbox_series_s": "Microsoft Xbox Series X and Series S",
    "sega_genesis": "Sega Genesis",
    "mega_drive": "Sega Genesis",
    "sega_mega_drive": "Sega Genesis",
    "genesis": "Sega Genesis",
    "md": "Sega Genesis",
    "sega_master_system": "Sega Master System",
    "master_system": "Sega Master System",
    "sms": "Sega Master System",
    "game_gear": "Sega Game Gear",
    "gamegear": "Sega Game Gear",
    "gg": "Sega Game Gear",
    "sega_saturn": "Sega Saturn",
    "saturn": "Sega Saturn",
    "sat": "Sega Saturn",
    "sega_32x": "Sega 32X",
    "32x": "Sega 32X",
    "sega_cd": "Sega Mega-CD",
    "mega_cd": "Sega Mega-CD",
    "sega_mega_cd": "Sega Mega-CD",
    "segacd": "Sega Mega-CD",
    "sega_mega_cd_3": "Sega Mega-CD 32X",
    "mega_cd_3": "Sega Mega-CD 32X",
    "sega_cd_3": "Sega Mega-CD 32X",
    "sgm": "Sega Mega-CD 32X",
    "sega_sgm": "Sega Mega-CD 32X",
    "dreamcast": "Sega Dreamcast",
    "dc": "Sega Dreamcast",
    "sg_1000": "Sega SG-1000",
    "sg1000": "Sega SG-1000",
    "sc_3000": "Sega SG-1000 and SC-3000",
    "sc3000": "Sega SG-1000 and SC-3000",
    "pico": "Sega Pico",
    "beenaa": "Sega Beena",
    "naomi": "Sega NAOMI Arcade System",
    "naomi2": "Sega NAOMI 2 Arcade System",
    "naomi_2": "Sega NAOMI 2 Arcade System",
    "chihiro": "Sega Chihiro Arcade System",
    "lindbergh": "Sega Lindbergh Arcade System",
    "ringedge": "Sega RingEdge Arcade System",
    "triforce": "Sega Triforce Arcade System",
    "atari_2600": "Atari 2600",
    "atarixe": "Atari 2600",
    "atari_xe": "Atari 2600",
    "atari_5200": "Atari 5200",
    "atari_7800": "Atari 7800",
    "atari_jaguar": "Atari Jaguar",
    "jaguar": "Atari Jaguar",
    "atari_jaguar_cd": "Atari Jaguar CD",
    "atari_lynx": "Atari Lynx",
    "lynx": "Atari Lynx",
    "atari_st": "Atari ST",
    "atarist": "Atari ST",
    "atari_8_bit": "Atari 8-bit Family",
    "atari8": "Atari 8-bit Family",
    "atari_8bit": "Atari 8-bit Family",
    "commodore_64": "Commodore 64",
    "c64": "Commodore 64",
    "amiga": "Commodore Amiga",
    "commodore_amiga": "Commodore Amiga",
    "amigacd32": "Commodore Amiga CD32",
    "amiga_cd32": "Commodore Amiga CD32",
    "cdtv": "Commodore Amiga CDTV",
    "commodore_cdtv": "Commodore Amiga CDTV",
    "plus_4": "Commodore Plus/4",
    "vic_20": "Commodore VIC-20",
    "vic20": "Commodore VIC-20",
    "pet": "Commodore Pet",
    "pc_88": "NEC PC-8801",
    "pc_8801": "NEC PC-8801",
    "pc_98": "NEC PC-9801",
    "pc_9801": "NEC PC-9801",
    "pc_fx": "NEC PC-FX",
    "pc_eggs": "NEC PC-E",
    "pc_engine": "NEC PC Engine and TurboGrafx-16",
    "pce": "NEC PC Engine and TurboGrafx-16",
    "turboGrafx_16": "NEC PC Engine and TurboGrafx-16",
    "tgui16": "NEC PC Engine and TurboGrafx-16",
    "turboGrafx_cd": "NEC PC Engine CD and TurboGrafx-CD",
    "tg_cd": "NEC PC Engine CD and TurboGrafx-CD",
    "pccd": "NEC PC Engine CD and TurboGrafx-CD",
    "pcdtv": "NEC PC-FX AV",
    "pc_fx_av": "NEC PC-FX AV",
    "neo_geo": "SNK Neo Geo",
    "neogeo": "SNK Neo Geo",
    "aes": "SNK Neo Geo",
    "neo_geo_cd": "SNK Neo Geo CD",
    "ngcd": "SNK Neo Geo CD",
    "neo_geo_pocket": "SNK Neo Geo Pocket",
    "ngp": "SNK Neo Geo Pocket",
    "neo_geo_pocket_color": "SNK Neo Geo Pocket Color",
    "ngpc": "SNK Neo Geo Pocket Color",
    "wonder_swan": "Bandai WonderSwan",
    "wonderswan": "Bandai WonderSwan",
    "wonder_swan_color": "Bandai WonderSwan Color",
    "wonderswan_color": "Bandai WonderSwan Color",
    "pippin": "Bandai Pippin",
    "x68000": "Sharp X68000",
    "x1": "Sharp X1",
    "xz": "Sharp MZ",
    "3do": "Panasonic 3DO",
    "panasonic_3do": "Panasonic 3DO",
    "cd_i": "Philips CD-i",
    "cdi": "Philips CD-i",
    "watara_supervision": "Watara Supervision",
    "supervision": "Watara Supervision",
    "colovision": "ColecoVision",
    "coleco_vision": "ColecoVision",
    "intellivision": "Intellivision",
    "vectrex": "Vectrex",
    "amstrad_cpc": "Amstrad CPC",
    "tandy": "Tandy / TRS-80",
    "pda": "PDA / Handheld",
    "pocket_pc": "Pocket PC",
    "ngage": "Nokia N-Gage",
    "nokia_n_gage": "Nokia N-Gage",
    "zeebo": "Zeebo",
    "arduboy": "Arduboy",
    "playdate": "Panic Playdate",
    "game_wave": "Game Wave Family Entertainment System",
    "msx": "MSX",
    "msx2": "MSX2",
    "msx_turbo_r": "MSX Turbo-R",
    "mame": "MAME",
    "arcade": "Arcade",
    "dos": "DOS (PC)",
    "pc": "PC",
    "windows": "Windows (PC)",
    "ibm_pc": "IBM PC Compatible",
    "ibm_pc_compatible": "IBM PC Compatible",
    "dos_pc": "DOS (PC)",
    "amiibo": "amiibo",
}

def _normalize_token(token: str) -> str:
    token = token.lower().strip()
    token = re.sub(r"[-\s]+", "_", token)
    token = re.sub(r"[^a-z0-9_]", "", token)
    return token

def _normalize_token_v2(token: str) -> str:
    token = token.lower().strip()
    token = re.sub(r"[-\s]+", "_", token)
    token = re.sub(r"[^a-z0-9_]", "", token)
    return token

_KNOWN_PROVIDERS_NORM: dict[str, str] = {
    "no_intro": "No-Intro",
    "redump": "Redump",
    "tosec": "TOSEC",
    "tosec_iso": "TOSEC-ISO",
    "tosec_pix": "TOSEC-PIX",
    "mame": "MAME",
    "hb_mame": "HBMAME",
    "finalburn_neo": "FinalBurn Neo",
    "internet_archive": "Internet Archive",
    "bitsavers": "bitsavers",
    "retroachievements": "RetroAchievements",
    "t_en_collection": "T-En Collection",
    "total_dos_collection": "Total DOS Collection",
    "touhou_project_collection": "Touhou Project Collection",
    "eggmans_arcade_repository": "Eggman's Arcade Repository",
    "teknoparrot": "TeknoParrot",
    "lost_level": "Lost Level",
    "outliers": "Outliers",
    "miscellaneous": "Miscellaneous",
    "exog": "eXo",
    "ex_g": "eXo",
    "laserdisc_collection": "Laserdisc Collection",
    "hardware_target_game_database": "Hardware Target Game Database",
}

def _extract_provider(raw: str) -> str:
    token = _normalize_token_v2(raw)
    if token in _KNOWN_PROVIDERS_NORM:
        return _KNOWN_PROVIDERS_NORM[token]
    for provider_key in _KNOWN_PROVIDERS_NORM:
        if provider_key in token or token in provider_key:
            return _KNOWN_PROVIDERS_NORM[provider_key]
    return "Unknown"

def _normalize_provider_name(raw: str) -> str:
    raw = raw.lower().strip()
    return _KNOWN_PROVIDERS_NORM.get(raw, raw.title().replace("_", "-"))

_VARIANT_TOKENS: frozenset[str] = frozenset({
    "decrypted", "encrypted", "digital", "cdn", "headered", "headerless",
    "bigendian", "byteswapped", "padded",
    "bios", "demo", "demos", "prototypes", "prototype", "beta", "betas",
    "kiosk", "homebrew", "aftermarket", "unlicensed", "hack", "hacks",
    "translations", "translation", "magazine", "sampler", "dlc", "update",
    "updates", "patch", "patches", "bootleg", "bootlegs", "demoscene",
    "music", "c64_music", "demodisc", "demodiscs", "wads", "wad",
    "minis", "umd_video", "umd_music", "nonpdrm", "npdp", "npdp_carts",
    "bs_manuals", "st_games", "sd_cards", "dsvision", "photopi",
    "smartmedia", "disk", "gd_rom", "mil_cd", "e_reader", "video",
    "multiboot", "play_yan",
    "roms", "chds", "samples", "devices",
    "cue", "ccd", "img", "iso", "chd", "mds", "toc", "ssf", "sub",
    "cbz", "zip", "rar", "nfo", "md5", "torrent",
    "rom", "bin", "cof", "bll", "lnx", "lyx", "pp", "gz", "7z",
    "sd", "cards", "no", "mario", "sbi", "subchannels", "net", "jet",
})

def _is_variant_token(token: str) -> bool:
    norm = _normalize_token_v2(token)
    if norm in _VARIANT_TOKENS:
        return True
    if len(token) == 1 and token.isalpha():
        return True
    if re.fullmatch(r"\d+[-_]\d+", norm):
        return True
    return False

_NON_GAME_PREFIXES: frozenset[str] = frozenset({
    "Source Code", "source code",
    "Wallpapers", "wallpapers",
    "Manuals", "manual",
    "Magazine Scans", "magazine scans",
    "OSTs", "ost", "soundtrack",
    "Documents", "document",
    "Flux", "fluxdump", "kryoflux",
    "Bitstream", "bitstream",
    "Waveform", "wav",
    "Disc Keys", "disc keys",
    "Tapes", "tapes",
    "Flash Media", "flash media",
    "Development Kit", "development kit",
    "Unofficial", "unofficial",
    "Non Redump", "non redump",
    "Promo", "promo",
    "SDK", "sdk",
    "Music Tracks", "music tracks",
    "Kiosk Video", "kiosk video",
    "Memory Card", "memory card",
    "NPDP Carts", "npdp carts",
    "Visual Memory Unit", "visual memory unit",
    "Starlight Fun Center", "starlight",
    "Lotcheck", "lotcheck",
    "Dev Dev",
    "Digital Cdn Dev",
    "Deprecated",
    "Hentai",
    "Gameshark",
    "Updates And Dlc",
    "Pocket Challenge",
    "Studybox",
    "Satellite Terminal Pc",
    "Hasbro Ion",
    "Electronic Book",
    "Polymega",
    "Megatouch",
    "Python 2",
    "Purikura",
    "NKit",
    "GDI Files",
    "BIOS Images",
    "DoM Version",
    "WUX",
})

_NON_GAME_SUBSTRINGS: frozenset[str] = frozenset({
    "fluxdump", "kryoflux", "kryo", "flux ", " flux/",
    " bitstream", " bitstream/",
    " waveform", " wav/",
    " sector", " sector/",
    " hdm",
    " uncategorized",
    " appstore", " app store",
    " google play", " samsung galaxy",
    " itch.io", " itchio",
    " various",
    " speed hacks", " msu1", " enhanced colors", " t-en",
    " roms_only",
    " collection",
    " bios images", " nkit", " wux",
    " raid", " gdi files", " dom version",
    " studio box", " pocket challenge",
    " net jet", " purikura", " megatouch",
    " python 2", " hasbro ion", " polymega",
    " electronic book", " zaurus", " satellite terminal",
    " disc keys", " wux", " nkit", " gdi files", " bios images",
    " lotcheck", " starlight",
})

def _looks_like_non_game(raw_parts: list[str]) -> bool:
    combined = " ".join(raw_parts).title()
    combined_lower = combined.lower()
    for prefix in _NON_GAME_PREFIXES:
        if prefix in combined:
            return True
    for substr in _NON_GAME_SUBSTRINGS:
        if substr in combined_lower:
            return True
    return False

@dataclass(frozen=True)
class Classification:
    provider: str
    console: str
    console_key: str
    original_path: str
    variant_parts: tuple = field(default_factory=tuple)

def classify_torrent_name(torrent_name: str) -> Classification:
    name = torrent_name
    if name.endswith(".torrent"):
        name = name[:-len(".torrent")]

    parts = [p.strip() for p in name.split("_-_")]
    if parts and "minerva" in parts[0].lower() and "myrient" in parts[0].lower():
        parts = parts[1:]

    if not parts:
        return Classification(
            provider="Unknown",
            console="Unknown",
            console_key="unknown",
            original_path="",
        )

    provider = _extract_provider(parts[0])
    console_parts = parts[1:]

    _STRIPPABLE_PREFIXES = ["Non Redump", "Unofficial", "Source Code"]
    while console_parts and console_parts[0] in _STRIPPABLE_PREFIXES:
        console_parts = console_parts[1:]

    if not console_parts:
        console = provider if provider != "Unknown" else "Unknown"
        return Classification(
            provider=provider,
            console=console,
            console_key=_normalize_token_v2(console),
            original_path="",
            variant_parts=(),
        )

    if _looks_like_non_game(console_parts):
        combined = " ".join(console_parts).title()
        return Classification(
            provider=provider,
            console=combined,
            console_key=_normalize_token_v2(combined),
            original_path="",
            variant_parts=tuple(console_parts),
        )

    _expanded: list[tuple[int, str]] = []
    for _orig_idx, _orig_part in enumerate(console_parts):
        _sub = _normalize_token_v2(_orig_part)
        for _subtoken in _sub.split("_"):
            if _subtoken:
                _expanded.append((_orig_idx, _subtoken))

    def _match_from_expanded(
        min_start: int = 0, max_end: int | None = None
    ) -> tuple[str | None, set[int]]:
        if max_end is None:
            max_end = len(_expanded)

        candidates: list[tuple[int, int, str, set[int]]] = []

        for length in range(min(max_end - min_start, 3), 1, -1):
            for start in range(min_start, max_end - length + 1):
                candidate = "_".join(t[1] for t in _expanded[start:start + length])
                if candidate in _CONSOLE_CANONICAL:
                    indices: set[int] = {t[0] for t in _expanded[start:start + length]}
                    candidates.append((length, start, candidate, indices))

        for start in range(min_start, max_end):
            tok = _expanded[start][1]
            if tok in _CONSOLE_CANONICAL:
                candidates.append((1, start, tok, {_expanded[start][0]}))

        if not candidates:
            return None, set()

        best_length, best_start, best_key, best_indices = max(
            candidates, key=lambda c: (c[0], c[1])
        )

        return best_key, best_indices

    key, indices = _match_from_expanded()
    if key:
        console_display = _CONSOLE_CANONICAL[key]
        min_idx = min(indices) if indices else 0
        max_idx = max(indices) if indices else 0

        leftover_parts: list[str] = []
        for i, p in enumerate(console_parts):
            if i in indices:
                continue
            if i < min_idx:
                continue
            console_words = set(console_display.lower().split())
            if all(w in console_words for w in p.lower().split() if w.isalpha()):
                continue
            leftover_parts.append(p)

        return Classification(
            provider=provider,
            console=console_display,
            console_key=key,
            original_path="",
            variant_parts=tuple(leftover_parts),
        )

    for strip_count in range(1, len(_expanded)):
        key, indices = _match_from_expanded(max_end=len(_expanded) - strip_count)
        if key:
            console_display = _CONSOLE_CANONICAL[key]
            min_idx = min(indices) if indices else 0
            leftover_parts: list[str] = []
            for i, p in enumerate(console_parts):
                if i in indices:
                    continue
                if i < min_idx:
                    continue
                console_words = set(console_display.lower().split())
                if all(w in console_words for w in p.lower().split() if w.isalpha()):
                    continue
                leftover_parts.append(p)
            return Classification(
                provider=provider,
                console=console_display,
                console_key=key,
                original_path="",
                variant_parts=tuple(leftover_parts),
            )

    combined = "_".join(_normalize_token_v2(p) for p in console_parts)
    return Classification(
        provider=provider,
        console=combined.replace("_", " ").title(),
        console_key=combined,
        original_path="",
        variant_parts=tuple(console_parts[1:]) if len(console_parts) > 1 else (),
    )

_DB_CONSOLE_MAPPERS: list[tuple[str, str]] = [
    ("Nintendo DS (NDS)", "Nintendo DS (NDS)"),
    ("Nintendo DSi", "Nintendo DSi"),
    ("Nintendo 3DS", "Nintendo 3DS"),
    ("New Nintendo 3DS", "New Nintendo 3DS"),
    ("Nintendo 64", "Nintendo 64"),
    ("Nintendo 64DD", "Nintendo 64DD"),
    ("Nintendo GameCube (GC)", "Nintendo GameCube (GC)"),
    ("Nintendo Wii U", "Nintendo Wii U"),
    ("Nintendo Game & Watch", "Nintendo Game & Watch"),
    ("Virtual Boy (VB)", "Virtual Boy (VB)"),
    ("Satellaview", "Satellaview"),
    ("Sufami Turbo", "Sufami Turbo"),
    ("Pokemon Mini", "Pokemon Mini"),
    ("Game Boy (GB)", "Game Boy (GB)"),
    ("Game Boy Color (GBC)", "Game Boy Color (GBC)"),
    ("Game Boy Advance (GBA)", "Game Boy Advance (GBA)"),
    ("Family Computer (FC)", "Family Computer (FC)"),
    ("Family Computer Disk System (FDS)", "Family Computer Disk System (FDS)"),
    ("Famicom Terebikko", "Famicom Terebikko"),
    ("Nintendo Entertainment System (NES)", "Nintendo Entertainment System (NES)"),
    ("Super Nintendo Entertainment System (SNES)", "Super Nintendo Entertainment System (SNES)"),
    ("Super Famicom (SFC)", "Super Famicom (SFC)"),
    ("Sony PlayStation", "Sony PlayStation"),
    ("PlayStation 2 (PS2)", "PlayStation 2 (PS2)"),
    ("PlayStation 3 (PS3)", "PlayStation 3 (PS3)"),
    ("PlayStation Portable (PSP)", "PlayStation Portable (PSP)"),
    ("Sony PlayStation Vita", "Sony PlayStation Vita"),
    ("PS One", "Sony PlayStation"),
    ("PSone", "Sony PlayStation"),
    ("Microsoft Xbox", "Microsoft Xbox"),
    ("Microsoft Xbox 360", "Microsoft Xbox 360"),
    ("Microsoft Xbox One", "Microsoft Xbox One"),
    ("Microsoft Xbox Series X and Series S", "Microsoft Xbox Series X and Series S"),
    ("Sega Genesis", "Sega Genesis"),
    ("Sega Master System", "Sega Master System"),
    ("Sega Game Gear", "Sega Game Gear"),
    ("Sega Saturn", "Sega Saturn"),
    ("Sega 32X", "Sega 32X"),
    ("Sega Mega-CD", "Sega Mega-CD"),
    ("Sega Mega-CD 32X", "Sega Mega-CD 32X"),
    ("Sega Dreamcast", "Sega Dreamcast"),
    ("Sega NAOMI Arcade System", "Sega NAOMI Arcade System"),
    ("Sega NAOMI 2 Arcade System", "Sega NAOMI 2 Arcade System"),
    ("Sega Lindbergh Arcade System", "Sega Lindbergh Arcade System"),
    ("Sega RingEdge Arcade System", "Sega RingEdge Arcade System"),
    ("Sega Triforce Arcade System", "Sega Triforce Arcade System"),
    ("Sega SG-1000", "Sega SG-1000"),
    ("Sega Pico", "Sega Pico"),
    ("Sega Beena", "Sega Beena"),
    ("Atari 2600", "Atari 2600"),
    ("Atari 5200", "Atari 5200"),
    ("Atari 7800", "Atari 7800"),
    ("Atari Jaguar", "Atari Jaguar"),
    ("Atari Jaguar CD", "Atari Jaguar CD"),
    ("Atari Lynx", "Atari Lynx"),
    ("Atari ST", "Atari ST"),
    ("Atari 8-bit Family", "Atari 8-bit Family"),
    ("Commodore Amiga", "Commodore Amiga"),
    ("Commodore Amiga CD32", "Commodore Amiga CD32"),
    ("Commodore Amiga CDTV", "Commodore Amiga CDTV"),
    ("Commodore 64", "Commodore 64"),
    ("Commodore VIC-20", "Commodore VIC-20"),
    ("Commodore Plus/4", "Commodore Plus/4"),
    ("Commodore Pet", "Commodore Pet"),
    ("IBM PC Compatible", "IBM PC Compatible"),
    ("MSX", "MSX"),
    ("MSX2", "MSX2"),
    ("Arcade", "Arcade"),
    ("MAME", "MAME"),
    ("Panasonic 3DO", "Panasonic 3DO"),
    ("Philips CD-i", "Philips CD-i"),
    ("SNK Neo Geo", "SNK Neo Geo"),
    ("SNK Neo Geo CD", "SNK Neo Geo CD"),
    ("SNK Neo Geo Pocket", "SNK Neo Geo Pocket"),
    ("SNK Neo Geo Pocket Color", "SNK Neo Geo Pocket Color"),
    ("Bandai WonderSwan", "Bandai WonderSwan"),
    ("Bandai WonderSwan Color", "Bandai WonderSwan Color"),
    ("Bandai Pippin", "Bandai Pippin"),
    ("Panic Playdate", "Panic Playdate"),
    ("Sharp X68000", "Sharp X68000"),
    ("Watara Supervision", "Watara Supervision"),
    ("NEC PC Engine and TurboGrafx-16", "NEC PC Engine and TurboGrafx-16"),
    ("NEC PC Engine CD and TurboGrafx-CD", "NEC PC Engine CD and TurboGrafx-CD"),
    ("NEC PC-FX AV", "NEC PC-FX AV"),
    ("NEC PC-8801", "NEC PC-8801"),
    ("NEC PC-9801", "NEC PC-9801"),
    ("Nintendo Wii", "Nintendo Wii"),
    ("Sinclair", "Sinclair"),
    ("Ibm", "IBM PC Compatible"),
    ("Nec", "NEC PC Engine and TurboGrafx-16"),
    ("Sony", "Sony PlayStation"),
    ("Apple", "Apple II"),
    ("Ique Ique Cdn", "Ique"),
    ("Ique Ique Decrypted", "Ique"),
    ("Ique", "Ique"),
    ("Multi Format", "Multi Format"),
    ("Obscure Gamers", "Obscure Gamers"),
    ("Project Egg", "Project Egg"),
    ("Acorn", "Acorn"),
    ("Texas Instruments", "Texas Instruments"),
    ("Bally", "Bally"),
    ("Casio", "Casio"),
    ("Fujitsu", "Fujitsu"),
    ("Microsoft", "Microsoft Xbox"),
    ("Tandy / TRS-80", "Tandy / TRS-80"),
    ("Tangerine", "Tangerine"),
    ("Enterprise", "Enterprise"),
    ("Thomson", "Thomson"),
    ("Audio Cd", "Audio CD"),
    ("Cd Rom", "CD-ROM"),
    ("Photo Cd", "Photo CD"),
    ("Dvd Video", "DVD Video"),
    ("Bd Video", "Blu-ray Video"),
    ("Dvd Rom", "DVD-ROM"),
    ("Video Game Osts Hardware Recordings", "Video Game OSTs"),
    ("Video Game Scans Raw", "Video Game Scans"),
    ("Video Game Magazine Scans Raw", "Video Game Magazine Scans"),
    ("Video Game Magazine Scans Cbz", "Video Game Magazine Scans"),
    ("Video Game Osts Digital Raw", "Video Game OSTs"),
    ("Super Mario Maker Courses Warc", "Super Mario Maker Courses"),
    ("Ouya Ouya", "Ouya"),
    ("Robotron", "Robotron"),
    ("Mgt", "MGT"),
    ("Microkey", "Microkey"),
    ("Cce", "CCE"),
    ("Nintendo Misc", "Nintendo Misc"),
    ("Nintendo Sdks", "Nintendo SDKs"),
    ("Nintendo Wallpapers", "Nintendo Wallpapers"),
    ("Nintendo Kiosk Video Compact Flash Cardimage", "Nintendo Kiosk Video"),
    ("Nintendo Kiosk Video Compact Flash Extracted", "Nintendo Kiosk Video"),
    ("Nintendo Wii Nkit Rvz", "Nintendo Wii"),
    ("Nintendo Wii Nkit_Rvz", "Nintendo Wii"),
    ("Nintendo Wii U Wux", "Nintendo Wii U"),
]

def _normalize_db_console(raw: str) -> str:
    if not raw:
        return "Unknown"

    for pattern, canonical in _DB_CONSOLE_MAPPERS:
        if raw == pattern:
            return canonical

    if raw.startswith("Nintendo Wii_(Digital)"):
        return "Nintendo Wii"
    if raw.startswith("Nintendo Wii Nkit"):
        return "Nintendo Wii"
    if raw.startswith("Nintendo Wii_U_Wux"):
        return "Nintendo Wii U"
    if raw == "Nintendo Wii_U Wux":
        return "Nintendo Wii U"
    if raw.startswith("Nintendo Wii_U_(Digital)"):
        return "Nintendo Wii U"
    if raw.startswith("Nintendo Wii_(Split_Dlc)"):
        return "Nintendo Wii"
    if raw.startswith("Nintendo Wii"):
        return "Nintendo Wii"

    if raw.startswith("Nintendo Nintendo "):
        rest = raw[len("Nintendo "):]
        lower_rest = rest.lower()
        if "music tracks" in lower_rest:
            return "Nintendo Wii"
        if "sdks" in lower_rest:
            return "Nintendo Wii"
        if "misc" in lower_rest or "wallpapers" in lower_rest:
            return "Nintendo Wii"
        if "kiosk video" in lower_rest:
            return "Nintendo Wii"
        if "courses" in lower_rest or "warc" in lower_rest:
            return "Nintendo Wii"
        return "Nintendo Wii"

    if raw == "Atari":
        return "Atari"

    if raw == "Sega":
        return "Sega"

    if raw.startswith("Non Redump Sega"):
        return "Sega"

    if "Namco Sega Nintendo" in raw:
        return "Multi"

    if "Sega Prologue" in raw:
        return "Sega"

    if raw == "Nec":
        return "NEC PC Engine and TurboGrafx-16"

    if raw == "Sharp":
        return "Sharp X68000"

    if raw == "Sony":
        return "Sony PlayStation"

    if raw == "Commodore":
        return "Commodore Amiga"

    if raw.startswith("Apple ") and raw != "Apple Macintosh" and "Ii" not in raw and "Iigs" not in raw:
        return "Apple II"

    if raw == "Ibm":
        return "IBM PC Compatible"

    if raw == "Watara":
        return "Watara Supervision"

    if raw.startswith("NEC PC-"):
        return "NEC PC-9801"

    if "Sega Mega-CD" in raw and "3" not in raw:
        return "Sega Mega-CD"

    if "Sega Naomi" in raw:
        return "Sega NAOMI Arcade System"

    if "Sega SG-1000 and SC-3000" in raw:
        return "Sega SG-1000 and SC-3000"

    if "Casio Loopy" in raw:
        return "Casio Loopy"

    if "Commodore Amiga_(" in raw:
        return "Commodore Amiga"

    if "Sharp X68000_(" in raw or "Sharp X1_(" in raw:
        return "Sharp X68000"

    if raw.startswith("IBM PC Compatible ") and raw != "IBM PC Compatible":
        return "IBM PC Compatible"

    if raw == "Microsoft":
        return "Microsoft Xbox"

    if raw == "Sinclair":
        return "Sinclair"
    if raw == "Snk":
        return "SNK Neo Geo"
    if raw == "Bandai":
        return "Bandai WonderSwan"
    if raw == "Texas Instruments":
        return "TI-99/4A"

    return raw

def get_all_known_consoles() -> list[str]:
    return sorted(set(_CONSOLE_CANONICAL.values()))

def get_known_console_keys() -> dict[str, str]:
    return {v: k for k, v in _CONSOLE_CANONICAL.items()}