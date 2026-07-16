# DRAAI technical reference

Per-domain deep dives into how DRAAI works, written for people (and agents)
changing the code. Each doc is grounded in the actual source with `file:line`
anchors, and follows the same shape: **purpose → where it lives → key concepts
→ gotchas → references**.

The project root [`CLAUDE.md`](../../CLAUDE.md) holds the condensed, always-load
gotchas. These docs are the full story behind them.

## The domains

| Doc | Read it when you're touching… |
|---|---|
| [architecture.md](architecture.md) | The package as a whole: module map, import DAG, `python3 -m draai` vs `draai.pyz`, `build.py`/zipapp, `_load_ui()`, storage paths, threading, autostart. |
| [sonos-protocol.md](sonos-protocol.md) | Sonos/Symfonisk control: SSDP discovery, zone-group topology, SOAP/DIDL, queue reorder semantics, grouping, volume, EQ, resume. |
| [google-cast.md](google-cast.md) | The Google Cast / Chromecast backend: mDNS, the hand-rolled CASTV2 protobuf, TLS transport, media sessions, position polling, the codec matrix. |
| [http-and-media.md](http-and-media.md) | The HTTP server: request routing, the `/api/*` surface, `/media` serving with HTTP Range, track identity, guest/QR, UI-vs-`PAGE`. |
| [library-and-metadata.md](library-and-metadata.md) | Scanning folders, the hand-rolled per-format tag/art readers, search/sort/group, and the file-based integrations (yt-dlp import, m3u playlists). |
| [audio-analysis.md](audio-analysis.md) | The optional ffmpeg waveform pipeline: loudness envelope + frequency bands, the versioned JSON cache, background analysis, graceful degradation. |
| [web-ui.md](web-ui.md) | The single-file `player_ui.html`: token theming, the album-palette pipeline, class-collision hazards, fullscreen now-playing, media keys. |
| [security-and-privacy.md](security-and-privacy.md) | The privacy guarantees and how they're enforced: the egress inventory, the stdlib-only / no-phone-home CI guards, the trust boundary, and how to audit it yourself. |

## Ground rules these docs assume

- **Pure Python standard library, no pip — ever.** Optional tools (ffmpeg,
  yt-dlp) are *detected* at runtime, never required.
- **`player_ui.html` stays one file.** No CDNs, no web fonts, no
  local/sessionStorage; preferences persist through the engine.
- **No cloud, no accounts, no telemetry.** Everything stays on the local
  network.
