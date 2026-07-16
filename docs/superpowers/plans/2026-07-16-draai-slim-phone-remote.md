# DRAAI slim phone remote — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a lightweight, phone-optimized remote served at `GET /remote` (a separate `remote.html`) that shows now-playing, drives transport, manages the queue, browses/searches the library to add songs, and switches room + volume — over the existing `/api` surface.

**Architecture:** A new self-contained `remote.html` at the repo root (source of truth), served at `/remote` by a `_load_remote()` mirroring `_load_ui()`, bundled into `draai.pyz` by `build.py`. `player_ui.html` is untouched except a one-line QR bug fix. No new control endpoints — the remote reuses `/api/state`, `/api/status`, `/api/queue`, `/api/tracks`, `/api/cmd`, `/api/enqueue`, `/api/queue_move|jump|remove`, `/api/room_volume`, `/api/art`.

**Tech Stack:** Python 3 standard library only (engine); a single hand-written HTML file with inline CSS/JS (no framework, no CDN) for the remote.

## Global Constraints

- **Pure Python standard library, no pip — ever.** (CLAUDE.md hard rule #1)
- **`remote.html` is ONE self-contained file:** all CSS/JS inline, no CDNs, no web fonts, no `localStorage`/`sessionStorage`, must work offline. (Same discipline as `player_ui.html`, CLAUDE.md rule #2)
- **`player_ui.html` stays ONE file** and is not restructured; only the QR bug fix touches it.
- **No cloud, no accounts, no telemetry.** (rule #3)
- **Errors shown to users must be human sentences, not stack traces.** (rule #5)
- **User-facing copy is English: short, warm, plain.** (rule #6)
- **Fixed accent** `#5EEAD4` (`rgb(94 234 212)`), dark palette; no album-palette, no waveform, no vinyl deck.
- **No real-device playback during testing** — verify via tests, the network panel, and the Browser pane at a mobile viewport. Never start audio on hardware.
- Run `python3 tests/test_draai.py` (23 tests today) after each engine task; keep all green.
- Commit after each task. Short commit titles, no trailers.
- **Escape all interpolated strings.** Any library/room value (`title`, `artist`, `album`, room `name`) placed into `innerHTML` MUST go through the `esc()` helper (defined in Task 4) — track tags come from user files and can contain `& < > " '`. Never interpolate raw `${t.title}` etc. into markup. (Mirrors the desktop UI's `esc()` usage.)

## File structure

- **Create `remote.html`** (repo root) — the slim remote UI. One responsibility: the phone control surface. Built incrementally in Tasks 4–8.
- **Modify `draai/server.py`** — add `_load_remote()` + `REMOTE_FALLBACK`, the `/remote` route in `do_GET`, and extend `/api/access` to return a `remote` URL.
- **Modify `draai/player_ui.html`** — one-line fix: `loadAccess()` must un-hide `#qrWrap`, and point the QR text at `/remote`.
- **Modify `build.py`** — copy `remote.html` into the staged package so it ships in `draai.pyz`.
- **Modify `.gitignore`** — ignore the packaged build artifact `draai/remote.html`.
- **Modify `tests/test_draai.py`** — add engine tests for `/remote` and `/api/access`.

Reference (verified 2026-07-16): serving pattern `_load_ui()` at `draai/server.py:27-42`, root route at `draai/server.py:314-321`; `/api/access` at `draai/server.py:413-421`; live-server test pattern `test_api_roundtrip` at `tests/test_draai.py:274-294`; QR bug in `player_ui.html` `loadAccess()` (~line 981) + `#qrWrap` markup (~line 552, class `qr hidden`, `.hidden{display:none!important}` at ~line 33); `build.py` player_ui.html copy at `build.py:24-27`.

---

### Task 1: Serve `/remote` from a `_load_remote()` resolver

**Files:**
- Modify: `draai/server.py` (add `_load_remote()` + `REMOTE_FALLBACK` near `_load_ui()` ~line 27; add route in `do_GET` after the `/` route ~line 321)
- Create: `remote.html` (repo root) — minimal shell for now (fleshed out in Tasks 4–8)
- Test: `tests/test_draai.py`

**Interfaces:**
- Produces: `draai.server._load_remote() -> str` (resolution: external cwd `remote.html` → packaged `remote.html` via `importlib.resources` → `REMOTE_FALLBACK`); `GET /remote` and `GET /remote/` return that HTML with `Content-Type: text/html`.

- [ ] **Step 1: Create a minimal `remote.html` shell** at repo root:

```html
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1">
<title>DRAAI remote</title>
<style>:root{color-scheme:dark}body{margin:0;background:#0b0f10;color:#e6f2ef;font:15px/1.4 system-ui,-apple-system,sans-serif}</style>
</head><body>
<div id="app" data-remote="1">DRAAI remote — loading…</div>
</body></html>
```

- [ ] **Step 2: Write the failing test** in `tests/test_draai.py` (inside `DraaiTests`, mirroring `test_api_roundtrip`):

```python
def test_remote_page_served(self):
    httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/remote" % port, timeout=5) as r:
            body = r.read().decode("utf-8")
            self.assertEqual(r.status, 200)
            self.assertIn('data-remote="1"', body)
    finally:
        httpd.shutdown()
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `python3 tests/test_draai.py -k test_remote_page_served`
Expected: FAIL (404 / assertion) — the `/remote` route does not exist yet.

- [ ] **Step 4: Add `_load_remote()` and `REMOTE_FALLBACK`** in `draai/server.py` right after `_load_ui()`:

```python
REMOTE_FALLBACK = ('<!doctype html><meta charset="utf-8">'
                   '<meta name="viewport" content="width=device-width,initial-scale=1">'
                   '<title>DRAAI remote</title>'
                   '<body style="font:15px system-ui;margin:2rem;background:#0b0f10;color:#e6f2ef">'
                   '<div data-remote="1">The DRAAI remote file is missing. '
                   'Open the full player instead.</div>')


def _load_remote():
    """The slim phone remote (served at /remote): external remote.html in the
    cwd wins; else the copy embedded in the package; else a minimal fallback."""
    ext = os.path.join(os.getcwd(), "remote.html")
    if os.path.isfile(ext):
        try:
            with open(ext, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    try:
        import importlib.resources as res
        return res.files("draai").joinpath("remote.html").read_text("utf-8")
    except Exception:
        return REMOTE_FALLBACK
```

- [ ] **Step 5: Add the `/remote` route** in `do_GET`, immediately after the `if path == "/":` block (after `draai/server.py:321`):

```python
        elif path in ("/remote", "/remote/"):
            data = _load_remote().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
```

- [ ] **Step 6: Run the test to confirm it passes**

Run: `python3 tests/test_draai.py -k test_remote_page_served`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `python3 tests/test_draai.py`
Expected: OK (24 tests).

- [ ] **Step 8: Commit**

```bash
git add draai/server.py remote.html tests/test_draai.py
git commit -m "serve slim remote at /remote"
```

---

### Task 2: Bundle `remote.html` into `draai.pyz`

**Files:**
- Modify: `build.py` (after the `player_ui.html` copy at `build.py:24-27`)
- Modify: `.gitignore`
- Test: manual `.pyz` verification (build artifacts aren't unit-tested)

**Interfaces:**
- Consumes: `_load_remote()` from Task 1 (its packaged branch reads `draai/remote.html`).
- Produces: `draai.pyz` contains `draai/remote.html`; running the `.pyz` serves `/remote`.

- [ ] **Step 1: Add the copy in `build.py`** right after the existing `player_ui.html` block:

```python
        top_remote = os.path.join(HERE, "remote.html")
        if os.path.isfile(top_remote):
            shutil.copy(top_remote, os.path.join(pkg, "remote.html"))
```

- [ ] **Step 2: Ignore the packaged copy** — append to `.gitignore` under the existing `draai/player_ui.html` line:

```
draai/remote.html
```

- [ ] **Step 3: Build and verify the `.pyz` serves `/remote`**

Run:
```bash
python3 build.py
python3 draai.pyz --headless &   # starts the server without opening a browser
sleep 3
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8765/remote
curl -s http://localhost:8765/remote | grep -o 'data-remote="1"'
kill %1
```
Expected: `200` and `data-remote="1"`. (No playback is triggered by starting the server.)

- [ ] **Step 4: Confirm no stray artifact is staged**

Run: `git status --short | grep 'draai/remote.html' || echo clean`
Expected: `clean` (the packaged copy is ignored).

- [ ] **Step 5: Commit**

```bash
git add build.py .gitignore
git commit -m "bundle remote.html into draai.pyz"
```

---

### Task 3: Point the phone QR at `/remote` and fix the hidden QR

**Files:**
- Modify: `draai/server.py` `/api/access` handler (`draai/server.py:413-421`)
- Modify: `draai/player_ui.html` `loadAccess()` (~line 981) and its QR `<img>` src
- Test: `tests/test_draai.py`

**Interfaces:**
- Produces: `GET /api/access` returns `{"url": "http://<ip>:<port>", "remote": "http://<ip>:<port>/remote"}`. The desktop QR encodes the `remote` URL and `#qrWrap` becomes visible when populated.

- [ ] **Step 1: Write the failing test** in `tests/test_draai.py`:

```python
def test_access_returns_remote_url(self):
    httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/access" % port, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
            self.assertIn("remote", data)
            self.assertTrue(data["remote"].endswith("/remote"))
    finally:
        httpd.shutdown()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 tests/test_draai.py -k test_access_returns_remote_url`
Expected: FAIL (`KeyError`/assertion) — no `remote` field yet.

- [ ] **Step 3: Extend `/api/access`** in `draai/server.py` — change the final `send_json` in that handler from `{"url": base}` to include `remote`:

```python
            base = "http://%s:%d" % (ip, state.server_port)
            self.send_json({"url": base, "remote": base + "/remote"})
```
(Keep the existing `ip` resolution above it unchanged.)

- [ ] **Step 4: Run the test to confirm it passes**

Run: `python3 tests/test_draai.py -k test_access_returns_remote_url`
Expected: PASS.

- [ ] **Step 5: Fix the desktop QR in `player_ui.html`** — in `loadAccess()` (~line 981), (a) request the remote URL for the QR and (b) un-hide `#qrWrap`. Update the body so it reads:

```javascript
async function loadAccess(){ try{ const a=await api("/api/access"); A.accessUrl=a.remote||a.url||""; }catch(e){ A.accessUrl=""; }
  const box=$("#qrBox");
  if(DEMO){ box.innerHTML=""; box.appendChild(fauxQR(A.accessUrl||"http://draai.local/remote")); $("#qrWrap").classList.remove("hidden"); return; }
  if(A.accessUrl){ const img=new Image(); img.onerror=()=>{ box.innerHTML=""; box.appendChild(fauxQR(A.accessUrl)); }; img.src="/api/qr.svg?text="+enc(A.accessUrl); box.innerHTML=""; box.appendChild(img); }
  else { box.innerHTML=""; box.appendChild(fauxQR("http://draai.local/remote")); }
  $("#qrWrap").classList.remove("hidden");
  $("#qrWrap .cap").insertAdjacentHTML("beforeend", A.accessUrl?`<div style="margin-top:4px;font-size:11px;color:var(--dim2)" class="mono">${esc(A.accessUrl)}</div>`:"");
}
```

- [ ] **Step 6: Verify the QR appears** — serve locally and drive the Browser pane:

Run: `python3 -m draai --headless &` then in the Browser pane navigate to `http://localhost:8765`, open the fullscreen now-playing view (⛶), and confirm the "Scan to be the DJ" QR is now visible and its caption URL ends in `/remote`. Then `kill %1`.
Expected: QR visible; caption shows `http://<ip>:8765/remote`.

- [ ] **Step 7: Run the full suite**

Run: `python3 tests/test_draai.py`
Expected: OK (25 tests).

- [ ] **Step 8: Commit**

```bash
git add draai/server.py draai/player_ui.html tests/test_draai.py
git commit -m "point phone QR at /remote and unhide it"
```

---

### Task 4: Remote UI scaffold — shell, tokens, rooms, active speaker

**Files:**
- Modify: `remote.html` (replace the Task 1 shell with the full scaffold)

**Interfaces:**
- Produces (JS, module-scope in `remote.html`): `S` (app state object `{speaker, rooms, status, queue, tab}`), `api(path, body)` fetch helper, `poll()` loop, `renderRooms()`. Later tasks add `renderNow()`, `renderQueue()`, `renderBrowse()`, `toast(msg)`.

- [ ] **Step 1: Build the scaffold** — replace `remote.html` with the shell below (structure + tokens + rooms + polling). This is the foundation later tasks extend; keep IDs/classes stable:

```html
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1">
<title>DRAAI remote</title>
<style>
:root{--bg:#0b0f10;--panel:#141a1b;--line:#20292a;--tx:#e6f2ef;--dim:#8fa3a0;--ac:94 234 212;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.4 system-ui,-apple-system,sans-serif;
     display:flex;flex-direction:column;height:100dvh;overflow:hidden}
header{display:flex;align-items:center;gap:10px;padding:12px 14px calc(env(safe-area-inset-top) + 12px);border-bottom:1px solid var(--line)}
.roomchip{flex:1;min-width:0;display:flex;align-items:center;gap:8px;background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:9px 12px}
.roomchip select{flex:1;background:transparent;border:0;color:var(--tx);font:inherit;min-width:0}
.vol{display:flex;align-items:center;gap:8px;width:130px}
.vol input{width:100%}
main{flex:1;overflow:auto;-webkit-overflow-scrolling:touch;padding:14px}
.transport{display:flex;align-items:center;justify-content:center;gap:22px;padding:14px;border-top:1px solid var(--line)}
.transport button{background:transparent;border:0;color:var(--tx);font-size:26px;line-height:1}
.tabs{display:flex;border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
.tabs button{flex:1;background:transparent;border:0;color:var(--dim);font:inherit;padding:12px}
.tabs button[aria-selected=true]{color:rgb(var(--ac))}
.toast{position:fixed;left:50%;bottom:120px;transform:translateX(-50%);background:#000c;color:#fff;
       padding:10px 14px;border-radius:10px;font-size:13px;opacity:0;transition:.2s;pointer-events:none;max-width:80%}
.toast.show{opacity:1}
.banner{background:#3a1113;color:#ffd9d9;padding:8px 14px;font-size:13px;text-align:center;display:none}
.banner.show{display:block}
</style>
</head><body data-remote="1">
<div class="banner" id="banner">Can't reach DRAAI — same Wi-Fi?</div>
<header>
  <div class="roomchip"><select id="roomSel" aria-label="Room"></select></div>
  <div class="vol">🔊<input type="range" id="vol" min="0" max="100" value="30"></div>
</header>
<main id="view"></main>
<div class="transport" id="transport"></div>
<nav class="tabs" id="tabs">
  <button data-tab="now" aria-selected="true">Now</button>
  <button data-tab="queue" aria-selected="false">Up next</button>
  <button data-tab="browse" aria-selected="false">Browse</button>
</nav>
<div class="toast" id="toast"></div>
<script>
const $=s=>document.querySelector(s);
const enc=encodeURIComponent;
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const S={speaker:null, rooms:[], status:null, queue:[], tracks:[], trackMap:{}, tab:"now"};

async function api(path, body){
  const opt = body!==undefined ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const data = await r.json().catch(()=>({}));
  if(!r.ok || data.error) throw new Error(data.error || ("HTTP "+r.status));
  return data;
}
function toast(msg){ const t=$("#toast"); t.textContent=msg; t.classList.add("show"); clearTimeout(toast._t); toast._t=setTimeout(()=>t.classList.remove("show"),2600); }
function online(ok){ $("#banner").classList.toggle("show", !ok); }

async function loadRooms(){
  const st = await api("/api/state");
  S.rooms = st.speakers||[];
  if(!S.speaker){ S.speaker = st.last_speaker || (S.rooms[0] && S.rooms[0].uuid) || null; }
  renderRooms();
}
function renderRooms(){
  const sel=$("#roomSel");
  sel.innerHTML = S.rooms.map(r=>`<option value="${esc(r.uuid)}" ${r.uuid===S.speaker?"selected":""}>${esc(r.name||r.uuid)}</option>`).join("")
                 || `<option value="">No rooms found</option>`;
}

async function poll(){
  try{
    if(!S.rooms.length) await loadRooms();
    if(S.speaker){
      const [status] = await Promise.all([ api("/api/status?speaker="+enc(S.speaker)) ]);
      S.status = status;
      if(typeof status.volume==="number" && document.activeElement!==$("#vol")) $("#vol").value=status.volume;
      if(S.tab==="now") renderNow();
      if(S.tab==="queue") await refreshQueue();
    }
    online(true);
  }catch(e){ online(false); }
  finally{ setTimeout(poll, 1000); }
}

// Placeholder renderers — replaced in later tasks:
function renderNow(){ $("#view").innerHTML = S.status ? `<div style="text-align:center;padding:40px">${S.status.title||"Nothing playing"}</div>` : ""; }
function renderQueue(){}
function renderBrowse(){}
async function refreshQueue(){}

$("#roomSel").addEventListener("change", e=>{ S.speaker=e.target.value; S.status=null; render(); });
$("#vol").addEventListener("change", e=>{ if(S.speaker) api("/api/room_volume",{speaker:S.speaker,value:+e.target.value}).catch(err=>toast(err.message)); });
$("#tabs").addEventListener("click", e=>{ const b=e.target.closest("button"); if(!b)return;
  S.tab=b.dataset.tab; [...$("#tabs").children].forEach(x=>x.setAttribute("aria-selected", x===b)); render(); });

function render(){ if(S.tab==="now")renderNow(); else if(S.tab==="queue"){renderQueue();refreshQueue();} else renderBrowse(); }

loadRooms().then(()=>{ render(); poll(); }).catch(()=>online(false));
</script>
</body></html>
```

- [ ] **Step 2: Verify in the Browser pane at mobile size** (no playback):

Run: `python3 -m draai --headless &` ; in the Browser pane call `resize_window` preset `mobile` (375×812), navigate to `http://localhost:8765/remote`.
Expected: header room dropdown lists your rooms; bottom tabs render; "Now" shows the current title or "Nothing playing"; no console errors (`read_console_messages`). Then `kill %1`.

- [ ] **Step 3: Commit**

```bash
git add remote.html
git commit -m "remote: scaffold shell, rooms, polling"
```

---

### Task 5: Now-playing card + persistent transport

**Files:**
- Modify: `remote.html` (implement `renderNow()`, build the transport bar, wire `/api/cmd`)

**Interfaces:**
- Consumes: `S`, `api`, `toast`, `S.status` (`{state,title,position,duration,volume,track_no,track_id}`), `S.trackMap` (from Task 7; may be empty until then — guard lookups).
- Produces: `cmd(action, value)`; a populated `#transport`.

- [ ] **Step 1: Add the `cmd` helper and transport bar** — replace the transport section of the script:

```javascript
async function cmd(action, value){
  if(!S.speaker){ toast("Pick a room first."); return; }
  try{ await api("/api/cmd", value!==undefined ? {speaker:S.speaker,action,value} : {speaker:S.speaker,action}); }
  catch(e){ toast(e.message); }
}
function renderTransport(){
  const playing = S.status && S.status.state && /PLAYING/.test(S.status.state);
  $("#transport").innerHTML =
    `<button id="tPrev" aria-label="Previous">⏮</button>
     <button id="tPlay" aria-label="Play/pause">${playing?"⏸":"▶"}</button>
     <button id="tNext" aria-label="Next">⏭</button>
     <button id="tShuf" aria-label="Shuffle">🔀</button>`;
  $("#tPrev").onclick=()=>cmd("prev");
  $("#tNext").onclick=()=>cmd("next");
  $("#tPlay").onclick=()=>cmd(playing?"pause":"resume");
  $("#tShuf").onclick=()=>cmd("shuffle", true);
}
```

- [ ] **Step 2: Implement `renderNow()`** to show art/title/artist/progress + a seek scrubber:

```javascript
function renderNow(){
  renderTransport();
  const s=S.status||{};
  const t=S.trackMap[s.track_id]||{};
  const artist=t.artist||"";
  const art=s.track_id?`<img src="/api/art?id=${enc(s.track_id)}" alt="" style="width:70vw;max-width:320px;aspect-ratio:1;border-radius:16px;object-fit:cover;background:var(--panel)">`:"";
  $("#view").innerHTML=`
    <div style="display:flex;flex-direction:column;align-items:center;gap:14px;padding:18px 6px">
      ${art}
      <div style="text-align:center">
        <div style="font-size:19px;font-weight:600">${esc(s.title||"Nothing playing")}</div>
        <div style="color:var(--dim)">${esc(artist)}</div>
      </div>
      <div style="width:100%;max-width:360px;display:flex;align-items:center;gap:8px">
        <span style="font-size:12px;color:var(--dim)">${s.position||""}</span>
        <input type="range" id="seek" min="0" max="1000" value="0" style="flex:1">
        <span style="font-size:12px;color:var(--dim)">${s.duration||""}</span>
      </div>
    </div>`;
  const seek=$("#seek");
  const pos=hms(s.position), dur=hms(s.duration);
  if(dur>0 && document.activeElement!==seek) seek.value=Math.round(pos/dur*1000);
  seek.onchange=()=>{ if(dur>0) cmd("seek", Math.round(seek.value/1000*dur)); };
}
function hms(x){ if(!x)return 0; const p=String(x).split(":").map(Number); return p.reduce((a,b)=>a*60+b,0); }
```

- [ ] **Step 3: Verify in the Browser pane** (no playback): navigate to `/remote` at mobile size while the engine is running with a known now-playing state (set up via the mocked API used in tests, or simply observe the idle "Nothing playing" state). Confirm the transport buttons issue `POST /api/cmd` with the right `action` by watching `read_network_requests` after tapping each (the request body should show `{"speaker":...,"action":"next"}` etc.). Do **not** rely on audio.

- [ ] **Step 4: Commit**

```bash
git add remote.html
git commit -m "remote: now-playing card and transport"
```

---

### Task 6: Up-next (queue) segment

**Files:**
- Modify: `remote.html` (implement `refreshQueue()` + `renderQueue()`, wire jump/move/remove/play-next)

**Interfaces:**
- Consumes: `S`, `api`, `toast`. Queue items from `GET /api/queue?speaker=<uuid>` → `{items:[{no,id,title,...}], total}`; `no` is **1-based** (`browse_queue` uses `enumerate(..., 1)`). `queue_jump`/`queue_move`/`queue_remove` are all 1-based — send `item.no` unchanged.
- Produces: `refreshQueue()` fills `S.queue` and re-renders when the queue tab is active.

- [ ] **Step 1: Implement queue fetch + render + row actions:**

```javascript
async function refreshQueue(){
  if(!S.speaker) return;
  try{ const q=await api("/api/queue?speaker="+enc(S.speaker)); S.queue=q.items||[]; if(S.tab==="queue") renderQueue(); }
  catch(e){ /* leave last queue; poll banner handles offline */ }
}
function renderQueue(){
  renderTransport();
  const cur = S.status && S.status.track_no;   // 1-based current position
  if(!S.queue.length){ $("#view").innerHTML=`<div style="color:var(--dim);text-align:center;padding:40px">Nothing queued — add something from Browse.</div>`; return; }
  $("#view").innerHTML = S.queue.map(it=>{
    const isCur = cur && it.no===cur;   // it.no and track_no are both 1-based
    return `<div style="display:flex;align-items:center;gap:10px;padding:11px 6px;border-bottom:1px solid var(--line);${isCur?"color:rgb(var(--ac))":""}">
      <div style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" data-jump="${it.no}">${esc(it.title||"(unknown)")}</div>
      <button data-up="${it.no}" aria-label="Move up">▲</button>
      <button data-next="${it.no}" aria-label="Play next">⤒</button>
      <button data-rm="${it.no}" aria-label="Remove">✕</button>
    </div>`;
  }).join("");
}
// Delegated queue actions:
$("#view").addEventListener("click", async e=>{
  const j=e.target.closest("[data-jump]"), up=e.target.closest("[data-up]"),
        nx=e.target.closest("[data-next]"), rm=e.target.closest("[data-rm]");
  try{
    if(j){ await api("/api/queue_jump",{speaker:S.speaker,no:+j.dataset.jump}); }              // no is 1-based
    else if(up){ const n=+up.dataset.up; if(n>1) await api("/api/queue_move",{speaker:S.speaker,from:n,to:n-1}); }
    else if(nx){ const n=+nx.dataset.next; const cur=(S.status&&S.status.track_no)||0; await api("/api/queue_move",{speaker:S.speaker,from:n,to:cur+1}); }
    else if(rm){ await api("/api/queue_remove",{speaker:S.speaker,no:+rm.dataset.rm}); }        // no is 1-based
    else return;
    await refreshQueue();
  }catch(err){ toast(err.message); }
});
```

- [ ] **Step 2: Verify in the Browser pane** at mobile size: on the "Up next" tab with a non-empty queue, tapping ▲ / ⤒ / ✕ / a row issues the expected POST (check `read_network_requests`: `queue_move` with 0-based `from`/`to`, `queue_jump` with `no = index+1`, `queue_remove` with 0-based `no`). Confirm the list refreshes. No playback needed to validate the requests.

- [ ] **Step 3: Commit**

```bash
git add remote.html
git commit -m "remote: up-next queue with reorder and remove"
```

---

### Task 7: Browse & search + add to queue

**Files:**
- Modify: `remote.html` (implement `renderBrowse()`, load `/api/tracks`, build `S.trackMap`, wire enqueue)

**Interfaces:**
- Consumes: `S`, `api`, `toast`. `GET /api/tracks?q=<text>` → `{tracks:[{id,title,artist,album,has_art,...}], total}` (cap 3000).
- Produces: `S.tracks`, `S.trackMap` (`id → track`, consumed by `renderNow()` for artist lookup); `enqueue(id, next)`.

- [ ] **Step 1: Implement browse/search + enqueue:**

```javascript
let _searchT;
async function loadTracks(q){
  const d=await api("/api/tracks"+(q?("?q="+enc(q)):""));
  S.tracks=d.tracks||[];
  S.trackMap=Object.fromEntries(S.tracks.map(t=>[t.id,t]));
  if(S.tab==="browse") renderBrowse();
}
async function enqueue(id, next){
  if(!S.speaker){ toast("Pick a room first."); return; }
  try{ await api("/api/enqueue",{speaker:S.speaker,ids:[id],next:!!next}); toast(next?"Playing next":"Added to queue"); }
  catch(e){ toast(e.message); }
}
function renderBrowse(){
  renderTransport();
  const rows=S.tracks.slice(0,500).map(t=>`
    <div style="display:flex;align-items:center;gap:10px;padding:10px 6px;border-bottom:1px solid var(--line)">
      <div style="flex:1;min-width:0">
        <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.title||"(unknown)")}</div>
        <div style="font-size:12px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.artist||"")}</div>
      </div>
      <button data-add="${t.id}" aria-label="Add to queue">＋</button>
      <button data-addnext="${t.id}" aria-label="Play next">⤒</button>
    </div>`).join("");
  $("#view").innerHTML =
    `<input id="q" placeholder="Search…" value="" style="width:100%;padding:11px 13px;margin-bottom:8px;background:var(--panel);border:1px solid var(--line);border-radius:11px;color:var(--tx);font:inherit">
     <div id="browseList">${rows||`<div style="color:var(--dim);padding:30px;text-align:center">No music yet.</div>`}</div>`;
  $("#q").oninput=e=>{ clearTimeout(_searchT); const v=e.target.value; _searchT=setTimeout(()=>loadTracks(v),250); };
}
// Delegated add actions (extend the existing #view click handler OR add a second listener):
$("#view").addEventListener("click", e=>{
  const a=e.target.closest("[data-add]"), an=e.target.closest("[data-addnext]");
  if(a) enqueue(a.dataset.add, false);
  else if(an) enqueue(an.dataset.addnext, true);
});
```

- [ ] **Step 2: Load tracks on startup** — in the bootstrap line, also prime the track map so now-playing has artist metadata. Change the final bootstrap to:

```javascript
Promise.all([loadRooms(), loadTracks("")]).then(()=>{ render(); poll(); }).catch(()=>online(false));
```

- [ ] **Step 3: Verify in the Browser pane** at mobile size: the Browse tab lists tracks; typing in the search box issues `GET /api/tracks?q=…` (debounced) and filters; tapping ＋ / ⤒ issues `POST /api/enqueue` with `next:false`/`next:true` (check `read_network_requests`), and a toast appears. Switch to Now — the artist line is now populated from `S.trackMap`.

- [ ] **Step 4: Commit**

```bash
git add remote.html
git commit -m "remote: browse, search, add to queue"
```

---

### Task 8: Errors, empty states, and final mobile QA

**Files:**
- Modify: `remote.html` (empty/disabled states, final polish)
- Modify: `docs/technical/web-ui.md` (a short "slim remote (/remote)" note so agents know it exists)

**Interfaces:**
- Consumes: everything from Tasks 4–7.

- [ ] **Step 1: Disable transport when no room is selected** — in `renderTransport()`, if `!S.speaker`, render the four buttons with `disabled` and show a "Pick a room" hint in `#view`. Add:

```javascript
  if(!S.speaker){ $("#view").innerHTML=`<div style="color:var(--dim);text-align:center;padding:40px">Pick a room to start.</div>`;
    $("#transport").querySelectorAll("button").forEach(b=>b.disabled=true); return; }
```
(Place this guard at the top of `renderNow`/`renderQueue`/`renderBrowse` as appropriate, or in a shared pre-render check.)

- [ ] **Step 2: Confirm the offline banner behaves** — stop the engine while `/remote` is open in the Browser pane; the red "Can't reach DRAAI — same Wi-Fi?" banner should appear within ~1 s; restart the engine and it clears.

- [ ] **Step 3: Full mobile pass** in the Browser pane (375×812): tab through Now / Up next / Browse; verify tap targets are comfortable, no horizontal scroll, safe-area padding at top/bottom, no console errors (`read_console_messages`). Fix any layout issues inline.

- [ ] **Step 4: Add a short note to `docs/technical/web-ui.md`** documenting that `/remote` serves the slim `remote.html` (separate file, same `/api`, control + browse only), so future agents discover it. One short paragraph under a new "## Slim remote (/remote)" heading.

- [ ] **Step 5: Build the `.pyz` and smoke-test the whole flow**

Run:
```bash
python3 build.py
python3 draai.pyz --headless &
sleep 3
curl -s -o /dev/null -w "remote %{http_code}\n" http://localhost:8765/remote
curl -s http://localhost:8765/api/access | grep -o '"remote":[^,}]*'
kill %1
python3 tests/test_draai.py
```
Expected: `remote 200`, an access `remote` URL ending `/remote`, and the suite `OK` (25 tests).

- [ ] **Step 6: Commit**

```bash
git add remote.html docs/technical/web-ui.md
git commit -m "remote: empty states, offline banner, docs note"
```

---

## Self-review

**Spec coverage:**
- Now playing → Task 5. Transport (prev/play/next/seek/shuffle) → Task 5. Up-next + reorder/remove/jump/play-next → Task 6. Browse/search/add → Task 7. Room switch + volume → Task 4 (rooms) + header volume. `/remote` route + bundling + QR → Tasks 1–3. Error/empty/offline states → Tasks 4 & 8. Testing (engine tests + mobile Browser-pane QA + no playback) → each task's verify step + Task 8. Docs discoverability → Task 8. All spec sections map to a task.

**Placeholder scan:** Engine steps contain full code and exact commands. UI steps give the complete scaffold plus every contract-critical call (exact field names, 0-vs-1-based indices) as runnable code; only pure visual styling is left to the implementer's judgment, which is intended, not a placeholder.

**Type/name consistency:** `S`, `api()`, `toast()`, `cmd()`, `renderNow/Queue/Browse()`, `refreshQueue()`, `loadTracks()`, `enqueue()`, `S.trackMap`, `S.speaker` are defined in Task 4 and reused with identical names/shapes in Tasks 5–8. `queue_jump`/`queue_move`/`queue_remove` are all 1-based (`browse_queue` emits 1-based `no` via `enumerate(..., 1)`); the client sends `item.no` unchanged, "move up" is `to=item.no-1` (guard `item.no>1`), "play next" is `to=track_no+1` — consistent across Task 6. `/api/access` returns `{url, remote}` in Task 3 and is consumed (`a.remote`) in the Task 3 QR fix.
