# PiraChest

<p align="center">
  <img width="1887" height="421" alt="PiraChest" src="https://github.com/user-attachments/assets/5c6d937a-26b6-4da7-994a-02b20e7373bb" />
</p>

<p align="center">
  <a href="https://justrun.is-a.dev/pirachest/">
    <img src="https://img.shields.io/badge/🌐%20Website-PiraChest-blue?style=for-the-badge" alt="PiraChest Website">
  </a>
  <a href="https://discord.gg/kkPbmpgMTF">
    <img src="https://img.shields.io/badge/Discord-Join%20the%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Join the PiraChest Discord">
  </a>
</p>

<p align="center">
  <a href="https://github.com/TheJustRun/PiraChest/releases">
    <img src="https://img.shields.io/github/v/release/TheJustRun/PiraChest?include_prereleases&style=flat-square" alt="GitHub Release">
  </a>
  <a href="https://github.com/TheJustRun/PiraChest/releases">
    <img src="https://img.shields.io/github/downloads/TheJustRun/PiraChest/total?style=flat-square" alt="Downloads">
  </a>
  <a href="https://github.com/TheJustRun/PiraChest/stargazers">
    <img src="https://img.shields.io/github/stars/TheJustRun/PiraChest?style=flat-square" alt="Stars">
  </a>
  <a href="https://github.com/TheJustRun/PiraChest/issues">
    <img src="https://img.shields.io/github/issues/TheJustRun/PiraChest?style=flat-square" alt="Issues">
  </a>
  <img src="https://img.shields.io/badge/status-alpha-orange?style=flat-square" alt="Status: Alpha">
  <img src="https://img.shields.io/badge/platform-Windows-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows">
  <img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue?style=flat-square" alt="License">
</p>

<p align="center">
  <strong>An all-in-one desktop application for browsing, watching, listening to, and downloading media from third-party sources.</strong>
</p>

> [!WARNING]
> **PiraChest is alpha software.** Expect bugs, crashes, incomplete features, and unreliable third-party sources.

> [!IMPORTANT]
> PiraChest does not host or distribute copyrighted content. You are responsible for how you use the software and for complying with applicable laws.

---

## Download

<p align="center">
  <a href="https://github.com/TheJustRun/PiraChest/releases/latest">
    <img src="https://img.shields.io/badge/Download-Latest%20Release-2ea44f?style=for-the-badge&logo=github&logoColor=white" alt="Download Latest Release">
  </a>
</p>

