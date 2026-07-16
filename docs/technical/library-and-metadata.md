# Library and metadata

> How DRAAI turns a folder of audio files into a searchable, sortable
> library — with hand-rolled tag readers instead of a tagging library — plus
> the file-based integrations that add tracks (yt-dlp import) and reuse them
> (m3u playlists).

## Purpose

DRAAI has zero pip dependencies (`CLAUDE.md` rule 1). That means there is no
`mutagen`/`eyed3`/`tinytag` for reading ID3/MP4/FLAC tags, and no database
for the library or playlists. `draai/library.py` implements just enough of
each tag format's binary layout to pull title/artist/album and embedded
cover art, and the library itself is an in-memory list rebuilt by a full
filesystem walk (`scan_all`). Playlists and YouTube import piggyback on the
same track records and the plain filesystem — `.m3u` files and an
`Imported/` folder — so nothing here needs its own storage format.

## Where it lives

- `draai/library.py` — tag readers (`_tags_mp3`, `_tags_mp4`, `_tags_flac`),
  `read_tags` dispatcher, `get_art`, `scan_all` / `scan_folder` / `_scan_root`.
- `draai/playlists.py` — `.m3u` read/write/list/delete.
- `draai/youtube.py` — `start_youtube_job` (shells out to the user's yt-dlp).
- `draai/media.py` — `media_url` (track id → HTTP URL), `find_tool`
  (PATH + Homebrew-prefix lookup for `ffmpeg`/`yt-dlp`).
- `draai/constants.py` — `AUDIO_EXTS` (supported extensions + MIME types).
- `draai/state.py` — the `tracks` list / `tracks_by_id` dict / `config`
  dict that everything above reads and mutates under `state_lock`.
- `draai/server.py` — `/api/tracks`, `/api/folder*`, `/api/art`,
  `/media/<id>/<name>` (byte-range file serving), `/api/playlist*`,
  `/api/yt_*`.
- `player_ui.html` — client-side sort (`sortTracks`) and group-by
  (`renderSongRows`); the server does not sort or group, only search.

## Key concepts

### Track identity

A track id is the first 16 hex chars of `sha1(path)`, computed in
`_scan_root` (`draai/library.py:315`). It is stable across rescans as long
as the absolute path doesn't change, and it is *not* stable if you move a
file — a moved file is a new track. The same id-from-path scheme is used
independently for Sonos `TrackURI` parsing in `draai/backends.py` (see
`CLAUDE.md`'s "Track identity" note) — `media_url()` in
`draai/media.py:20-23` embeds the id in the URL path
(`/media/<id>/<title><ext>`), and the engine looks the id back up from that
URL shape, so don't change the `/media/<id>/<name>` layout without updating
both sides.

### scan_all and folder management

- `config["folders"]` is a list of expanded, non-empty paths (default:
  `~/Music`, set in `draai/state.py:16`). `scan_all()`
  (`draai/library.py:274`) expands `~`, walks each root that exists,
  collects missing ones into an error string, and replaces the whole
  in-memory library atomically under `state_lock`
  (`draai/library.py:287-290`).
- `scan_folder(folder)` (`draai/library.py:295`) is a back-compat shim: it
  overwrites `config["folders"]` with a single folder, then calls
  `scan_all()`. `/api/folder` (POST) uses this; `/api/folders_add` /
  `/api/folders_remove` (`draai/server.py:524-543`) mutate the list directly
  and then call `scan_all()`.
- `_scan_root(folder, multi)` (`draai/library.py:304`) does `os.walk`,
  skipping dot-directories (`dirs[:] = [d for d in dirs if not
  d.startswith(".")]`, `draai/library.py:309`) and dot-files, filtering by
  `AUDIO_EXTS`. Files are read in sorted order per directory
  (`sorted(files)`, `draai/library.py:310`).
- **Multi-folder prefixing**: when more than one library folder is
  configured (`multi = len(roots) > 1`, `draai/library.py:279`), each
  track's `folder` field is prefixed with that root's basename so tracks
  from different roots don't collide in the folder-grouped view
  (`draai/library.py:318-319`, exercised by
  `tests/test_draai.py:195-209` — `test_scan_multiple_folders`). With a
  single folder, `folder` is just the path relative to that folder (empty
  string at the root).
- `added` is `st_birthtime` if the OS provides it (macOS does), else
  `st_mtime` (`draai/library.py:321-325`) — this is what "sort by date
  added" sorts on.
- The final list is sorted once, server-side, by `(folder.lower(),
  title.lower())` (`draai/library.py:286`) before being stored — this is
  the library's canonical/default order, independent of whatever sort the
  UI later applies client-side.

### Metadata: hand-rolled tag readers (no external library)

`read_tags(path, want_art=False)` (`draai/library.py:241`) dispatches purely
on file extension — it does not sniff content for non-dispatched formats:

| ext | parser | format details |
|---|---|---|
| `.mp3` | `_tags_mp3` (`draai/library.py:45`) | ID3v2 header (`ID3` + syncsafe size, `draai/library.py:10-11`), skips an extended header if the flag bit is set, then walks frames. ID3v2.2 uses 3-char ids/`TT2`/`TP1`/`TAL`/`PIC`; v2.3+/v2.4 use 4-char ids/`TIT2`/`TPE1`/`TALB`/`APIC`, with v2.4 frame sizes also syncsafe (`draai/library.py:62-79`). Text frames go through `_id3_text` (`draai/library.py:14`), which honors the ID3 encoding byte (0=latin-1, 1=UTF-16 w/ BOM, 2=UTF-16BE, 3=UTF-8). Art frames (`PIC`/`APIC`) are located by skipping the encoding byte + MIME/format + picture-type + description (`_null_split`, `draai/library.py:32`, single-null for latin/UTF-8, double-null for UTF-16), then sniffed as JPEG (`\xff\xd8\xff`) or PNG (`\x89PNG`) magic bytes. |
| `.m4a` / `.aac` / `.mp4` | `_tags_mp4` (`draai/library.py:124`) | Streams the file's top-level box headers looking for `moov` (reads it into memory, capped at 64 MiB, `draai/library.py:145`) *without* reading the whole file first — important because `moov` can be near the end of a large file. Then descends `udta > meta > ilst > <tag> > data` via the generic box walker `_mp4_children` (`draai/library.py:107`), mapping `©nam`/`©ART`/`©alb` to title/artist/album and `covr` to art (again sniffed by magic bytes; only JPEG/PNG are recognized). |
| `.flac` | `_tags_flac` (`draai/library.py:182`) | Verifies the `fLaC` magic, then walks METADATA_BLOCKs by header byte (top bit = last-block flag, low 7 bits = block type, `draai/library.py:191-193`). Type 4 is VORBIS_COMMENT: skips the vendor string, then reads `KEY=value` pairs (case-insensitively upper-cased) for `TITLE`/`ARTIST`/`ALBUM`, capped at 256 comments. Type 6 is PICTURE: parses MIME length, description length + skip, then width/height/depth/colors (16 bytes) before the image data length + bytes (`draai/library.py:216-231`). |
| `.wav`, `.aiff`/`.aif`, `.ogg` | **none** | These extensions are in `AUDIO_EXTS` (`draai/constants.py:5-14`) so they are scanned, played, and streamed, but `read_tags` has no branch for them (`draai/library.py:241-252`) — it silently returns `{}`. Title falls back to the filename stem; artist/album are empty; there is no embedded-art extraction for these formats. |

Every parser is wrapped in `try/except Exception: pass` inside `read_tags`
(`draai/library.py:250-251`), and the per-format functions catch locally
around risky offset math too (e.g. `draai/library.py:214`,
`draai/library.py:230`) — a malformed or truncated tag degrades to "no
metadata for this field", never a crash during scan.

`get_art(track)` (`draai/library.py:257`) is a separate call path from
`_scan_root`: the scan only records `has_art` (a bool) to keep the scan
itself cheap; actual art bytes are re-read from disk on demand (`GET
/api/art?id=`) and cached in `state.art_cache`, a dict capped at
`ART_CACHE_MAX = 64` entries (`draai/constants.py:22`) with naive
oldest-first eviction (`art_cache.pop(next(iter(art_cache)))`,
`draai/library.py:264-265` — insertion-ordered dict, not LRU).

### Supported formats and "streamed untouched"

`AUDIO_EXTS` (`draai/constants.py:5-14`) is both the scan filter and the
`Content-Type` map for serving: `.mp3`→audio/mpeg, `.m4a`→audio/mp4,
`.aac`→audio/aac, `.flac`→audio/flac, `.wav`→audio/wav,
`.aiff`/`.aif`→audio/aiff, `.ogg`→audio/ogg. `serve_media`
(`draai/server.py:698-752`) opens the original file, seeks to the requested
byte offset, and streams raw chunks (65536-byte reads) straight to the
socket — there is no decode/re-encode step anywhere in this path. It parses
`Range: bytes=start-end` (including open-ended and suffix forms,
`draai/server.py:713-729`) and replies `206 Partial Content` with
`Content-Range`/`Accept-Ranges: bytes`, which is what lets Sonos/Cast seek
and what CLAUDE.md means by "with HTTP Range support (Sonos needs it)". The
practical consequence: a FLAC stays bit-identical end to end (lossless in,
lossless out), and a low-bitrate MP3 is never "upgraded" — DRAAI never
touches the audio bytes, only the container's tag bytes (for reading) are
parsed.

### Search / sort / group

Search and sort/group are split across the server and the client:

- **Search** is server-side, in the `GET /api/tracks` handler
  (`draai/server.py:338-358`). The query string is lower-cased, split on
  whitespace, and every word must appear (AND, substring match) somewhere in
  `title + " " + artist + " " + album + " " + folder`
  (`draai/server.py:344-349`) — there's no per-field search syntax. Results
  are capped at the first 3000 matches (`items[:3000]`,
  `draai/server.py:356`) but `total` reports the untruncated match count
  (`draai/server.py:357`).
- **Sort** is client-side, `sortTracks()` in `player_ui.html:1239-1246`.
  Modes: `title` (case-insensitive), `artist` (artist then title, `"~"`
  fallback so untagged-artist tracks sort last), `added` (numeric,
  negated for newest-first). Direction is a `±1` multiplier (`A.dir`,
  toggled by `#dirBtn`, `player_ui.html:1311`). The sorted view is then
  capped at 500 rows (`arr.slice(0,500)`, `player_ui.html:1245`) — this is
  a second, tighter cap on top of the server's 3000.
- **Group-by** is also client-side, `renderSongRows()`
  (`player_ui.html:1248-1275`). `A.group` is `"none"`, `"folder"`, or
  `"artist"`; grouping is a simple `Map` keyed by `t.folder||"Library"` or
  `t.artist||"Unknown artist"`, preserving first-seen order (JS `Map`
  iteration order), so effective group order follows the current sort, not
  alphabetical group names. Each folder-group header's tooltip is the real
  containing directory (`ts[0].dir`, `player_ui.html:1265`, itself sourced
  from `os.path.dirname(t["path"])` set server-side at
  `draai/server.py:355`), while the folder chip only shows the last path
  segment (`name.split("/").pop()`) — so nested folders with the same leaf
  name are visually indistinguishable except by that tooltip. Group
  collapse state persists per `(group-mode, group-name)` key in UI prefs
  (`A.collapsed`, `savePrefs()`), which is server-persisted via `/api/prefs`
  per the "no localStorage" rule.

### yt-dlp integration (draai/youtube.py)

DRAAI never bundles, downloads, or installs yt-dlp — it only looks for it
on the user's machine and hands off a URL, matching `CLAUDE.md` rule 4 and
the "no site-specific downloader code" rule.

- `yt_available()` (`draai/youtube.py:19-21`) reports whether both
  `yt-dlp` and `ffmpeg` are found via `find_tool()` (PATH, then
  `/opt/homebrew/bin` / `/usr/local/bin`, `draai/media.py:26-34`); the UI
  hides the import section entirely if either is missing
  (`player_ui.html:1201`).
- `start_youtube_job(url)` (`draai/youtube.py:24-72`) raises immediately
  with an actionable message (`"brew install yt-dlp ffmpeg"`) if a tool is
  missing (`draai/youtube.py:26-28`). Otherwise it spins up a background
  daemon thread and returns a job id right away; progress is polled via
  `yt_jobs[job_id]` (`state.yt_jobs`, a plain dict — no persistence, jobs
  vanish on restart).
- The background thread does two subprocess calls to the *user's own*
  `yt-dlp` binary: first `yt-dlp --no-playlist --print title <url>`
  (best-effort, just for a UI label,
  `draai/youtube.py:37-44`), then the real extraction:
  `yt-dlp --no-playlist -x --audio-format mp3 --audio-quality 0
  --ffmpeg-location <ffmpeg> --embed-metadata --embed-thumbnail
  --convert-thumbnails jpg -o "<Imported>/%(title)s.%(ext)s" <url>`
  (`draai/youtube.py:49-55`) — exactly the flags CLAUDE.md documents.
  Output always goes to `<first configured library folder>/Imported/`
  (`draai/youtube.py:45-48`; falls back to `~/Music/Imported` if no
  folders are configured), created with `os.makedirs(..., exist_ok=True)`.
- No URL validation or site-specific parsing happens in DRAAI's code — the
  `YT_URL_RE` regex (`draai/youtube.py:14`) is defined but not actually
  referenced anywhere in `start_youtube_job`; whatever URL string the UI
  sends is passed straight to yt-dlp, which does all URL/site handling
  itself. This is a discrepancy worth knowing: the regex looks like
  validation but isn't wired in — the client-side form input is the only
  gate.
- On success, `scan_all()` is called synchronously in the worker thread
  (`draai/youtube.py:60`) so the new file appears in the library without a
  manual rescan; on non-zero exit or timeout (30 min cap,
  `draai/youtube.py:55`), the job is marked `"error"` with the last
  non-empty stderr/stdout line as the message (`draai/youtube.py:56-59`) —
  kept short and human per CLAUDE.md rule 5.

### Playlists (draai/playlists.py)

Plain `.m3u` files, one per playlist, living in
`<first library folder>/Playlists/` (`playlists_dir()`,
`draai/playlists.py:9-12` — same "first folder" convention as `Imported/`).
No database, no DRAAI-specific format: any player can open these files.

- `safe_playlist_name(name)` (`draai/playlists.py:15-17`) strips path
  separators and control characters and truncates to 80 chars, so playlist
  names double as filenames safely (`"Mix/1"` becomes the file
  `Mix-1.m3u`, per `tests/test_draai.py:334-343`).
- `save_playlist(spk, name)` (`draai/playlists.py:37-55`) reads the
  *current Sonos queue* via `browse_queue(spk)`, keeps only entries that
  resolve back to a known library track (`tracks_by_id.get(...)`,
  `draai/playlists.py:42-43` — non-library queue items are silently
  dropped), and writes an `#EXTM3U` file with `#EXTINF:-1,<title>` +
  the track's absolute `path` per entry (`draai/playlists.py:52-54`).
  Raises if nothing in the queue matched a library track.
- `load_playlist(name)` (`draai/playlists.py:58-74`) reads the file back
  line by line, resolves each non-comment line (an absolute path) against
  a fresh `path -> id` map built from the *current* `tracks` list
  (`draai/playlists.py:64-65`). It returns `(ids_present, total_lines)` —
  paths that no longer exist in the library (moved/deleted files, or
  files from a folder that's since been removed) are silently skipped, so
  the two counts can diverge; callers should surface that gap to the user
  rather than assume every line loaded.
- `list_playlists()` (`draai/playlists.py:20-34`) just lists `*.m3u` files
  in the directory and counts non-comment lines per file (best-effort,
  swallows read errors as count `0`). `delete_playlist` removes the file
  if present, otherwise no-ops.

### Shape of a track object reaching the API/UI

Two different, overlapping shapes exist — know which one you're looking at:

- **Internal record** (`state.tracks` / `state.tracks_by_id`), built by
  `_scan_root` (`draai/library.py:326-336`):
  `{id, path, added, title, artist, album, has_art, folder, ext}`. `path`
  is an absolute filesystem path and is never sent to the client directly.
- **`GET /api/tracks` wire shape** (`draai/server.py:350-356`):
  `{id, title, artist, album, has_art, added, dir, folder}` — `path` is
  replaced with `dir` (`os.path.dirname(path)`, used only for the
  folder-group tooltip), and `ext`/the raw `path` are omitted (the client
  never needs the extension directly; `/media/<id>/<name>` resolves it
  server-side via the track lookup, and `media_url()` reconstructs the
  filename from `title + ext`, `draai/media.py:22`).
- Art is never embedded in the track JSON — the client fetches it
  separately via `GET /api/art?id=<id>` (`draai/server.py:378-392`), which
  streams `get_art()`'s cached `(mime, bytes)` tuple with a 24h
  `Cache-Control`.

## Gotchas

- **wav/aiff/ogg are metadata-blind.** They scan and play fine (byte
  streaming doesn't care about tags) but show only their filename as title
  and never show artist/album/art — don't assume `read_tags` covers every
  extension in `AUDIO_EXTS`; it only covers three formats
  (`draai/library.py:241-252`).
- **Three different result caps stack**: server search caps at 3000
  (`draai/server.py:356`), client sort caps at 500
  (`player_ui.html:1245`), and group-by operates on whatever the sort step
  already truncated to — so a library with >3000 matches for a search term,
  or >500 tracks in one grouped/sorted view, will silently hide the tail
  rather than error. There's no pagination.
- **`YT_URL_RE` is dead code** (`draai/youtube.py:14`) — it's never
  referenced in `start_youtube_job`; don't assume DRAAI validates URLs
  before shelling out to yt-dlp.
- **`art_cache` eviction is insertion-order, not LRU** — it evicts the
  oldest-*inserted* entry (`draai/library.py:264-265`), not the
  least-recently-*accessed* one, so a frequently-viewed track's art can
  still be evicted while a stale one lingers.
- **Playlists silently drop stale entries.** `load_playlist` returns fewer
  ids than `total` lines whenever a path in the `.m3u` no longer matches a
  currently-scanned track (moved file, removed folder) — callers must
  check both numbers, not just assume `ids` == the saved playlist.
  `save_playlist` similarly drops any queued item that isn't a known
  library track (e.g., something added ad hoc) without warning beyond the
  final count.
- **Multi-folder `folder` prefixing changes track identity of the *field*,
  not the id.** The `folder` string a track carries depends on how many
  folders are currently configured at scan time (`multi` flag,
  `draai/library.py:279`, `318-319`) — removing a folder and rescanning
  with only one left changes every remaining track's `folder` value (drops
  the root-name prefix), which affects group-by but not the track `id`
  (still `sha1(path)`-based) or playlist matching (path-based).
- **`_scan_root` re-parses tags for every file on every scan** — there is
  no mtime/cache check before calling `read_tags`, so `scan_all()` cost is
  proportional to total library size each time it runs (folder add/remove,
  manual rescan, post-yt-dlp-import). Fine for typical personal libraries,
  worth knowing before assuming it's cheap to call frequently.
- **`CLAUDE.md` vs. code**: the documented behavior ("hands the URL to the
  user's own yt-dlp installation; downloads land in `<first
  folder>/Imported/` with `--embed-metadata --embed-thumbnail`") matches
  the code exactly (`draai/youtube.py:49-55`) — no discrepancy found there.
  The one gap found during this review is the unused `YT_URL_RE` noted
  above, which isn't mentioned in `CLAUDE.md` either way.

## References

- `draai/library.py:10-29` — `_syncsafe`, `_id3_text` (ID3 integer/text decoding).
- `draai/library.py:32-42` — `_null_split` (null/double-null terminator scan for tag strings/art).
- `draai/library.py:45-104` — `_tags_mp3` (ID3v2.2/2.3/2.4 frame walk + APIC/PIC art).
- `draai/library.py:107-179` — `_mp4_children`, `_tags_mp4` (moov/udta/meta/ilst box walk + covr art).
- `draai/library.py:182-238` — `_tags_flac` (VORBIS_COMMENT + PICTURE blocks).
- `draai/library.py:241-252` — `read_tags` dispatcher (extension-based; wav/aiff/ogg unhandled).
- `draai/library.py:257-267` — `get_art` (on-demand re-read + bounded cache).
- `draai/library.py:274-337` — `scan_all`, `scan_folder`, `_scan_root` (walk, track id, multi-folder prefix, `added` timestamp).
- `draai/media.py:20-34` — `media_url`, `find_tool`.
- `draai/constants.py:5-14` — `AUDIO_EXTS` (supported extensions/MIME types).
- `draai/server.py:338-358` — `GET /api/tracks` (search + wire shape + 3000 cap).
- `draai/server.py:378-392` — `GET /api/art`.
- `draai/server.py:511-543` — `/api/folder`, `/api/folders_add`, `/api/folders_remove`.
- `draai/server.py:698-752` — `serve_media` (Range support, raw byte streaming).
- `draai/playlists.py:9-81` — full m3u playlist implementation.
- `draai/youtube.py:14-72` — `YT_URL_RE` (unused), `yt_available`, `start_youtube_job`.
- `player_ui.html:1239-1275` — `sortTracks`, `renderSongRows` (client-side sort/group).
- `tests/test_draai.py:174-209` — tag-reading and multi-folder scan tests (`test_mp3_tags_and_art`, `test_flac_tags`, `test_scan_multiple_folders`).
- `tests/test_draai.py:334-343` — `test_playlist_roundtrip`.
