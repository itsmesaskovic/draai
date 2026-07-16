# Sonos protocol

> Purpose: explain how DRAAI talks to real Sonos/Symfonisk hardware — discovery,
> the hand-rolled SOAP client, queue/grouping/volume/EQ control, and the
> resume/now-playing bridge — so an agent can change this code without
> silently breaking real speakers (there is no way to unit-test hardware
> quirks; the mock in `tests/test_draai.py` encodes what's been learned
> the hard way).

## Purpose

Sonos speakers are controlled entirely with UPnP/SOAP over HTTP on port
1400 — no cloud, no Sonos app, no external SDK (stdlib-only, per
`CLAUDE.md`). All of this logic lives in one file,
`draai/backends.py`, and is dispatched alongside the Google Cast backend
(`draai/cast.py`) by checking `spk.get("backend")` at the top of nearly
every public function.

## Where it lives

- `draai/backends.py` — everything: SSDP discovery, zone topology
  parsing, the SOAP client, DIDL-Lite metadata, queue ops, grouping,
  volume, EQ, sleep timer, resume positions, room listing.
- `draai/media.py` — `media_url()` builds the HTTP URL a speaker fetches
  audio from; `local_ip_facing()` picks the Mac's IP as seen from the
  speaker's subnet.
- `draai/library.py:315` — track id = first 16 hex chars of
  `sha1(path)`; this id is embedded in every media URL and is the only
  thing tying a Sonos `TrackURI` back to a library track.
- `tests/test_draai.py` — `SoapMock` (around line 96) stands in for a
  real household and encodes real queue/topology semantics; the test
  suite currently has 23 tests (`python3 tests/test_draai.py`), not the
  15 the top-level `CLAUDE.md` mentions — that count is stale, re-verify
  rather than trust either number.

## Key concepts

### Discovery: SSDP then zone topology

`ssdp_discover()` (`draai/backends.py:35`) sends two UDP M-SEARCH
multicasts to `239.255.255.250:1900` with
`ST: urn:schemas-upnp-org:device:ZonePlayer:1`, then listens for replies
for up to `timeout` seconds (default 3s), collecting sender IPs whose
response text contains `"Sonos"` or `"ZonePlayer"`. This only yields raw
device IPs, not rooms or groups.

`refresh_speakers()` (`:73`) drives the whole discovery cycle:
1. Union SSDP results with any manually-added IPs (`config["manual_ips"]`).
2. For each candidate IP (sorted, so it's deterministic), call
   `get_zone_groups(ip)` — the *first* IP that returns a non-empty
   topology wins; the rest are never queried. Any single reachable
   speaker can describe the entire household.
3. Tag every Sonos group with `"backend": "sonos"`, discover Cast
   devices via `cast_discover()`, and merge the two lists into
   `state.speakers` under `state_lock`.
4. Returns `(merged, None)` on success. On failure it distinguishes "we
   found devices but couldn't read topology" (returns the SOAP error
   string) from "found nothing at all" (returns a human, Wi-Fi-focused
   message) — see `:97-103`.

`get_zone_groups(any_ip)` (`:106`) asks that one speaker for
`GetZoneGroupState` (SOAP call, see below), then parses the embedded,
double-escaped `<ZoneGroupState>` XML blob. For each `<ZoneGroup>`:

- The `Coordinator` attribute names the coordinator's UUID; the member
  element with a matching `UUID` is looked up as `coord`.
- Every `<ZoneGroupMember>` is walked. `Invisible="1"` marks a member
  that is bonded to another physical unit in the same room (e.g. one
  half of a stereo pair, or a bonded sub) rather than an independently
  selectable room — its name is *not* added to the group's display
  name list (`:137-141`), but it's still registered as a physical
  device via `add_member(mem, fixed=invisible)` (`:144`). So: visible
  members are movable/joinable rooms; invisible members become
  `fixed: true` entries in `members[]` and must never be sent
  `BecomeCoordinatorOfStandaloneGroup` or otherwise ungrouped — Sonos
  will reject it or desync the bonded pair.
- Each member's `<Satellite>` children (surrounds/height channels
  bonded under a soundbar, or the second unit of certain stereo setups)
  are added the same way, always `fixed=True` (`:145-146`), regardless
  of the parent's own `Invisible` value.
- Same-named devices within a group (e.g. two "Living Room" stereo-pair
  units) get their display name disambiguated with the last IP octet:
  `"Living Room · 52"` (`:147-156`), so the UI can tell them apart in
  the per-room volume list.
- A group with no coordinator match or no visible names is skipped
  (`:157-158`, e.g. a group that is 100% invisible/bonded members with
  no independently-named room).
