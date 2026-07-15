# Contributing to DRAAI

Thanks for wanting to make DRAAI better. It's a small project with a few
strong opinions — reading this first will save you time.

## The short version

1. **Found a bug or have an idea?** Open an issue first, especially for
   features. A two-line issue can save you an evening of building something
   that doesn't fit the project.
2. **Fork** the repository, create a branch, make your change.
3. **Run the tests** — they need no speakers, network, or ffmpeg:

   ```
   python3 tests/test_draai.py
   ```

4. **Open a pull request.** The test suite runs automatically on your PR.
   Describe what the change does and, for anything speaker-related, which
   hardware you tested on (e.g. "Sonos One, S2 firmware" or "Symfonisk
   bookshelf pair").

## Project principles (the strong opinions)

- **The engine stays a single file.** `sonos_player.py` being one
  dependency-free file IS the product's distribution story: download one
  file, run it. Please don't split it into modules or add third-party
  imports. Optional tools (ffmpeg, yt-dlp) may be *detected*, never
  required or bundled.
- **The interface stays a single file** (`player_ui.html`): all CSS/JS
  inline, no CDNs, no web fonts, no localStorage. It must work with the
  internet unplugged.
- **No cloud, no accounts, no telemetry.** Everything happens on the
  user's own network. This is non-negotiable.
- **No site-specific downloader code.** The yt-dlp integration hands a URL
  to a tool the user installed themselves, and that's as far as it goes.
- **Friendly errors.** Anything a user can see should be a human sentence,
  not a stack trace.

## Good first contributions

Bug reports with the terminal output included; support quirks for speaker
models we haven't met (include your model and what the Sonos app shows);
translations of the interface; a Chromecast backend (planned — open an
issue to coordinate before starting); documentation improvements.

## Testing against real speakers

The automated tests mock the Sonos protocol. If your change touches
playback, grouping, or discovery, please also test against at least one
real speaker and say so in the PR. If you don't own one, mark the PR as
untested-on-hardware and we'll find someone who does.

## Code style

Match what's there: Python standard library only, ~79-column-ish lines,
section banners, comments that explain *why*. For the interface: vanilla
JS, no frameworks, no build step.
