![PiraChest Banner](https://github.com/user-attachments/assets/9ec37694-bbe5-410c-80bb-191f57f63eb1)

<p align="center">
  <a href="https://justrun.is-a.dev/pirachest/"><img src="https://img.shields.io/badge/🌐%20Website-PiraChest-blue?style=for-the-badge" alt="Website"></a>
  <a href="https://discord.gg/kkPbmpgMTF"><img src="https://img.shields.io/badge/Discord-Join%20the%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/github/v/release/TheJustRun/PiraChest?include_prereleases&label=Latest%20Release" alt="Latest Release">
  <img src="https://img.shields.io/github/downloads/TheJustRun/PiraChest/total?label=Downloads" alt="Downloads">
  <img src="https://img.shields.io/github/stars/TheJustRun/PiraChest?label=Stars" alt="Stars">
  <img src="https://img.shields.io/github/issues/TheJustRun/PiraChest?label=Issues" alt="Issues">
  <img src="https://img.shields.io/badge/Status-Beta-blue" alt="Beta">
  <img src="https://img.shields.io/badge/Platform-Windows-blue" alt="Windows">
  <img src="https://img.shields.io/badge/License-PolyForm%20Noncommercial-lightgrey" alt="License">
</p>

**PiraChest is an all-in-one Windows app for games, ROMs, music, anime, manga, movies, TV, downloads, torrents, and a whole lot of customization.**

The idea is simple: stop jumping between a million different apps and websites when you can have the stuff you actually use in one place.

And yes, you can customize the hell out of it.

> [!WARNING]
> PiraChest is in beta now. That's a big step up from alpha, but it's still not finished. Expect bugs, unfinished features, and the occasional thing that worked yesterday suddenly deciding it doesn't want to work today. Especially Debrid, which is still very much a work in progress.

## Download

Get the latest build from [GitHub Releases](https://github.com/TheJustRun/PiraChest/releases).

[![Download](https://img.shields.io/badge/Download-Latest%20Release-brightgreen?style=for-the-badge)](https://github.com/TheJustRun/PiraChest/releases)

Chocolatey support is out, installing it is as simple as:

```powershell
choco install pirachest
```

## Features

### Home

There's a new Home page that acts as a central starting point for discovering content and getting to the main parts of PiraChest.

### Sailseek

A fast global search system that lets you search across the content and functionality PiraChest offers from one compact interface — movies, shows, games, music, anime, manga, settings, and more. Result cards show relevant metadata like artwork, name, source, and date where available. There's a customizable keyboard shortcut for opening it too.

### ROMs

A local SQLite-indexed catalog covering **70+ console platforms**. Filters include:

* No-Intro, Redump, and TOSEC
* Retail games and BIOS files
* Demos, prototypes, and homebrew
* Encrypted and decrypted ROMs

Because manually sorting thousands of ROMs sounds like a fantastic way to waste an entire weekend.

### PC Game Repacks

Browse games in a grid, check game details, look at upcoming releases, and choose which files to download.

* Grid view and game details
* Upcoming games page
* Selective downloads
* Torrent integration

### Music

Search for music, play tracks, choose audio quality, and customize the lyrics panel.

### Anime

Search, browse details, watch episodes, and download episodes or entire seasons. Anime discovery is in now too, and AniPub is the default streamer.

### Manga and Books

Browse manga and books from available sources. Manga is considerably more developed; books are limited because finding reliable sources is a pain.

### Movies and TV

Browse and watch movies, TV shows, series, and TV channels with metadata and details. Playback works now, and Watch Later is supported.

### YouTube

Download videos or audio with metadata and quality selection.

### The Black Hole Bay (TBHB)

TBHB is currently the only torrent aggregator integrated into PiraChest, with its own dedicated page. More aggregators are planned.

### Download Manager

* Drag-and-drop queue reordering
* Live download speed and progress
* Seeding statistics
* Per-download settings
* Pause, resume, retry, and cancel
* Persistent downloads between sessions

### Torrent Engine

* Selective file downloading
* Persistent queue
* Pause, resume, retry, and cancel
* Download and upload speed limits
* Peer limits
* Seeding ratio and time limits
* Force recheck

### Customization

This is one of the things I care about most. I don't want you stuck with the same interface everyone else has.

You can customize things like:

* Themes and interface styling
* GPU shaders
* Photos and images
* GIFs and videos
* Custom title bars
* Sounds and icons
* Application backgrounds (with blur and darkness controls)
* Other visual details

Themes can now control their own appearance independently instead of being stuck to the global accent color, and they can follow your accent color if you want. They can also bring their own configurable settings, custom icons, images, GIFs, videos, and GPU shaders. Want to share your theme or show off what you've made? There's a [Discord server](https://discord.gg/kkPbmpgMTF) for that. Guides and documentation are on the [website](https://justrun.is-a.dev/pirachest/).

## What's New in Beta

The big focus for beta was making the app faster, more polished, more customizable, easier to use, and much more stable. A lot of that work isn't immediately visible — navigation, caching, threading, rendering, resource management, stability, and internal architecture all got significant improvements.

Some highlights:

* **Sailseek**: global search across movies, shows, games, music, anime, manga, settings, and more, with a customizable keyboard shortcut.
* **Picture-in-Picture**: the video player supports PiP now, and the PiP window can stay above other windows.
* **Movie and TV playback**: movies and shows actually play now, and Watch Later is in.
* **Unified Downloads page**: one dedicated place for download management with better controls and more consistent state handling.
* **Settings backup and restore**: back up and restore your PiraChest configuration.
* **Better updates**: changelogs show before you accept an update, plus a Don't Show Again option and a proper cancel option. Updates preserve your settings and downloads.
* **RSS checking**: PiraChest can check supported RSS sources for updates/content.
* **Context menus**: quicker access to relevant actions across many parts of the app.
* **Keyboard navigation**: broader keyboard navigation and customizable shortcuts throughout the interface.
* **Experimental features**: an Experimental settings category with stuff still being tested, like install-on-download and a theme maker.
* **Debrid**: initial Debrid integration is in, but it's unfinished and needs testing. Expect things to break or change.
* **Localization**: added Russian and Dutch.

## Roadmap

Stuff that's done, stuff that's planned, and stuff that might take longer than I'd like.

* [x] ROM catalog and platform filters
* [x] PC game repacks browsing
* [x] Upcoming games page
* [x] Selective downloads
* [x] Music search and playback
* [x] Anime browsing and downloads
* [x] YouTube downloads
* [x] Download manager
* [x] Torrent engine
* [x] The Black Hole Bay integration
* [x] Advanced customization
* [x] Chocolatey support
* [ ] One unified Torrenting page
* [ ] Multiple torrent aggregators as tabs
* [ ] More fixes, improvements, and features

> [!IMPORTANT]
> A checked box means the feature is implemented, not that it's flawless. And an unchecked box doesn't mean it'll magically appear next Tuesday.

## Screenshots

### Home

![Home](https://github.com/user-attachments/assets/63da344d-f6d1-4155-ba71-37446c2386f8)

### Sailseek

![Sailseek](https://github.com/user-attachments/assets/22b1c541-5fbc-4941-ac78-e52fac67b7f5)

### PC Game Repacks

![Repacks](https://github.com/user-attachments/assets/87745b5d-50af-4b9a-be20-c4f91e2dda29)

![Repacks Selective Download](https://github.com/user-attachments/assets/eb7b18a3-0772-42c5-b649-abc208a83a6f)

![Repacks Details](https://github.com/user-attachments/assets/76336ec8-4e68-4512-9b01-f602e551429c)

![Repack Sources](https://github.com/user-attachments/assets/224a4474-69c0-422b-8c43-60e8d32fc1bc)

![GOG](https://github.com/user-attachments/assets/29680df6-3954-44ce-b4af-d0b793eeedff)

![Upcoming](https://github.com/user-attachments/assets/e3b07b4c-7c93-43b4-8321-5bfb226ba71c)

### Music

![Music](https://github.com/user-attachments/assets/ee4cd666-7cb0-4b56-8c6d-57540bc6b24b)

### Minerva

![Minerva](https://github.com/user-attachments/assets/bf773d12-1a87-48dd-8171-22c6ff44104b)

### Downloads

![Downloads](https://github.com/user-attachments/assets/9e48aff8-474f-4bf0-8586-1f19eb32a91b)

### Books

![Books](https://github.com/user-attachments/assets/3c5b7893-e5b5-4a79-a2a9-274199d9d261)

### Settings

![Settings](https://github.com/user-attachments/assets/b5ba2962-207a-4864-bdc8-21257c4b8cdb)

### YouTube

![YouTube](https://github.com/user-attachments/assets/d5036c06-b8d8-4c61-a9de-8388eb168ac1)

### Movies and TV

![TV](https://github.com/user-attachments/assets/464ca861-de93-43fa-8cf3-1a4a6a78a21b)

### The Black Hole Bay (TBHB)

![TBHB Search](https://github.com/user-attachments/assets/9e9e07d8-50b1-466b-8972-728c8d333ab3)

![TBHB Results](https://github.com/user-attachments/assets/fff44a0a-97e0-4659-a635-dcda5c718809)

## Current Limitations

PiraChest is in beta. Here's what isn't ready yet:

* Debrid support is unfinished and needs testing.
* Book sources are limited.
* TBHB is currently the only integrated torrent aggregator.
* Some parts of the app are still rough or incomplete.

That's the honest version. I'd rather tell you what's missing than pretend everything is finished.

## Privacy

> [!IMPORTANT]
> **PiraChest itself collects no personal data and sends no telemetry.** No built-in analytics tracking what you do, no usage reports sent back to me. Your app activity isn't being secretly shipped off to the developer.

There is a difference between PiraChest itself and the services it connects to, though.

* External websites and services may log your IP address, requests, and other information needed to provide their services.
* Your ISP can see network activity and may be subject to local laws and data-retention requirements.
* Downloading or accessing something through PiraChest doesn't make that activity invisible.

> [!CAUTION]
> **PiraChest is not a VPN, proxy, or anonymity tool.** It does not hide you from your ISP, your government, or copyright enforcement. It does not protect you from copyright law, and it does not make illegal activity legal.
>
> Your connection is still your connection. Don't confuse an app that collects no telemetry with an app that makes you anonymous.

## AI-Assisted Development

This app has been AI-assisted using a local LLM (Qwen 3.6 35B) to help with the backend. Without it, I probably wouldn't have gotten the torrent per-file downloading system working, and I would've shot myself dead.
tho things by it can break stuff or introduce issues, that's why i don't rely on it a lot.

## Support PiraChest

If you find PiraChest useful, consider supporting its development.

<table>
  <tr>
    <td align="center" width="33%">
      <strong>Bitcoin (BTC)</strong><br><br>
      <img src="https://github.com/user-attachments/assets/5d4220bc-2a6d-4eba-ac21-b92b10430380" width="140" alt="Bitcoin donation QR code"><br>
      <code>bc1qkghdq757u9lwntjr3029ljg3phwv9yffj926vv</code>
    </td>
    <td align="center" width="33%">
      <strong>Ethereum (ETH)</strong><br><br>
      <img src="https://github.com/user-attachments/assets/499a6be1-158a-434e-8727-93f3788ab8cc" width="140" alt="Ethereum donation QR code"><br>
      <code>0xD8e4d1408aF248B97C2cdCe85afbeD83e61Cc3f8xv</code>
    </td>
    <td align="center" width="33%">
      <strong>Litecoin (LTC)</strong><br><br>
      <img src="https://github.com/user-attachments/assets/ed9ed961-90dd-4a2d-8893-e49eaa45f5f7" width="140" alt="Litecoin donation QR code"><br>
      <code>Lb3qSpBgc8Bb1xAnBKpPvuCSEwFjMNeuPm</code>
    </td>
  </tr>
  <tr>
    <td align="center" width="33%">
      <strong>Solana (SOL)</strong><br><br>
      <img src="https://github.com/user-attachments/assets/0dec7f9c-4537-410b-8972-7d62be2e9240" width="140" alt="Solana donation QR code"><br>
      <code>Fe1JqgUTPkq45r7aSx9BYtz2nAbH6cPMSWztBJYu7Mc8</code>
    </td>
    <td align="center" width="33%">
      <strong>USD Coin (USDC)</strong><br><br>
      <img src="https://github.com/user-attachments/assets/ab47780b-b9cb-4e34-879b-3a104713d0ee" width="140" alt="USDC donation QR code"><br>
      <code>0xD8e4d1408aF248B97C2cdCe85afbeD83e61Cc3f8</code>
    </td>
    <td align="center" width="33%">
      <strong>Tether (USDT)</strong><br><br>
      <img src="https://github.com/user-attachments/assets/27570992-d974-42dc-82fc-79ea8be60f42" width="140" alt="USDT donation QR code"><br>
      <code>0xD8e4d1408aF248B97C2cdCe85afbeD83e61Cc3f8</code>
    </td>
  </tr>
</table>

**Please verify the cryptocurrency and network before sending.** USDC and USDT may use different networks, even when the receiving address is identical.

Donations are optional. You don't have to pay anything to use PiraChest.

## Credits

* **Logo:** [spicysaltysparty](https://www.reddit.com/user/spicysaltysparty/)
* **Torrent engine:** [libtorrent 2.1](https://www.libtorrent.org/)

## License

PiraChest uses the **[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)**. The `LICENSE` file is included in the repository.

You can use, modify, and share the project under the license terms. **Commercial use requires separate permission.**

> [!IMPORTANT]
> If you want to use PiraChest commercially, read the license and get permission first. Don't just assume it's fine.

## Disclaimer

PiraChest does not host, upload, or distribute copyrighted content itself. It provides tools that interact with third-party websites, sources, and services.

I'm not encouraging piracy or copyright infringement. You're responsible for what you access, download, and do with the software, and for following the laws that apply where you live.

> [!WARNING]
> PiraChest doesn't magically erase the law because you clicked a button in an app. Use your brain, check your local laws, and understand what you're doing.
>
> If you misuse the software, **that's your responsibility. Fuck you.**

## Project Stats

[![Star History](https://api.star-history.com/svg?repos=TheJustRun/PiraChest&type=Date)](https://star-history.com/#TheJustRun/PiraChest&Date)