- The group's own display name is the coordinator's zone name plus any
  *other* distinct visible names joined with `" + "` (multi-room-bonded
  groups, e.g. "Kitchen + Dining") — `:163-165`.

`tests/test_draai.py:213` (`test_zone_groups_members_and_fixed`) is the
executable spec for this: one group with a coordinator, one invisible
bonded member, and one `<Satellite>` — asserts exactly the coordinator
is non-fixed and the other two are `fixed: true`.

### soap_call() and avt(): the transport

`soap_call(ip, path, service, action, args)` (`:184`) builds a minimal
SOAP 1.1 envelope by hand — no XML library on the request side, just
string formatting with `xml_escape()` on argument values — and POSTs it
to `http://<ip>:1400<path>` with:
- `Content-Type: text/xml; charset="utf-8"`
- `SOAPACTION: "<service>#<action>"`
- `Connection: close`

An 8-second timeout applies to every call. On an HTTP error the response
body is scraped for `<errorCode>` and re-raised as a `RuntimeError` with
a human-readable UPnP error code (`:207-211`) — callers never see a raw
`HTTPError`.

Three fixed `(path, service)` pairs are used throughout:
- `AVT` = `/MediaRenderer/AVTransport/Control`,
  `urn:schemas-upnp-org:service:AVTransport:1` (`:172-173`)
- `RC` = `/MediaRenderer/RenderingControl/Control`,
  `urn:schemas-upnp-org:service:RenderingControl:1` (`:176-177`)
- `GRC` = `/MediaRenderer/GroupRenderingControl/Control`,
  `urn:schemas-upnp-org:service:GroupRenderingControl:1` (`:180-181`)
- (plus `CD` = ContentDirectory, at `:443-444`, used only for
  `Browse`/queue reads, and `ZoneGroupTopology`, used inline in
  `get_zone_groups`.)

`avt(ip, action, args=None)` (`:214`) is a thin wrapper around
`soap_call` for the AVTransport service that always injects
`InstanceID: 0` — nearly every playback/queue/transport call goes
through this helper.

### DIDL-Lite metadata and the cdudn desc element

`didl_for(track, url)` (`:220`) builds the `<DIDL-Lite>` XML document
sent as `EnqueuedURIMetaData` when adding a track to the queue. It's
intentionally minimal: `dc:title`, `upnp:class` =
`object.item.audioItem.musicTrack`, a `<res>` pointing at the media URL
with a `protocolInfo` built from the track's MIME type
(`AUDIO_EXTS.get(track["ext"], "audio/mpeg")`), and — critically —

```xml
<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">
  RINCON_AssociatedZPUDN
</desc>
```

