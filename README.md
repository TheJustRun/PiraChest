# PiraChest: An All-in-One Desktop Free Media Downloader (WIP)

<img width="1887" height="421" alt="t071snp" src="https://github.com/user-attachments/assets/5c6d937a-26b6-4da7-994a-02b20e7373bb" />

A work-in-progress desktop GUI application for downloading ROMs from the Minerva Archive, PC game repacks, watching anime, listening to music, downloading books, watching TV channels and shows, and downloading YouTube videos.

## Donations
Consider Donating to the app (USDT TRC-20), it will **REALLY** help :) 

<img width="400" height="400" alt="donate_qr" src="https://github.com/user-attachments/assets/458ff27b-e03c-4e09-9dab-75895f718a8a" />


## Features

**DISCLAIMER:** This app is in **ALPHA**. I'm releasing it as-is right now to get feedback and contributions to help polish and improve it over time. Even the features that are already implemented may have bugs, issues, freezes, or may simply be incomplete.

**DISCLAIMER²:** This app has been AI-assisted using a local LLM (Qwen 3.6 35B) to *help* with the backend. Without it, I probably wouldn't have gotten the torrent per-file downloading system working, and I would've shot myself dead.

* **ROM Browsing:** Browse a local SQLite-indexed ROM catalog covering 70+ console platforms. Filter by console, source (No-Intro, Redump, TOSEC, etc.), and per-console variants.
* **Smart Torrent Engine:** Uses `libtorrent 2.0.13` to download only the requested ROM file from multi-gigabyte torrent dumps, via a persistent queue with pause/resume/retry/cancel controls.
* **PC Repacks:** Browse and download PC game repacks (currently FitGirl for now).
* **Music:** Search, listen to, and download music in lossless quality or whatever quality you want. You can also listen to music directly in the app, with a customizable lyrics panel.
* **Anime:** Watch and download anime episodes or entire seasons directly from the app.
* **Books:** Currently a work in progress and proof of concept. You can download books from the app.
* **YouTube Downloading:** Download YouTube videos in audio or video format with metadata.
* **TV**: Watch and download Movies, Series, Sports and TV Channels live from the app 
* **Download Manager:** Real-time download queue with drag-and-drop reordering, live speed/progress/seed stats, and per-torrent settings (speed limits, peer caps, ratio/time limits, force recheck).
* **Console-First Classification:** Automatic console detection from the Minerva Archive naming scheme, with per-console variant support (Retail, Encrypted/Decrypted, BIOS, Demo, Prototype, Homebrew, etc.).
* **Dark & Light Theme:** Full Light/Dark/Auto theming via QFluentWidgets, with a shared palette so every widget stays in sync.
* **Persistent Queue:** The download queue survives app restarts. Partially downloaded torrents resume from disk instead of starting from scratch.
* **Per-Torrent Concurrency Controls:** Global and per-item download/upload speed limits, max peer caps, seed ratio, and time limits.

## Roadmap

* [x] ROM downloading
* [x] Torrent support
* [x] Multi-console support
* [x] Download manager
* [x] PC Games (Repacks)
* [x] Localization
* [x] Media (Music, Books, Anime...)
* [ ] DAT Support
* [ ] Updates & DLC (You can find some, but it isn't very reliable)
* [ ] More stuff, I guess
* [ ] Linux (absolutely never by me, fuck that shit)

## Photos

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

## **Current** Issues and Quirks

* Not all consoles have their variant system working yet.
* Light mode sucks.
* And more, I guess? I need more testing. That's why I'm releasing it in alpha: so I can get more feedback on the app instead of just blindly making it.
* Probably a bunch of other bugs I haven't found yet.

Also, special thanks to [spicysaltysparty](https://www.reddit.com/user/spicysaltysparty/) for creating the logo!

# Disclaimer

> **For legal and educational purposes only.**
>
> This application does not host, distribute, or endorse copyrighted content. It merely provides tools to access third-party sources. You are solely responsible for how you use this software and for complying with all applicable laws.
>
> **If you misuse it, that's your responsibility. Fuck you.**

