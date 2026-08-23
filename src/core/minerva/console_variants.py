from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

@dataclass
class ConsoleVariantConfig:
    console: str
    variants: list[str] = field(default_factory=list)
    default_variant: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "console": self.console,
            "variants": self.variants,
            "default_variant": self.default_variant,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConsoleVariantConfig":
        return cls(
            console=data["console"],
            variants=list(data.get("variants", [])),
            default_variant=data.get("default_variant"),
        )

_ALPHA_NUMERIC_BUCKETS = [
    "0-9", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M",
    "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
]

CONSOLE_VARIANT_CONFIG: Dict[str, ConsoleVariantConfig] = {
    "Nintendo DS": ConsoleVariantConfig(
        console="Nintendo DS",
        variants=[
            "Retail",
            "Encrypted",
            "Decrypted",
            "Download Play",
            "DSiWare",
            "DSVision",
            "Demos",
            "Kiosk",
            "BIOS",
            "Homebrew",
            "Aftermarket",
        ],
        default_variant="Decrypted",
    ),
    "Nintendo DSi": ConsoleVariantConfig(
        console="Nintendo DSi",
        variants=[
            "Retail",
            "Encrypted",
            "Decrypted",
            "Digital CDN",
            "BIOS",
        ],
        default_variant="Decrypted",
    ),
    "Nintendo 3DS": ConsoleVariantConfig(
        console="Nintendo 3DS",
        variants=[
            "Retail",
            "Digital CDN",
            "Encrypted",
            "Decrypted",
            "DLC",
            "Updates",
        ],
        default_variant="Decrypted",
    ),
    "New Nintendo 3DS": ConsoleVariantConfig(
        console="New Nintendo 3DS",
        variants=["Retail", "Digital CDN", "Decrypted"],
        default_variant="Retail",
    ),
    "Nintendo Entertainment System": ConsoleVariantConfig(
        console="Nintendo Entertainment System",
        variants=[
            "Retail",
            "Headered",
            "Headerless",
            "Aftermarket",
            "Homebrew",
            "Unlicensed",
            "Hack",
            "BIOS",
        ],
        default_variant="Retail",
    ),
    "Super Nintendo Entertainment System": ConsoleVariantConfig(
        console="Super Nintendo Entertainment System",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Unlicensed",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "Nintendo Super Famicom": ConsoleVariantConfig(
        console="Nintendo Super Famicom",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Unlicensed",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "Nintendo Family Computer": ConsoleVariantConfig(
        console="Nintendo Family Computer",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Unlicensed",
            "Hack",
            "BIOS",
        ],
        default_variant="Retail",
    ),
    "Nintendo Family Computer Disk System": ConsoleVariantConfig(
        console="Nintendo Family Computer Disk System",
        variants=[
            "Retail",
            "Aftermarket",
            "Hack",
            "Homebrew",
        ],
        default_variant="Retail",
    ),
    "Nintendo 64": ConsoleVariantConfig(
        console="Nintendo 64",
        variants=[
            "Retail",
            "Bigendian",
            "Byteswapped",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
            "Prototype",
        ],
        default_variant="Bigendian",
    ),
    "Nintendo 64DD": ConsoleVariantConfig(
        console="Nintendo 64DD",
        variants=["Retail", "Aftermarket", "Prototype"],
        default_variant="Retail",
    ),
    "Nintendo Game Boy": ConsoleVariantConfig(
        console="Nintendo Game Boy",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Unlicensed",
            "Hack",
            "BIOS",
        ],
        default_variant="Retail",
    ),
    "Nintendo Game Boy Color": ConsoleVariantConfig(
        console="Nintendo Game Boy Color",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Unlicensed",
            "Hack",
            "BIOS",
        ],
        default_variant="Retail",
    ),
    "Nintendo Game Boy Advance": ConsoleVariantConfig(
        console="Nintendo Game Boy Advance",
        variants=[
            "Retail",
            "e-Reader",
            "Video",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Nintendo Game Boy Micro": ConsoleVariantConfig(
        console="Nintendo Game Boy Micro",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack"],
        default_variant="Retail",
    ),
    "Nintendo Virtual Boy": ConsoleVariantConfig(
        console="Nintendo Virtual Boy",
        variants=["Retail", "Aftermarket", "Homebrew", "Prototype"],
        default_variant="Retail",
    ),
    "Nintendo GameCube": ConsoleVariantConfig(
        console="Nintendo GameCube",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo Disc",
        ],
        default_variant="Retail",
    ),
    "Nintendo Wii": ConsoleVariantConfig(
        console="Nintendo Wii",
        variants=[
            "Retail",
            "Digital CDN",
            "WAD",
            "DLC",
            "Homebrew",
            "Aftermarket",
            "Demo Disc",
        ],
        default_variant="Retail",
    ),
    "Nintendo Wii U": ConsoleVariantConfig(
        console="Nintendo Wii U",
        variants=[
            "Retail",
            "Digital CDN",
            "DLC",
            "Homebrew",
            "Aftermarket",
        ],
        default_variant="Retail",
    ),
    "Pokemon Mini": ConsoleVariantConfig(
        console="Pokemon Mini",
        variants=["Retail", "Aftermarket", "Homebrew", "Demo"],
        default_variant="Retail",
    ),
    "Satellaview": ConsoleVariantConfig(
        console="Satellaview",
        variants=["Retail", "Aftermarket", "St-Games", "BS-Manuals"],
        default_variant="Retail",
    ),
    "Sufami Turbo": ConsoleVariantConfig(
        console="Sufami Turbo",
        variants=["Retail", "Aftermarket"],
        default_variant="Retail",
    ),
    "Nintendo Game & Watch": ConsoleVariantConfig(
        console="Nintendo Game & Watch",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Ique": ConsoleVariantConfig(
        console="Ique",
        variants=["Retail", "Digital CDN", "Decrypted"],
        default_variant="Decrypted",
    ),
    "Famicom Terebikko": ConsoleVariantConfig(
        console="Famicom Terebikko",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Sony PlayStation": ConsoleVariantConfig(
        console="Sony PlayStation",
        variants=[
            "Retail",
            "BIOS",
            "Demo Discs",
            "Magazine Discs",
            "Prototype",
            "Beta",
            "Hack",
        ],
        default_variant="Retail",
    ),
    "Sony PlayStation 2": ConsoleVariantConfig(
        console="Sony PlayStation 2",
        variants=[
            "Retail DVD",
            "CD",
            "BIOS",
            "Demo",
            "Prototype",
            "Hack",
        ],
        default_variant="Retail DVD",
    ),
    "Sony PlayStation 3": ConsoleVariantConfig(
        console="Sony PlayStation 3",
        variants=[
            "Retail",
            "Digital CDN",
            "DLC",
            "Themes",
            "Avatars",
            "Updates",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Sony PlayStation Portable": ConsoleVariantConfig(
        console="Sony PlayStation Portable",
        variants=[
            "Retail",
            "Digital CDN",
            "Encrypted",
            "Decrypted",
            "Minis",
            "DLC",
            "Updates",
            "UMD Video",
            "UMD Music",
            "Prototype",
        ],
        default_variant="Decrypted",
    ),
    "Sony PlayStation Vita": ConsoleVariantConfig(
        console="Sony PlayStation Vita",
        variants=[
            "Retail",
            "Digital CDN",
            "DLC",
            "Updates",
            "Decrypted",
            "Non-PDRM",
            "Prototype",
        ],
        default_variant="Decrypted",
    ),
    "Microsoft Xbox": ConsoleVariantConfig(
        console="Microsoft Xbox",
        variants=[
            "Retail",
            "BIOS",
            "Dashboard",
            "DLC",
            "Updates",
            "Development Kit",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Microsoft Xbox 360": ConsoleVariantConfig(
        console="Microsoft Xbox 360",
        variants=[
            "Retail",
            "Digital CDN",
            "DLC",
            "Updates",
            "Dashboard",
            "Development Kit",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Microsoft Xbox One": ConsoleVariantConfig(
        console="Microsoft Xbox One",
        variants=["Retail", "Digital CDN", "DLC", "Updates"],
        default_variant="Retail",
    ),
    "Microsoft Xbox Series X and Series S": ConsoleVariantConfig(
        console="Microsoft Xbox Series X and Series S",
        variants=["Retail", "Digital CDN", "DLC"],
        default_variant="Retail",
    ),
    "Sega Genesis": ConsoleVariantConfig(
        console="Sega Genesis",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Unlicensed",
            "BIOS",
            "Demo",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Sega Master System": ConsoleVariantConfig(
        console="Sega Master System",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Unlicensed",
            "BIOS",
            "Demo",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Sega Game Gear": ConsoleVariantConfig(
        console="Sega Game Gear",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Sega Saturn": ConsoleVariantConfig(
        console="Sega Saturn",
        variants=[
            "Retail",
            "CD",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo Disc",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Sega 32X": ConsoleVariantConfig(
        console="Sega 32X",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Sega Mega-CD": ConsoleVariantConfig(
        console="Sega Mega-CD",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Sega Mega-CD 32X": ConsoleVariantConfig(
        console="Sega Mega-CD 32X",
        variants=[
            "Retail",
            "Aftermarket",
            "Hack",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Sega Dreamcast": ConsoleVariantConfig(
        console="Sega Dreamcast",
        variants=[
            "GD-ROM",
            "MIL-CD",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
            "Prototype",
        ],
        default_variant="GD-ROM",
    ),
    "Sega NAOMI Arcade System": ConsoleVariantConfig(
        console="Sega NAOMI Arcade System",
        variants=["Retail", "Bootleg", "Hack", "Development Kit"],
        default_variant="Retail",
    ),
    "Sega NAOMI 2 Arcade System": ConsoleVariantConfig(
        console="Sega NAOMI 2 Arcade System",
        variants=["Retail", "Bootleg", "Hack", "Development Kit"],
        default_variant="Retail",
    ),
    "Sega Lindbergh Arcade System": ConsoleVariantConfig(
        console="Sega Lindbergh Arcade System",
        variants=["Retail", "Hack", "Development Kit"],
        default_variant="Retail",
    ),
    "Sega RingEdge Arcade System": ConsoleVariantConfig(
        console="Sega RingEdge Arcade System",
        variants=["Retail", "Hack", "Development Kit"],
        default_variant="Retail",
    ),
    "Sega Triforce Arcade System": ConsoleVariantConfig(
        console="Sega Triforce Arcade System",
        variants=["Retail", "Hack"],
        default_variant="Retail",
    ),
    "Sega SG-1000": ConsoleVariantConfig(
        console="Sega SG-1000",
        variants=["Retail", "Aftermarket", "Hack", "Homebrew", "BIOS"],
        default_variant="Retail",
    ),
    "Sega SG-1000 and SC-3000": ConsoleVariantConfig(
        console="Sega SG-1000 and SC-3000",
        variants=["Retail", "Aftermarket", "Hack", "Homebrew", "BIOS"],
        default_variant="Retail",
    ),
    "Sega Pico": ConsoleVariantConfig(
        console="Sega Pico",
        variants=["Retail", "Aftermarket", "Hack"],
        default_variant="Retail",
    ),
    "Sega Beena": ConsoleVariantConfig(
        console="Sega Beena",
        variants=["Retail", "Aftermarket"],
        default_variant="Retail",
    ),
    "Sega Chihiro Arcade System": ConsoleVariantConfig(
        console="Sega Chihiro Arcade System",
        variants=["Retail", "Hack", "Development Kit"],
        default_variant="Retail",
    ),
    "Sega": ConsoleVariantConfig(
        console="Sega",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
            "Prototype",
            "Bootleg",
            "BIOS",
        ],
        default_variant="Retail",
    ),
    "Atari 2600": ConsoleVariantConfig(
        console="Atari 2600",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Unlicensed",
            "Hack",
            "Demo",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Atari 5200": ConsoleVariantConfig(
        console="Atari 5200",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack", "Demo"],
        default_variant="Retail",
    ),
    "Atari 7800": ConsoleVariantConfig(
        console="Atari 7800",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Atari Jaguar": ConsoleVariantConfig(
        console="Atari Jaguar",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Atari Jaguar CD": ConsoleVariantConfig(
        console="Atari Jaguar CD",
        variants=["Retail", "Aftermarket", "Homebrew"],
        default_variant="Retail",
    ),
    "Atari Lynx": ConsoleVariantConfig(
        console="Atari Lynx",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
            "Prototype",
        ],
        default_variant="Retail",
    ),
    "Atari 8-bit Family": ConsoleVariantConfig(
        console="Atari 8-bit Family",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "Atari ST": ConsoleVariantConfig(
        console="Atari ST",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "Atari": ConsoleVariantConfig(
        console="Atari",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
            "Prototype",
            "Unlicensed",
        ],
        default_variant="Retail",
    ),
    "Commodore Amiga": ConsoleVariantConfig(
        console="Commodore Amiga",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
            "Demoscene",
            "CD32",
            "CDTV",
            "CD",
        ],
        default_variant="Retail",
    ),
    "Commodore Amiga CD32": ConsoleVariantConfig(
        console="Commodore Amiga CD32",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack"],
        default_variant="Retail",
    ),
    "Commodore Amiga CDTV": ConsoleVariantConfig(
        console="Commodore Amiga CDTV",
        variants=["Retail", "Aftermarket"],
        default_variant="Retail",
    ),
    "Commodore 64": ConsoleVariantConfig(
        console="Commodore 64",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
            "Demoscene",
            "C64 Music",
            "Padded",
            "Headerless",
        ],
        default_variant="Retail",
    ),
    "Commodore VIC-20": ConsoleVariantConfig(
        console="Commodore VIC-20",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack"],
        default_variant="Retail",
    ),
    "Commodore Plus/4": ConsoleVariantConfig(
        console="Commodore Plus/4",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack"],
        default_variant="Retail",
    ),
    "Commodore Pet": ConsoleVariantConfig(
        console="Commodore Pet",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack"],
        default_variant="Retail",
    ),
    "Amstrad CPC": ConsoleVariantConfig(
        console="Amstrad CPC",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
            "Demoscene",
        ],
        default_variant="Retail",
    ),
    "NEC PC Engine and TurboGrafx-16": ConsoleVariantConfig(
        console="NEC PC Engine and TurboGrafx-16",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack", "BIOS"],
        default_variant="Retail",
    ),
    "NEC PC Engine CD and TurboGrafx-CD": ConsoleVariantConfig(
        console="NEC PC Engine CD and TurboGrafx-CD",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack"],
        default_variant="Retail",
    ),
    "NEC PC-FX": ConsoleVariantConfig(
        console="NEC PC-FX",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack", "BIOS"],
        default_variant="Retail",
    ),
    "NEC PC-FX AV": ConsoleVariantConfig(
        console="NEC PC-FX AV",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "NEC PC-8801": ConsoleVariantConfig(
        console="NEC PC-8801",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "NEC PC-9801": ConsoleVariantConfig(
        console="NEC PC-9801",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "NEC PC-E": ConsoleVariantConfig(
        console="NEC PC-E",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack", "BIOS"],
        default_variant="Retail",
    ),
    "SNK Neo Geo": ConsoleVariantConfig(
        console="SNK Neo Geo",
        variants=["Retail", "Bootleg", "Hack", "Homebrew"],
        default_variant="Retail",
    ),
    "SNK Neo Geo CD": ConsoleVariantConfig(
        console="SNK Neo Geo CD",
        variants=["Retail", "Bootleg", "Hack"],
        default_variant="Retail",
    ),
    "SNK Neo Geo Pocket": ConsoleVariantConfig(
        console="SNK Neo Geo Pocket",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack", "BIOS"],
        default_variant="Retail",
    ),
    "SNK Neo Geo Pocket Color": ConsoleVariantConfig(
        console="SNK Neo Geo Pocket Color",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack", "BIOS"],
        default_variant="Retail",
    ),
    "Bandai WonderSwan": ConsoleVariantConfig(
        console="Bandai WonderSwan",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack", "BIOS"],
        default_variant="Retail",
    ),
    "Bandai WonderSwan Color": ConsoleVariantConfig(
        console="Bandai WonderSwan Color",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack", "BIOS"],
        default_variant="Retail",
    ),
    "Bandai Pippin": ConsoleVariantConfig(
        console="Bandai Pippin",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "IBM PC Compatible": ConsoleVariantConfig(
        console="IBM PC Compatible",
        variants=_ALPHA_NUMERIC_BUCKETS + ["Tiger Electronics Net Jet"],
        default_variant="A",
    ),
    "DOS (PC)": ConsoleVariantConfig(
        console="DOS (PC)",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Shareware",
            "Freeware",
            "Demo",
            "Demo Discs",
        ],
        default_variant="Retail",
    ),
    "PC": ConsoleVariantConfig(
        console="PC",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack"],
        default_variant="Retail",
    ),
    "Windows (PC)": ConsoleVariantConfig(
        console="Windows (PC)",
        variants=[
            "Retail",
            "Shareware",
            "Freeware",
            "Demo",
            "Aftermarket",
            "Homebrew",
        ],
        default_variant="Retail",
    ),
    "MSX": ConsoleVariantConfig(
        console="MSX",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "MSX2": ConsoleVariantConfig(
        console="MSX2",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "MSX Turbo-R": ConsoleVariantConfig(
        console="MSX Turbo-R",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack", "BIOS"],
        default_variant="Retail",
    ),
    "MAME": ConsoleVariantConfig(
        console="MAME",
        variants=[
            "ROM Set",
            "BIOS",
            "CHD",
            "Samples",
            "Device ROMs",
            "Hack",
        ],
        default_variant="ROM Set",
    ),
    "Arcade": ConsoleVariantConfig(
        console="Arcade",
        variants=[
            "ROM Set",
            "BIOS",
            "Bootleg",
            "Hack",
            "Prototype",
        ],
        default_variant="ROM Set",
    ),
    "Panasonic 3DO": ConsoleVariantConfig(
        console="Panasonic 3DO",
        variants=["Retail", "Aftermarket", "Hack", "Demo"],
        default_variant="Retail",
    ),
    "Philips CD-i": ConsoleVariantConfig(
        console="Philips CD-i",
        variants=["Retail", "Aftermarket", "Hack", "Bootleg"],
        default_variant="Retail",
    ),
    "Sharp X68000": ConsoleVariantConfig(
        console="Sharp X68000",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack", "BIOS"],
        default_variant="Retail",
    ),
    "Sharp X1": ConsoleVariantConfig(
        console="Sharp X1",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "Sharp MZ": ConsoleVariantConfig(
        console="Sharp MZ",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "Watara Supervision": ConsoleVariantConfig(
        console="Watara Supervision",
        variants=["Retail", "Aftermarket", "Homebrew", "Hack"],
        default_variant="Retail",
    ),
    "Panic Playdate": ConsoleVariantConfig(
        console="Panic Playdate",
        variants=[
            "Retail",
            "Encrypted",
            "Decrypted",
            "Itch.io",
            "Homebrew",
        ],
        default_variant="Retail",
    ),
    "Nokia N-Gage": ConsoleVariantConfig(
        console="Nokia N-Gage",
        variants=["Retail", "Aftermarket", "Homebrew"],
        default_variant="Retail",
    ),
    "Zeebo": ConsoleVariantConfig(
        console="Zeebo",
        variants=["Retail", "DLC", "Updates"],
        default_variant="Retail",
    ),
    "Arduboy": ConsoleVariantConfig(
        console="Arduboy",
        variants=["Retail", "Homebrew", "Hack"],
        default_variant="Homebrew",
    ),
    "ColecoVision": ConsoleVariantConfig(
        console="ColecoVision",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Unlicensed",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "Intellivision": ConsoleVariantConfig(
        console="Intellivision",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "Vectrex": ConsoleVariantConfig(
        console="Vectrex",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "TI-99/4A": ConsoleVariantConfig(
        console="TI-99/4A",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "Tandy / TRS-80": ConsoleVariantConfig(
        console="Tandy / TRS-80",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
            "Demo",
        ],
        default_variant="Retail",
    ),
    "Sinclair": ConsoleVariantConfig(
        console="Sinclair",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
            "Demoscene",
            "T-Zero",
        ],
        default_variant="Retail",
    ),
    "Apple II": ConsoleVariantConfig(
        console="Apple II",
        variants=[
            "Retail",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "Demo",
            "Demoscene",
            "BIOS",
        ],
        default_variant="Retail",
    ),
    "Casio Loopy": ConsoleVariantConfig(
        console="Casio Loopy",
        variants=[
            "Retail",
            "Bigendian",
            "Littleendian",
            "Aftermarket",
            "Homebrew",
            "Hack",
            "BIOS",
        ],
        default_variant="Retail",
    ),
    "Game Wave Family Entertainment System": ConsoleVariantConfig(
        console="Game Wave Family Entertainment System",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "PDA / Handheld": ConsoleVariantConfig(
        console="PDA / Handheld",
        variants=["Retail", "Aftermarket", "Homebrew"],
        default_variant="Retail",
    ),
    "Pocket PC": ConsoleVariantConfig(
        console="Pocket PC",
        variants=["Retail", "Aftermarket"],
        default_variant="Retail",
    ),
    "amiibo": ConsoleVariantConfig(
        console="amiibo",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Multi Format": ConsoleVariantConfig(
        console="Multi Format",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Obscure Gamers": ConsoleVariantConfig(
        console="Obscure Gamers",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Project Egg": ConsoleVariantConfig(
        console="Project Egg",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Acorn": ConsoleVariantConfig(
        console="Acorn",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "CCE": ConsoleVariantConfig(
        console="CCE",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "MGT": ConsoleVariantConfig(
        console="MGT",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Ouya": ConsoleVariantConfig(
        console="Ouya",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Robotron": ConsoleVariantConfig(
        console="Robotron",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Nintendo Misc": ConsoleVariantConfig(
        console="Nintendo Misc",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Nintendo SDKs": ConsoleVariantConfig(
        console="Nintendo SDKs",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Nintendo Wallpapers": ConsoleVariantConfig(
        console="Nintendo Wallpapers",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Nintendo Kiosk Video": ConsoleVariantConfig(
        console="Nintendo Kiosk Video",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Super Mario Maker Courses": ConsoleVariantConfig(
        console="Super Mario Maker Courses",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Audio CD": ConsoleVariantConfig(
        console="Audio CD",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "CD-ROM": ConsoleVariantConfig(
        console="CD-ROM",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Photo CD": ConsoleVariantConfig(
        console="Photo CD",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "DVD Video": ConsoleVariantConfig(
        console="DVD Video",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Blu-ray Video": ConsoleVariantConfig(
        console="Blu-ray Video",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "DVD-ROM": ConsoleVariantConfig(
        console="DVD-ROM",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Video Game OSTs": ConsoleVariantConfig(
        console="Video Game OSTs",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Video Game Scans": ConsoleVariantConfig(
        console="Video Game Scans",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Video Game Magazine Scans": ConsoleVariantConfig(
        console="Video Game Magazine Scans",
        variants=["Retail"],
        default_variant="Retail",
    ),
    "Multi": ConsoleVariantConfig(
        console="Multi",
        variants=["Retail"],
        default_variant="Retail",
    ),
}

_GENERIC_VARIANT_KEYWORDS: Dict[str, str] = {
    "bios": "BIOS",
    "demo": "Demo",
    "demos": "Demos",
    "prototypes": "Demo",
    "proto": "Prototype",
    "prototype": "Prototype",
    "beta": "Beta",
    "betas": "Beta",
    "kiosk": "Kiosk",
    "homebrew": "Homebrew",
    "aftermarket": "Aftermarket",
    "unlicensed": "Unlicensed",
    "hack": "Hack",
    "hacks": "Hacks",
    "translation": "Translations",
    "translations": "Translations",
    "magazine": "Magazine Discs",
    "sampler": "Magazine Discs",
    "dlc": "DLC",
    "update": "Updates",
    "updates": "Updates",
    "patch": "Updates",
    "patches": "Updates",
    "chd": "CHD",
    "chds": "CHD",
    "samples": "Samples",
    "devices": "Device ROMs",
    "bootleg": "Bootleg",
    "bootlegs": "Bootleg",
    "demoscene": "Demoscene",
    "c64_music": "C64 Music",
    "music": "C64 Music",
    "padded": "Padded",
    "headered": "Headered",
    "headerless": "Headerless",
    "demodisc": "Demo Disc",
    "demo_disc": "Demo Disc",
    "demodiscs": "Demo Disc",
    "wii_wad": "WAD",
    "wad": "WAD",
    "cdn": "Digital CDN",
    "digital": "Digital CDN",
    "encrypted": "Encrypted",
    "decrypted": "Decrypted",
    "minis": "Minis",
    "umd_video": "UMD Video",
    "umd_music": "UMD Music",
    "nonpdrm": "Non-PDRM",
    "npdp": "NPDPCarts",
    "npdp_carts": "NPDPCarts",
    "bs_manuals": "BS-Manuals",
    "st_games": "St-Games",
    "sd_cards": "SD Cards",
    "dsvision": "DSVision",
    "photopi": "Photopi",
    "smartmedia": "SmartMedia",
    "disk": "Disk",
    "gd_rom": "GD-ROM",
    "mil_cd": "MIL-CD",
    "bigendian": "Bigendian",
    "byteswapped": "Byteswapped",
    "download_play": "Download Play",
    "e_reader": "e-Reader",
    "video": "Video",
    "multiboot": "Multiboot",
    "play_yan": "Play Yan",
}

_DESCRIPTOR_ONLY_TOKENS: frozenset = frozenset({
    "sd", "cards", "no", "mario", "sbi", "subchannels", "net", "jet",
    "rom", "bin", "cof", "bll", "lnx", "lyx", "pp", "gz", "zip", "7z",
    "torrent", "md5", "nfo", "cue", "ccd", "img", "iso", "chd", "mds",
    "toc", "ccd", "ssf", "sub", "cbz", "zip", "rar",
})

def is_known_descriptor_token(normalized_token: str) -> bool:
    if not normalized_token:
        return False
    if normalized_token in _GENERIC_VARIANT_KEYWORDS:
        return True
    if normalized_token in _DESCRIPTOR_ONLY_TOKENS:
        return True
    return False

DEFAULT_VARIANT_NAME = "Retail"

def _normalize_token(token: str) -> str:
    token = token.lower().strip()
    token = re.sub(r"[-\s]+", "_", token)
    token = re.sub(r"[^a-z0-9_]", "", token)
    return token

def register_console_variants(config: ConsoleVariantConfig) -> None:
    CONSOLE_VARIANT_CONFIG[config.console] = config

def load_variant_config_from_json(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load variant config from %s: %s", path, exc)
        return

    for entry in data:
        try:
            cfg = ConsoleVariantConfig.from_dict(entry)
            register_console_variants(cfg)
        except (KeyError, TypeError) as exc:
            logger.warning("Skipping malformed variant config entry: %s", exc)

def has_variants(console: str) -> bool:
    return len(get_variants(console)) > 1

def get_variants(console: str) -> list[str]:
    cfg = CONSOLE_VARIANT_CONFIG.get(console)
    if cfg and cfg.variants:
        return list(cfg.variants)
    return [DEFAULT_VARIANT_NAME]

def get_default_variant(console: str) -> str:
    cfg = CONSOLE_VARIANT_CONFIG.get(console)
    if cfg and cfg.default_variant:
        return cfg.default_variant
    variants = get_variants(console)
    return variants[0] if variants else DEFAULT_VARIANT_NAME

def guess_variant(console: str, raw_descriptor_parts: list[str]) -> str:
    known = get_variants(console)
    known_norm = {_normalize_token(v): v for v in known}

    for part in raw_descriptor_parts:
        norm = _normalize_token(part)
        if not norm:
            continue

        if norm in known_norm:
            return known_norm[norm]

        if norm in _GENERIC_VARIANT_KEYWORDS:
            mapped = _GENERIC_VARIANT_KEYWORDS[norm]
            if mapped in known_norm.values():
                return mapped
            return mapped

        stripped = part.strip().upper()
        if stripped in _ALPHA_NUMERIC_BUCKETS:
            return stripped

    return get_default_variant(console)

def get_all_known_consoles_with_variants() -> list[str]:
    return sorted(
        c for c, cfg in CONSOLE_VARIANT_CONFIG.items() if len(cfg.variants) > 1
    )