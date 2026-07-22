# PiraChest: An all-in-one desktop free media downloader (WIP)
<img width="2484" height="1200" alt="banner" src="https://github.com/user-attachments/assets/20c0fe91-385c-410a-94e0-7bf191610cab" />
 A Work in Progress desktop GUI application to download specific ROMs from the [Minerva Archive](https://minerva-archive.org) using BitTorrent (With more sources and features coming!), with the hope of being a complete app for sailing the seven seas, downloading music, books, courses, and many more.

## Features

**DISCLAIMER**: This app is in **ALPHA**. I'm just releasing it as is right now to get feedback and contributions to help polish and improve the app over time, even the current features that are added may have bugs, issues, freezes, or just incomplete and not finished
**another DISCLAIMER**: this app has been AI-assisted using a local LLM ( Qwen 3.6 35B ) to *help* with the backend, if without it, I wouldn't have gotten the torrent per-file downloading system working, and I would have shot myself dead
- **ROM Browsing** : Browse a local SQLite-indexed ROM catalog covering 70+ console platforms. Filter by console, source (No-Intro, Redump, TOSEC, etc.), and per-console variants.
- **Smart Torrent Engine**: Uses `libtorrent 2.0.13` to download only the requested ROM file from multi-gigabyte torrent dumps, via a persistent queue with pause/resume/retry/cancel controls.
- **Download Manager**: Real-time download queue with drag-and-drop reordering, live speed/progress/seed stats, per-torrent settings (speed limits, peer caps, ratio/time limits, force recheck).
- **Console-First Classification**: Automatic console detection from the Minerva archive naming scheme with per-console variant support (Retail, Encrypted/Decrypted, BIOS, Demo, Prototype, Homebrew, etc.).
- **Dark & Light Theme**: Full Light/Dark/Auto theming via QFluentWidgets, with a shared palette so every widget stays in sync.
- **Persistent Queue**: Download queue survives app restarts. Partially downloaded torrents resume from disk rather than starting from scratch.
- **Per-Torrent Concurrency Controls**: Global and per-item download/upload speed limits, max peer caps, seed ratio and time limits.

## How It Works

1. **Index Sync** — Downloads the `Caprico1/Minerva-archive-ids` markdown index from GitHub, parses console/provider/torrent/file-ID metadata, classifies each entry into the correct console family, deduplicates by provider, and bulk-inserts into a local SQLite database.
2. **Browse & Search** — Queries the SQLite index by name, console, source, and variant with offset-based pagination (30 items per page).
3. **Download** — Fetches the `.torrent` file from the Minerva CDN (`cdn.minerva-archive.org`), adds it to a libtorrent session, selects only the requested file from within the torrent, and writes the result into `downloads/{Console}/{Game Title}/`.

## Roadmap
- [x] ROM downloading
- [x] Torrent support
- [x] Multi-console support
- [x] Download manager
- [ ] DAT Support
- [ ] PC Games (Repacks..)
- [ ] Updates & DLC (You can find some, but it isn't very reliable)
- [ ] Media downloads (Music, Books, Courses..)
- [ ] Localization
- [ ] Linux *(never™)*
- more ig

## Photos

<table>
  <tr>
    <td><img width="1552" height="900" alt="UI" src="https://github.com/user-attachments/assets/4b4aebc8-e76f-45b5-b4ad-f5e6144cc176" />
</td>
    <td><img width="1552" height="900" alt="Console Selection Screen" src="https://github.com/user-attachments/assets/1dd739c1-daf7-4fca-bf7a-ea6ecc11d8bd" />
</td>
  </tr>
  <tr>
    <td><img width="1552" height="900" alt="Download Manager" src="https://github.com/user-attachments/assets/0f32cab9-a0cb-4210-ad37-3b582d9f3cab" />
</td>
    <td><img width="1552" height="900" alt="Settings" src="https://github.com/user-attachments/assets/6f80bc53-44d4-4d79-b125-bd4d35763e3f" />
</td>
  </tr>
</table>

## Supported Sources
- [x] Minerva Archive
- [ ] More... like nopaystation.. etc

## **Current** Issues and Quirks
- Not all Consoles have their variant system working yet
- Download manager is still quirky with a lot of files, currently it is recommended to download about 3 files at once. Needs more testing
- The accent color option is a placeholder
- Persistent settings don't work xd
- And more idk? I need more testing, that's why I'm releasing it in alpha, so I get more feedback on the thing rather than just blindly making it.
- Light mode sucks

## Requirements
- **Python 3.10+**
## Installation

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
```
### 2. Build the ROM index (required once, then periodically)
You can do that in the GUI or with commands
```bash
python src/core/indexer.py --sync
```
This downloads the latest index markdown files, parses them, and populates `src/data/minerva_index.db`.
It is also recommended to resync every release, not only that will insure you have the latest files, 
but also if there is a change in anything list related in the app, you will be updated as well

## Usage
### Launch the GUI
```bash
python src/main.py
```
or just open the exe in releases

### CLI index management

```bash
# Sync the index (clone + parse + upsert)
python src/core/indexer.py --sync

# Search for a ROM from the command line
python src/core/indexer.py --search "Mario"

# Show index statistics
python src/core/indexer.py --stats
```

### Build a standalone executable (optional)

```bash
# Windows
build.bat
```

The output will be in `dist/PiraChest/`.

## Configuration

Settings are stored in `.config/pirachest_settings.json` relative to the project root. You can edit them manually or use the built-in **Settings** dialog.

| Setting | Default | Description |
|---|---|---|
| `download_dir` | `downloads/` | Root folder for all ROM downloads |
| `seed_time` | `0` | Minutes to seed after download completes |
| `speed_limit` | `0` | Download speed cap in KB/s (0 = unlimited) |
| `upload_speed_limit` | `500` | Upload speed cap in KB/s (0 = unlimited) |
| `auto_download` | `false` | Start download immediately when a ROM is selected |
| `delete_torrent_after` | `true` | Remove cached `.torrent` file after download |
| `theme_mode` | `Dark` | `Dark`, `Light`, or `Auto` (follows system theme) |

## Project Structure

```
├── src/
│   ├── main.py                  # Application entry point
│   ├── core/
│   │   ├── config.py            # Centralised configuration (paths, libtorrent defaults, settings)
│   │   ├── console_mapper.py    # Console classification from archive torrent names
│   │   ├── console_variants.py  # Per-console variant metadata (Retail, BIOS, etc.)
│   │   ├── database.py          # SQLite schema, connection, queries, and pagination
│   │   ├── download_manager.py  # libtorrent 2.0.13 queue engine with persistence
│   │   ├── indexer.py           # GitHub markdown index fetch, parse, and DB sync
│   │   ├── sync.py              # Background sync worker (QThread)
│   │   └── theme.py             # Shared colour palette for Light/Dark mode
│   └── gui/
│       ├── main_window.py       # FluentWindow with Home, Download Manager, Settings pages
│       ├── settings_dialog.py   # Settings modal (download dir, seeding, speed limits)
│       ├── splash.py            # Splash screen icon lookup
│       ├── download_manager_panel.py  # Download queue page with live stats
│       ├── rom_details_panel.py     # Right-side ROM metadata card (local DB only)
│       ├── rom_table_model.py       # Paginated QAbstractTableModel for ROM table
│       └── components/
│           ├── rom_details.py       # Rich ROM info card (cover art, metadata)
│           ├── rom_table.py         # Results table with title cleaning & region extraction
│           └── search_bar.py        # Debounced search input
├── build.bat                  # PyInstaller build script (Windows)
├── MinervaROMDownloader.spec  # PyInstaller spec file
├── pyproject.toml
├── requirements.txt
└── README.md
```
Also, special thanks to [spicysaltysparty](https://www.reddit.com/user/spicysaltysparty/) for creating the logo

## Disclaimer

> **For educational and archival purposes only.**
>
> This tool is designed to interact with the Minerva Archive, a community
> repository of ROMs distributed for educational and archival purposes. Users
> are responsible for ensuring that their use of downloaded ROMs complies with
> all applicable laws and regulations in their jurisdiction.
>
> The Minerva Archive and its contributors do not endorse or encourage
> copyright infringement. Please only download ROMs for software you own a
> legal copy of, or for software that is in the public domain.

