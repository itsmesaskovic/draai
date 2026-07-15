# DRAAI

**Your library. Your network. Your control.**

**Play your own music on Sonos and IKEA Symfonisk speakers — from your Mac,
with no cloud, no account, and no subscription.**

You have a folder of mp3s. You have a Sonos (or IKEA Symfonisk, which is
Sonos inside) speaker. Getting the first to play on the second should be
easy, and somehow every app that promises it is broken, paywalled, or both.
This is a single-file app that fixes that. It runs on your Mac, finds your
speakers on your Wi-Fi, and streams your files to them directly — bit-perfect,
at whatever quality your files are (320 kbps mp3s, lossless FLAC, all of it).

No Sonos account. No internet needed. Nothing leaves your home network.

## Quick start

1. Download `sonos_player.py` and `player_ui.html` from this repository
   (green **Code** button → Download ZIP), and keep them in the same folder.
2. Open **Terminal** (press Cmd+Space, type "Terminal", press Enter).
3. Type `python3 `, **with a space after it**, then drag the downloaded file
   into the Terminal window, and press Enter.

A control panel opens in your browser. Pick a speaker, add your music
folder(s) with the folder picker, click a song. That's it.

Two things may happen on first run, both normal and both one-time:

- If your Mac has never used Python, macOS offers to install its developer
  tools. Click **Install** and wait a few minutes, then run the command again.
- macOS asks whether Python may **accept incoming network connections**.
  Click **Allow** — that's how the speakers fetch the music from your Mac.

Keep the Terminal window open while music plays (your Mac is the music
server). Press Ctrl+C in that window to quit.

## Features

- Finds Sonos / Symfonisk speakers automatically (or add one by IP address)
- Long sets resume where you left off, even after restarting
- Vinyl deck view: a spinning record with your artwork as the label
- Media keys: play/pause from your keyboard, artwork in macOS now-playing
- Library from one or many folders — add them with a built-in folder picker,
  no path-typing needed
- Library with search, real metadata from your files' tags, embedded album art
- Play, pause, skip, seek bar, shuffle, volume
- Queue: add songs, jump around, remove, clear
- Multi-room: group speakers to play in sync, per-room volume
- Real speaker EQ (bass / treble / loudness on the speaker itself)
- Sleep timer
- Guest mode: a QR code lets anyone on your Wi-Fi queue songs from their phone
- Supports mp3, m4a/aac, flac, wav, aiff, ogg — streamed untouched, so
  lossless files play lossless

## Optional extras

Some features use [ffmpeg](https://ffmpeg.org). Install it once with
[Homebrew](https://brew.sh):

```
brew install ffmpeg
```

This enables the waveform / audio-analysis visuals.
Without it, the app works fine — those visuals simply stay off.

### Start at login (optional)

To have DRAAI always running in the background — no Terminal window:

```
python3 sonos_player.py --install-autostart
```

Then just open http://localhost:8765 whenever you want music.
Undo anytime with `--uninstall-autostart`.

### yt-dlp integration (optional, read this)

[yt-dlp](https://github.com/yt-dlp/yt-dlp) is a separate, widely used
open-source media downloader. This project does not include, bundle, or
download it — but if you have installed it yourself
(`brew install yt-dlp ffmpeg`), the app shows an import box: paste a link,
and yt-dlp saves the audio into your music folder.

**Use this only for content you have the right to download**: your own
uploads, Creative Commons-licensed material, public-domain recordings, and
content whose owner permits it. Downloading may otherwise violate a
website's terms of service and/or copyright law in your country. The
authors of this project do not endorse or encourage downloading copyrighted
content, and how you use your own yt-dlp installation is your responsibility
alone.

## The interface

The player is two parts: an engine (`sonos_player.py`) and an interface.
A basic interface is built into the engine, but this repository also ships
**the DRAAI interface** (`player_ui.html`) — a far nicer one, with album-art-driven
colors, a synced visualizer, a full-screen mode, and guest access. Put
`player_ui.html` in the same folder as `sonos_player.py` and it loads
automatically. Delete it (or rename it) to fall back to the basic interface.

Want to build your own? The engine exposes a simple JSON API (`/api/...`) —
see the source — so anyone can design a front end without touching the
engine.

## Contributing

The engine is deliberately a single file — that's the distribution story
(download one file, run it), so please keep changes inside it rather than
splitting it into modules. The test suite needs no speakers, network, or
ffmpeg:

```
python3 tests/test_draai.py
```

## Troubleshooting

- **"No speakers found"** — Your Mac must be on the same Wi-Fi network as the
  speakers. Some routers block device discovery between devices; use *add by
  IP address* (find speaker IPs in the Sonos app under Settings → System →
  About My System).
- **Music stops when I close the laptop** — The speakers stream from your
  Mac, so the app must be running while music plays.
- **The song plays but shows no title/art** — That file has no embedded tags
  or artwork. Tag your files with any tagging tool and rescan.
- **Something else** — Open an issue in this repository.

## Fine print

This is an unofficial, independent project. It is not affiliated with,
endorsed by, or connected to Sonos Inc., Inter IKEA Systems B.V., Google LLC,
or YouTube. All trademarks belong to their respective owners. The software
controls speakers you own, on your own network, using their local network
interface, and is provided **"as is", without warranty of any kind** — see
[LICENSE](LICENSE).

## License

[MIT](LICENSE) — free to use, copy, modify, and share.
