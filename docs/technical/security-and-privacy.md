# Security & privacy

> DRAAI's privacy is an **architectural property, not a policy promise**. There
> is no cloud, no account, and no telemetry; the engine imports only the Python
> standard library (no supply chain); the web UI loads nothing from the
> internet; and every network connection the engine makes targets a device on
> your own LAN. Two tests keep those invariants true on every push. The one
> deliberate way bytes leave your network is your own yt-dlp, on a URL you
> paste. Within your LAN, DRAAI trusts your network — it has no authentication.

## Purpose

Most apps ask you to *trust* a privacy promise. DRAAI is small and
dependency-free enough that you can *verify* it instead. This doc is the audit
map: what leaves your machine (and what can't), the exact egress points in the
code, the trust boundary we assume, and the CI guards that stop a future change
from quietly breaking any of it.

## Where it lives

- `draai/backends.py` — Sonos discovery (SSDP) and control (SOAP over HTTP)
- `draai/cast.py` — Google Cast discovery (mDNS) and control (CASTV2 over TLS)
- `draai/media.py` — a UDP "which interface am I on" probe
- `draai/__main__.py` — the listening socket bind
- `ui/` — the served interface (assembled by `draai/ui.py`)
- `tests/test_draai.py` — `test_engine_imports_only_stdlib`,
  `test_ui_loads_nothing_from_the_internet`
- `.github/workflows/tests.yml` — runs the suite on every push and PR

## The guarantees

**No supply chain.** The engine is pure Python standard library — no
`requirements.txt`, no pip install, no transitive packages (`CLAUDE.md` rule 1).
The entire trust surface is the code in this repo plus the Python stdlib plus
your OS. There is no third-party package in the import graph that could read
your library and phone home, and no dependency that can be swapped out from
under you. `test_engine_imports_only_stdlib` walks every `import` in `draai/`
and fails the build if any resolves to a non-stdlib module.

**No egress to the internet.** Every outbound connection the engine opens
targets a device on your local network. The complete inventory:

| Where | What | Target |
|---|---|---|
| `backends.py:46` | SSDP `M-SEARCH` (Sonos discovery) | multicast `239.255.255.250:1900` (LAN) |
| `backends.py:194` | SOAP control (`SetAVTransportURI`, queue, volume, …) | `http://<speaker-ip>:1400` (LAN) |
| `cast.py:142` | mDNS query (Cast discovery) | multicast `224.0.0.251:5353` (LAN) |
| `cast.py:228` | CASTV2 control | TLS to `<cast-ip>:8009` (LAN) |
| `media.py:14` | UDP interface probe¹ | `connect((<speaker-ip>, 1400))` (LAN, no bytes sent) |

¹ Connecting a UDP socket doesn't transmit anything — it's the standard trick
for asking the OS which local IP would route to the speaker, so the engine can
put a reachable address in the media URLs it hands the speaker.

There is no analytics endpoint, no update check, and no crash reporting
anywhere in the tree.

**The UI loads nothing external.** The served interface is one self-contained
offline document: all CSS and JS inline, no CDN, no web fonts, no external
scripts or stylesheets. Every request the browser makes is a relative
`/api/...` call to your own engine. So there is nothing to track you with, and
the panel works with the internet unplugged. `test_ui_loads_nothing_from_the_internet`
asserts the assembled UI contains no `https://`, no `@import`/`<link>`/external
`src`, and only relative `fetch()` targets.

**Local storage only.** Preferences (`config.json`), resume positions
(`positions.json`), and the analysis cache live on your disk under
`~/Library/Application Support/SonosMP3Player/`. Nothing syncs anywhere. The UI
deliberately uses no `localStorage`/`sessionStorage` either — preferences round-trip
through the engine so there's a single, local source of truth.

**The one deliberate egress: yt-dlp.** The optional import box hands a URL *you
pasted* to *your own* yt-dlp install (`CLAUDE.md` rule 4 — no site-specific
downloader code ships here). That request is user-initiated and uses a tool you
installed yourself. It is the only path by which DRAAI reaches beyond your LAN,
and only when you ask it to.

## Threat model

**What this design defends against:** your listening habits, library contents,
or file paths being sent to the project's authors or any third party. It can't
happen because there is no code that sends them anywhere, no dependency that
could, and no external asset in the UI that could beacon.

**What it trusts:** your local network. DRAAI is built for a home Wi-Fi where
you trust the other devices — the same assumption the Sonos app makes.

## Boundaries (the honest limits)

A credible security doc names what it does *not* do. All three below are
consequences of the "trusted home LAN" model:

- **No authentication on the API, the phone remote, or `/media`.** Anyone who
  can reach the engine on your LAN can control playback, browse your library,
  and fetch your files. This is what makes the QR remote frictionless; it also
  means the trust boundary is your network, not a login.
- **The server binds `0.0.0.0`** (`__main__.py:96`) — all interfaces. It has to:
  Sonos and Cast devices fetch the media *from* your Mac, so the socket must be
  reachable on the LAN, not just `localhost`.
- **Media is served over plain HTTP** on the LAN, unencrypted, because Sonos
  requires an HTTP URL with Range support (see `http-and-media.md`). It never
  touches the internet, but it isn't encrypted in transit on your LAN.

**Practical implication:** run DRAAI on a network you trust (home Wi-Fi). On an
open or shared network (a café, a hotel), don't expose it — others on that
network could reach the API.

**Not built (a deliberate non-goal, for now):** an optional access token /
shared secret on the API and remote, for people who want to run on a less
trusted network. It would harden the LAN-access axis at the cost of the
zero-friction guest remote, so it's a future opt-in feature rather than a
default. Raise an issue if you want it.

## How the invariants are guarded (CI)

The two properties most easily broken by a well-meaning future change — "no pip
dependency" and "the UI never phones home" — are enforced by tests, not just by
convention in `CLAUDE.md`:

- **`test_engine_imports_only_stdlib`** — parses every `draai/*.py`, collects
  its imports, and fails if any resolves outside the standard library. Adding a
  third-party package turns the suite red.
- **`test_ui_loads_nothing_from_the_internet`** — assembles the UI and asserts
  no external asset references and only local `fetch()` targets.

Both run in `.github/workflows/tests.yml` on every push and PR, so a regression
is caught before it can ship in a `draai.pyz`.

## Verify it yourself

You don't have to take this doc's word for it:

1. `grep -rn "urlopen\|urllib.request\|socket\|create_connection" draai/` — every
   hit is a LAN target from the table above.
2. There is no `requirements.txt` / `pyproject` dependency list and no
   `pip install` step anywhere — `python3 -m draai` runs on a stock interpreter.
3. Run it behind an outbound-connection monitor (Little Snitch, `lsof -i`, or
   your router) and watch: you'll see traffic only to your speakers, plus
   whatever *your* yt-dlp does when *you* paste a link.
4. Read the whole engine. It's a few thousand lines of stdlib Python — a
   readable afternoon, not a black box.

## References

- `CLAUDE.md` — rules 1 (stdlib-only), 3 (no cloud/accounts/telemetry), 4 (yt-dlp)
- [architecture.md](architecture.md) — storage paths, threading, the bind, `_load_ui`
- [http-and-media.md](http-and-media.md) — the `/api` surface, `/media` Range serving, why HTTP
- [google-cast.md](google-cast.md) — the TLS CASTV2 transport
- `tests/test_draai.py` — the two guard tests above
