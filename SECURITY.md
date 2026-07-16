# Security policy

## Supported versions

DRAAI ships as a single rolling application; fixes land on `main` and in the
next tagged release. Please report against the **latest release** or `main`.

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue:

- Use GitHub's **[Report a vulnerability](https://github.com/itsmesaskovic/draai/security/advisories/new)**
  (the repository's *Security* tab → *Report a vulnerability*). This opens a
  private advisory visible only to the maintainer.

Include what you found, how to reproduce it, and the impact. You'll get an
acknowledgement, and a fix or an explanation of why it's out of scope. Please
give a reasonable window to address it before any public disclosure.

## Scope — what counts

DRAAI's trust boundary is **your local network** (see
[docs/technical/security-and-privacy.md](docs/technical/security-and-privacy.md)),
so a few things are known design boundaries rather than vulnerabilities:

- **No authentication on the API / phone remote / media.** Anyone on your LAN
  can control playback and browse your library — by design, on a trusted home
  network. Reports that assume an untrusted LAN are a *feature request* for
  optional access control, not a vulnerability.
- **Media is served over plain HTTP on the LAN**, because Sonos requires it.
  LAN-only, never the internet.

**In scope** and genuinely valued:

- Any path by which data leaves your machine to somewhere other than your own
  speakers or your own yt-dlp — this would break the core promise (see the
  egress inventory in the doc above).
- Any way a request reaching the engine escapes the media roots (path
  traversal), crashes it, or executes unintended commands.
- A third-party package sneaking into the import graph, or an external resource
  into the served UI — both are guarded by tests, but a bypass is in scope.

## What we do to prevent data leaks

The "nothing leaves your network" property is enforced, not just intended:

- The engine is pure Python standard library — **no pip dependencies, ever** —
  so there's no supply chain to compromise.
- `tests/test_draai.py` includes `test_engine_imports_only_stdlib` (fails the
  build on any non-stdlib import) and `test_ui_loads_nothing_from_the_internet`
  (fails on any external asset or non-local `fetch()` in the UI). Both run in CI
  on every push and pull request.