PiraChest currently targets Windows. Get the latest version from [GitHub Releases](https://github.com/TheJustRun/PiraChest/releases).

Chocolatey support is planned.

```powershell
choco install pirachest
```

---

## Features

### 🎮 ROMs

Browse a local SQLite-indexed ROM catalog covering **70+ console platforms**.

* Console and source filtering
* No-Intro, Redump, and TOSEC
* Retail releases and BIOS files
* Demos, prototypes, and homebrew
* Encrypted and decrypted variants

### ⚡ Torrenting Engine

Powered by **libtorrent 2.1**, the torrent engine supports selective downloading from large torrent collections without downloading every file.

* Selective file downloading
* Persistent download queue
* Pause, resume, retry, and cancel
* Download and upload speed limits
* Peer limits
* Seeding ratio and time limits
* Force recheck

### 🌐 The Black Hole Bay

**The Black Hole Bay (TBHB)** has its own dedicated page for torrent discovery and downloading.

More aggregators are planned. The long-term goal is a unified **Torrenting** page, with TBHB and future aggregators available as separate tabs.

### 🎮 PC Games

Browse and download PC game repacks.

* Game grid and details
* Upcoming games
* Selective downloading
* Download management

### 🎵 Music

Search, listen to, and download music.

* Quality selection
* Built-in audio playback
* Customizable lyrics panel

### 🍿 Anime

Search, watch, and download anime episodes or entire seasons.

### 📚 Manga & Books

Browse and access manga and books. **Manga support is more developed**, while books remain limited by the availability of reliable sources.

### 🎬 Movies & TV

Search and browse movies, TV shows, series, and TV channels, with metadata available.

> [!NOTE]
> Video playback is not implemented yet.

### 🏟️ Sports

Not implemented yet.

### ▶️ YouTube

Download YouTube videos or audio with metadata and quality selection.

### 📥 Download Manager

Manage downloads across supported sections with:

* Drag-and-drop queue reordering
* Live speed and progress
* Seeding statistics
* Per-download settings
* Pause, resume, retry, and cancel
* Persistent downloads across restarts

### 🎨 Deep Customization & Themes

PiraChest offers extensive customization beyond basic color schemes.

* **Custom themes:** Deep control over the application's appearance
* **GPU shaders:** Customize GPU-powered visual effects
* **Images, GIFs, and videos:** Personalize supported interface backgrounds and visuals
* **Custom title bars:** Customize the window chrome
* **Custom sounds:** Change interface sounds
* **Custom icons:** Customize supported application icons
* **Interface styling:** Customize colors, widgets, and other visual elements

#### Community Themes & Documentation

Share your themes, discover community creations, and exchange ideas in the [PiraChest Discord server](https://discord.gg/kkPbmpgMTF).

Visit the [PiraChest website](https://justrun.is-a.dev/pirachest/) for documentation and guides.

---

## Roadmap

| Feature                                     | Status |
| ------------------------------------------- | :----: |
| ROM catalog and downloading                 |    ✅   |
| Torrent engine                              |    ✅   |
| The Black Hole Bay integration              |    ✅   |
| Unified torrenting page and aggregator tabs |    ⬜   |
| Multi-console support                       |    ✅   |
| Download manager                            |    ✅   |
| PC games and repacks                        |    ✅   |
| Localization                                |    ✅   |
| Music                                       |    ✅   |
| Anime                                       |    ✅   |
| Manga                                       |    ✅   |
| Books                                       |   🟡   |
| Movie and TV search and metadata            |    ✅   |
| Movie and TV playback                       |    ⬜   |
| Sports                                      |    ⬜   |
| Deep theme customization                    |    ✅   |
| GPU shaders                                 |    ✅   |
| Custom title bars, sounds, and icons        |    ✅   |
| Image, GIF, and video customization         |    ✅   |
| More sources                                |   🔨   |
| Linux support                               |    ❌   |

**Legend:** ✅ Implemented · 🟡 Limited or in progress · 🔨 Active development · ⬜ Not implemented · ❌ Not currently supported or planned

---

## Screenshots

<table>
  <tr>
    <td><img width="2500" height="1440" alt="Minverva Page" src="https://github.com/user-attachments/assets/1cec40d1-0c39-4337-a4fa-f42cac525a1d" /></td>
    <td><img width="2500" height="1440" alt="Music Page" src="https://github.com/user-attachments/assets/25df2a04-965e-4d78-b276-fb79cf3c26d7" /></td>
  </tr>
  <tr>
    <td><img width="2500" height="1440" alt="Music Lyrics" src="https://github.com/user-attachments/assets/e66e24b0-03f5-4d7e-b001-7c012a7ba3f4" /></td>
    <td><img width="2500" height="1440" alt="Books Pages" src="https://github.com/user-attachments/assets/8fefb040-2a22-43e2-aa19-ab9ca73cce0a" /></td>
  </tr>
  <tr>
    <td><img width="2500" height="1440" alt="Anime Page" src="https://github.com/user-attachments/assets/ee71c5a8-b086-49a4-accd-cf50737155e6" /></td>
    <td><img width="2500" height="1440" alt="Anime Details" src="https://github.com/user-attachments/assets/db66e462-81d7-4ae8-abae-9216c77efc17" /></td>
  </tr>
  <tr>
    <td><img width="2500" height="1440" alt="Download Page" src="https://github.com/user-attachments/assets/253847af-8d58-4afe-a451-bb0ad41a5dab" /></td>
    <td><img width="2500" height="1440" alt="Settings Page" src="https://github.com/user-attachments/assets/f1bca3e2-1d6d-449a-85e7-e721e5a84ca5" /></td>
  </tr>
  <tr>
    <td><img width="2500" height="1440" alt="Repacks grid view" src="https://github.com/user-attachments/assets/7d79a53b-7515-462c-a587-67a5826191ee" /></td>
    <td><img width="2500" height="1440" alt="Upcoming Games" src="https://github.com/user-attachments/assets/15d198aa-373b-4c34-bef2-d0f0e0b668cf" /></td>
  </tr>
  <tr>
    <td><img width="2500" height="1440" alt="Game Details" src="https://github.com/user-attachments/assets/b9e04b64-53b4-4971-a4a0-896cf411669f" /></td>
    <td><img width="2500" height="1440" alt="Selective Downloading" src="https://github.com/user-attachments/assets/35a7ca92-1c16-43e7-a806-f75ff5652175" /></td>
  </tr>
</table>

---

## Current Limitations

* Some console variant systems are incomplete.
* Reliable book sources are limited.
* Movie and TV playback is unavailable.
* Sports support is not implemented.
* Third-party sources may change or become unavailable.

> [!TIP]
> Found a bug? [Open an issue](https://github.com/TheJustRun/PiraChest/issues) with logs, screenshots, and reproduction steps where possible.

---

## AI-Assisted Development

PiraChest has been developed with assistance from a **local Qwen 3.6 35B model**, primarily for backend development, debugging, investigation, and parts of the torrent infrastructure.

AI assistance does not guarantee correct code; development still involves investigation, testing, and debugging.

---

## Donations

Donations are appreciated if you want to support development and hosting costs.

**USDT TRC-20**

<p align="center">
  <img width="400" height="400" alt="USDT TRC-20 Donation QR Code" src="https://github.com/user-attachments/assets/458ff27b-e03c-4e09-9dab-75895f718a8a" />
</p>

---

## Credits

Special thanks to [spicysaltysparty](https://www.reddit.com/user/spicysaltysparty/) for creating the PiraChest logo.

---

## License

PiraChest is licensed under the **PolyForm Noncommercial License 1.0.0**.

You may use, modify, and redistribute PiraChest for permitted noncommercial purposes under the license terms. **Commercial use requires separate permission from the copyright holder.**

See [`LICENSE`](LICENSE) for the complete terms.

Official license: https://polyformproject.org/licenses/noncommercial/1.0.0

---

## Disclaimer

> [!CAUTION]
> PiraChest is a tool. It does not host, upload, or distribute copyrighted content itself.
>
> It may interact with third-party websites, services, archives, torrents, and other sources. Their content, availability, and legality are outside the project's control.
>
> You are responsible for complying with applicable laws, copyrights, licenses, and terms of service. The developers and contributors do not encourage or endorse copyright infringement, piracy, or unauthorized distribution.
>
> If you misuse the software, **that's your responsibility. Fuck you.**

---

## 📊 Project Stats

### ⭐ Star History

<p align="center">
  <a href="https://star-history.com/#TheJustRun/PiraChest&Date">
    <img src="https://api.star-history.com/svg?repos=TheJustRun/PiraChest&type=Date" alt="PiraChest Star History" width="800">
  </a>
</p>

### 📥 Downloads

<p align="center">
  <img src="https://img.shields.io/github/downloads/TheJustRun/PiraChest/total?style=for-the-badge&label=Total%20Downloads" alt="Total Downloads">
</p>

<p align="center">
  <a href="https://github.com/TheJustRun/PiraChest/releases">
    <img src="https://img.shields.io/github/downloads/TheJustRun/PiraChest/latest/total?style=flat-square&label=Latest%20Release%20Downloads" alt="Latest Release Downloads">
  </a>
</p>

---

<p align="center">
  <a href="https://github.com/TheJustRun/PiraChest">
    <img src="https://img.shields.io/github/stars/TheJustRun/PiraChest?style=for-the-badge&logo=github&label=Star%20PiraChest" alt="Star PiraChest">
  </a>
  <a href="https://github.com/TheJustRun/PiraChest/releases">
    <img src="https://img.shields.io/github/downloads/TheJustRun/PiraChest/total?style=for-the-badge&logo=github&label=Downloads" alt="PiraChest Downloads">
  </a>
</p>

<p align="center">
  Made with questionable amounts of code, debugging, and a local LLM.
</p>