Without this `<desc>` element, real Sonos hardware silently accepts the
queue add but drops the title metadata and displays the raw filename
instead — this cost real debugging time (see `CLAUDE.md`'s "paid for in
blood" gotchas) and there is no error to catch; it just looks wrong on
the speaker's own display/app. `tests/test_draai.py:229`
(`test_play_sends_didl_with_desc`) exists specifically to keep this
element from being refactored away.

### Queue operations

`play_tracks(spk, ids)` (`:248`) replaces the whole queue: clears it
(`RemoveAllTracksFromQueue`), adds the first track, points
`CurrentURI` at `x-rincon-queue:<uuid>#0`, seeks to `TRACK_NR 1`, plays,
then optionally seeks into a saved resume position (see below). The
remaining tracks are appended in a background daemon thread
(`add_rest`, `:291-304`) so playback starts immediately even for a
500-track queue; an `enqueue_generation` counter guards against a
second `play_tracks`/`enqueue_tracks` call racing with a still-running
background append — if the generation changed, the thread just stops
appending (`:293-294`, `:568-569`).

`enqueue_tracks(spk, ids, play_next=False)` (`:507`) either appends to
the end of the queue, or — with `play_next` — inserts the whole block
immediately after the currently-playing track, in order, using
`DesiredFirstTrackNumberEnqueued` + `EnqueueAsNext: 1` per track
(`:532-541`). It also auto-starts playback if the queue was empty and
nothing was already playing (`:554-562`).

`queue_move(spk, from_no, to_no)` (`:475`) is the one with a real gotcha
documented inline and in `CLAUDE.md`: Sonos's `ReorderTracksInQueue`
`InsertBefore` parameter is evaluated against the queue **as it stood
before the move's removal**, not after. So moving an item *down* the
queue needs `to_no + 1` to land in the right slot; moving it *up*
needs the raw `to_no`:

```python
insert_before = to_no if to_no < from_no else to_no + 1
```
(`:484`)

Worked example, taken directly from `test_queue_move_semantics`
(`tests/test_draai.py:314-323`) — queue starts as `[A, B, C, D, E]`
(1-based positions 1-5):

- **Move up**: `queue_move(spk, 5, 2)` moves E (position 5) up to
  position 2. `to_no(2) < from_no(5)` → `insert_before = to_no = 2`
  (SOAP call: `StartingIndex=5, InsertBefore=2`). Result:
  `[A, E, B, C, D]`.
- **Move down**: on that new queue, `queue_move(spk, 2, 4)` moves
  whatever is now at position 2 — E — down to position 4.
  `to_no(4) >= from_no(2)` → `insert_before = to_no + 1 = 5`
  (SOAP call: `StartingIndex=2, InsertBefore=5`). Result:
  `[A, B, C, E, D]`.

The `+1` on the down-move compensates for `InsertBefore` still being
counted against the *pre-removal* queue: removing the item at
`StartingIndex` shifts every later index left by one, so a naive
`InsertBefore = to_no` would land the item one slot too early. `SoapMock`
(`tests/test_draai.py:117-124`) implements this exact pre-removal-index
adjustment and both real hardware and the mock agree on it — if you
touch `queue_move()`, run `test_queue_move_semantics`
(`tests/test_draai.py:314-323`) rather than re-deriving the formula by
hand.

`queue_jump(spk, no)` (`:490`) points `CurrentURI` at the queue and
seeks to `TRACK_NR <no>`. `queue_remove(spk, no)` (`:500`) calls
`RemoveTrackFromQueue` with `ObjectID: "Q:0/<no>"`. `browse_queue(spk)`
(`:447`) does a `ContentDirectory.Browse` on `ObjectID: "Q:0"` and, for
each `<item>`, recovers the track id by regexing the DRAAI media URL
pattern `/media/([0-9a-f]{16})/` out of the DIDL — this is how the UI
maps queue rows back to library tracks (`:464-470`).

### Grouping

Join: `group_join(member_uuid, coordinator_uuid)` (`:637`) sends
`SetAVTransportURI` with `CurrentURI: x-rincon:<coordinatorUUID>` to the
*member's own IP* (`:644-646`) — the member effectively points its
transport at the coordinator's stream. Leave:
`group_leave(member_uuid)` (`:651`) calls
`BecomeCoordinatorOfStandaloneGroup` on the member's IP (`:658`), which
splits it back into its own single-speaker group. Both sleep 0.5s then
call `refresh_speakers()` so the caller gets updated topology
immediately (`:647-648`, `:659-660`) — the SOAP call itself returns
before Sonos has fully re-published `GetZoneGroupState`.

Both functions reject Cast-backend speakers up front with a
`RuntimeError` (`:638-640`, `:652-654`) — Sonos grouping semantics don't
apply to Cast, and DRAAI does not attempt to bridge them (`CLAUDE.md`:
"Sonos+Cast cannot play in sync — don't promise it").

Bonded units (the `fixed: true` members from zone-group parsing) are
never independently join/leave targets in the UI, because they were
never listed as their own room — but nothing in `group_join`/
`group_leave` itself checks the `fixed` flag; the guard is
enforced by what `zone_by_uuid()` / the UI expose as selectable rooms,
not by these two functions. An agent adding a new caller of
`group_leave`/`group_join` must keep using `fixed` members as
non-targets, since the SOAP layer will happily accept a bonded unit's
UUID and likely desync the pair.

### Volume

Group volume is the normal path: `set_volume(spk, value)` (`:406`)
calls `GroupRenderingControl.SetGroupVolume` on the *coordinator's* IP
(`GRC`, `:412-413`), which scales every member proportionally. If that
fails (older firmware / non-grouped edge cases), it falls back to plain
`RenderingControl.SetVolume` with `Channel: Master` on the same IP
(`:414-417`).

Per-device volume is different: `set_room_volume(member_uuid, value)`
(`:680`) resolves the *member's own* IP via `zone_by_uuid()` and calls
`RenderingControl.SetVolume` directly on it (`:687-689`) — this is how
the UI's per-room volume sliders work inside an open group.
`get_rooms(spk)` (`:663`) reads each member's individual volume the
same way (`GetVolume` on `m["ip"]`, `:668-669`) to populate that list,
also surfacing `fixed` (`:676`) so the UI can decide whether to show an
"ungroup" affordance.

### EQ

`get_eq(spk)` / `set_eq(spk, bass=None, treble=None, loudness=None)`
(`:584`, `:601`) are per-device `RenderingControl` calls: `GetBass`/
`SetBass` and `GetTreble`/`SetTreble` clamp to `[-10, 10]` (`:608`,
`:612`), and `GetLoudness`/`SetLoudness` toggle a boolean loudness
compensation flag (`Channel: Master`). Both raise a `RuntimeError` up
front for Cast speakers — "The equalizer isn't available on Chromecast."
(`:586`, `:603`) — since Cast has no equivalent RenderingControl
service.

### Resume and now-playing identity

Track identity threads through the whole system as a 16-hex-char id:
`sha1(path)[:16]` at scan time (`draai/library.py:315`). `media_url()`
(`draai/media.py:20-23`) embeds it directly in the URL path:
`http://<mac-ip>:<port>/media/<id>/<urlencoded-title-ext>`. That URL
becomes the `<res>` in `didl_for()`, so whatever the speaker reports
back as `TrackURI` contains this same id.

`get_status(spk)` (`:349`) polls `GetPositionInfo`, extracts
`TrackURI`, and regexes `/media/([0-9a-f]{16})/` back out of it
(`:378-382`) — this recovered id is what makes now-playing highlighting
in the UI work, and (when position/duration are both known) it feeds
`note_position(track_id, pos, dur)` (`:386`, defined `:338`). **Do not
change the `/media/<id>/<name>` URL shape** — anything that breaks this
regex breaks now-playing highlight and resume simultaneously, with no
loud failure (it just silently stops updating).

`note_position()` only bothers for tracks whose total duration is
≥ `RESUME_MIN_TRACK` = 600s / 10 minutes
(`draai/constants.py:24`, checked at `:339-340`) — the idea being sets,
mixes, and audiobooks, not regular songs. Within a long track, it drops
the saved position once you're within 120s of the end or before
`RESUME_MIN_POS` = 90s (`draai/constants.py:25`, `:342-343`) — "finished
or barely started" is treated as "nothing to resume." Positions live in
`state.positions`, guarded by `state.positions_lock`, and are flushed
to disk debounced by 10s (`save_positions_soon()`, `:319-335`) at
`POSITIONS_PATH` = `~/Library/Application Support/SonosMP3Player/positions.json`
(`draai/constants.py:23`). `play_tracks()` re-seeks into a saved
position (minus a 5s rewind buffer) when starting a track that has one,
via `REL_TIME` `Seek` (`:280-287`).

### speaker_by_uuid and the rooms list

`speaker_by_uuid(uuid)` (`:240`) does a linear scan of `state.speakers`
under `state_lock` and returns a *copy* (`dict(s)`) — every
backend-dispatch check (`spk.get("backend") == "cast"`) that guards
Sonos-only operations reads this copied dict, not a live reference.
`zone_by_uuid(zuuid)` (`:626`) is the finer-grained lookup: it searches
both group-level uuids and individual `members[]` uuids, so it resolves
either "the coordinator/group" or "one specific room inside a group" —
this is what `group_join`, `group_leave`, and `set_room_volume` use to
get a concrete IP to call. Both `state.speakers` (the list returned to
`/api/state`, `draai/server.py:327-337`) and the members list are built
straight from `get_zone_groups()`'s output — there's no separate "rooms"
data structure; `get_rooms(spk)` (`:663`) just re-derives per-member
volumes on demand from `spk["members"]`.

### Startup discovery retry and UI polling

Because Sonos SSDP discovery can be slow right after the Mac boots (or
right after this process starts), `draai/__main__.py:121-129`
(`warmup()`) retries `refresh_speakers()` up to 4 times with a 4s sleep
between attempts, stopping early on the first non-empty result — all in
a background daemon thread so the HTTP server is already serving the
page while this runs. `draai/server.py:498` and `:509` expose the same
`refresh_speakers()` call synchronously via `/api/rescan_speakers` and
`/api/add_ip` for the UI's manual "Rescan" button.

On the frontend, `boot()` (`draai/player_ui.html:1185-1204`) calls
`refreshState()` once, and if no speakers came back yet, starts its own
independent retry loop: every 2.5s, up to 12 tries (~30s), it calls
`refreshState()` again and auto-selects a room once one appears
(`:1193-1200`). Steady-state polling for playback state is separate —
`startPolling()` (`:788`, only started once a speaker is selected,
`:1203`) calls `/api/state`-adjacent `poll()` every 2 seconds.

## Gotchas

- Missing `<desc id="cdudn">…</desc>` in DIDL-Lite → real hardware shows
  raw filenames instead of titles, with no error. See `didl_for()`,
  `draai/backends.py:220-237`.
- `ReorderTracksInQueue`'s `InsertBefore` is pre-removal-indexed — moving
  an item down the queue needs `to_no + 1`; moving up uses `to_no`
  as-is. Get this wrong and tracks land one slot off. See
  `queue_move()`, `draai/backends.py:475-487`, and the semantics test at
  `tests/test_draai.py:314-323`.
- Never call `group_leave`/`BecomeCoordinatorOfStandaloneGroup` (or
  `group_join`) against a member whose zone entry has `fixed: true` —
  those are bonded stereo/satellite units, not independent rooms;
  Sonos will refuse or desync the pair. `fixed` comes straight from
  `Invisible="1"` / `<Satellite>` parsing in `get_zone_groups()`,
  `draai/backends.py:125-146`.
- Group volume (`GroupRenderingControl` on the *coordinator*) and
  per-device volume (`RenderingControl` on the *member's own IP*) are
  different services on different endpoints — don't conflate them; see
  `set_volume()` (`:406`) vs `set_room_volume()` (`:680`).
- The `/media/<16-hex-id>/<name>` URL shape is load-bearing: it's how
  `GetPositionInfo`'s `TrackURI` is parsed back into a track id for
  now-playing highlight and resume (`get_status()`,
  `draai/backends.py:378-382`). Changing the URL layout silently breaks
  both features — there's no exception, matches just stop happening.
- `soap_call()` always has an 8s timeout and converts UPnP faults into
  `RuntimeError("Speaker refused <action> (UPnP error <code>)")`
  (`draai/backends.py:204-211`) — callers rely on catching plain
  `RuntimeError`/`Exception`, not `urllib.error.HTTPError`.
- EQ (`get_eq`/`set_eq`) and grouping (`group_join`/`group_leave`) both
  explicitly reject Cast-backend speakers with a `RuntimeError` up
  front — don't remove those guards when refactoring backend dispatch,
  Cast has no equivalent services.
- Do not run any of this against real hardware or start playback to
  "verify" — per project memory (`no-unprompted-playback`), verify via
  `tests/test_draai.py` and the `SoapMock`, not real speakers (people
  and dogs are home).

## References

- `draai/backends.py:35` — `ssdp_discover()`
- `draai/backends.py:73` — `refresh_speakers()`
- `draai/backends.py:106` — `get_zone_groups()`
- `draai/backends.py:125-146` — member/satellite/`fixed` parsing
- `draai/backends.py:172-181` — `AVT`/`RC`/`GRC` service constants
- `draai/backends.py:184` — `soap_call()`
- `draai/backends.py:214` — `avt()`
- `draai/backends.py:220` — `didl_for()` (cdudn desc at `:230-232`)
- `draai/backends.py:240` — `speaker_by_uuid()`
- `draai/backends.py:248` — `play_tracks()`
- `draai/backends.py:338` — `note_position()`
- `draai/backends.py:349` — `get_status()` (`TrackURI` parse at `:378-382`)
- `draai/backends.py:406` — `set_volume()`
- `draai/backends.py:447` — `browse_queue()`
- `draai/backends.py:475` — `queue_move()`
- `draai/backends.py:490` — `queue_jump()`
- `draai/backends.py:500` — `queue_remove()`
- `draai/backends.py:507` — `enqueue_tracks()`
- `draai/backends.py:584` / `:601` — `get_eq()` / `set_eq()`
- `draai/backends.py:626` — `zone_by_uuid()`
- `draai/backends.py:637` / `:651` — `group_join()` / `group_leave()`
- `draai/backends.py:663` — `get_rooms()`
- `draai/backends.py:680` — `set_room_volume()`
- `draai/library.py:315` — track id derivation (`sha1(path)[:16]`)
- `draai/media.py:20-23` — `media_url()`
- `draai/constants.py:15,23-25` — `QUEUE_CAP`, `POSITIONS_PATH`,
  `RESUME_MIN_TRACK`, `RESUME_MIN_POS`
- `draai/__main__.py:121-129` — startup discovery retry (`warmup()`)
- `draai/server.py:327-337` — `/api/state` handler
- `draai/server.py:498`, `:509` — `/api/rescan_speakers`, `/api/add_ip`
- `draai/player_ui.html:1185-1204` — `boot()` and its own retry loop
- `draai/player_ui.html:788` — `startPolling()`
- `tests/test_draai.py:96` — `SoapMock`
- `tests/test_draai.py:117-124` — `ReorderTracksInQueue` mock semantics
- `tests/test_draai.py:213` — `test_zone_groups_members_and_fixed`
- `tests/test_draai.py:229` — `test_play_sends_didl_with_desc`
- `tests/test_draai.py:314-323` — `test_queue_move_semantics`
