# kareokie-ext-youtube: a YouTube source for KAREOKIE

A free, community-made **music source extension** for [KAREOKIE](https://kareokie.pages.dev):
search YouTube and import videos you have the rights to use. KAREOKIE itself only imports local
files; online sources like this one are separate extensions you choose to install.

> For accessing content you have the rights to use. You must follow YouTube's Terms of Service.
> See [TERMS.md](TERMS.md).

## What it does

| Capability | |
|---|---|
| `search` | YouTube search without an API key (yt-dlp `ytsearch`, only the results page is fetched) |
| `resolve` | the best audio stream as-is + the uploader's subtitles (used as lyrics candidates) |
| `video` | the music video, video-only, up to the app's `videoQuality` height, AV1 > VP9 > H.264 (`video.mp4` / `video.webm`), downloaded while the app separates the audio |
| `metadata` | title / artist / duration of a URL without downloading |
| actions | `signin` / `signout`, offered on an error in the import panel (see "Sign-in window") |
| URLs | `youtube.com/watch?v=…`, `youtu.be/…`, `/shorts/`, `/embed/`, `/live/`, `music.youtube.com` |

Dependencies: [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense) and `yt-dlp-ejs`, installed by KAREOKIE
into this extension's own folder (`pip install --target`), never into the app. YouTube changes often, so the
extension keeps yt-dlp up to date by itself: imports check PyPI at most every 12 hours and install a newer
yt-dlp into the extension's data folder (`<dataDir>/yt-dlp/<version>/`); the newer of the two copies is used.
An error a newer yt-dlp may fix ("confirm you're not a bot", missing formats, HTTP 403) checks again sooner and
retries the import once. A "confirm you're not a bot" check is often a passing flag on one request, so a
blocked import also waits and tries again (after 5, 15 and 30 s; searches once, after 3 s) before it reports
the error. When Chrome or Edge is installed (or a sign-in is saved), a blocked import skips those waits and goes
on to the sign-in instead (see "Sign-in window"). Some blocks don't pass, so a video blocked through every retry is
remembered for 6 hours and importing it again reports at once instead of waiting (`<dataDir>/blocked.json`). With a
JavaScript runtime installed (deno, Node.js or Bun), yt-dlp solves YouTube's player challenges with it and finds
every format.

### macOS (Apple Silicon)

The extension runs in KAREOKIE's own Python on the Mac too; nothing is Windows-only.

- **JavaScript runtime.** An app started from the Finder doesn't see your shell's PATH, so the extension looks for
  deno, Node.js (22+) and Bun in Homebrew (`/opt/homebrew/bin`, `/usr/local/bin`), MacPorts, `~/.deno/bin`,
  `~/.bun/bin`, Volta, `~/.local/bin` and nvm, and uses the first one that runs and is new enough. With none, the
  first import downloads deno (version and SHA-256 pinned in `main.py`, about 40 MB) into
  `<dataDir>/deno/<version>/`, executable and without `com.apple.quarantine`. When that download fails it is
  tried again 30 minutes later; yt-dlp works without a runtime meanwhile, with fewer formats.
- **ffmpeg** isn't needed: audio and video are downloaded as-is.
- **Sign-in window.** Chrome or Edge from `/Applications` (or `~/Applications`), the same as on Windows.
- With an app that doesn't pass its language, error messages follow the first of the Mac's preferred languages.

### Sign-in window

> **Don't sign in with your main account.** Downloading with an account may make YouTube restrict or disable it.
> Use a separately created secondary account.

There's nothing to set up. When YouTube blocks an import without a sign-in and none is saved, the error in the
import panel comes with a button, **Sign in in a browser window, then try again** (「開啟網頁登入後再次嘗試」), and a
one-line hint to use a secondary account. The button calls the extension's `action("signin")`, which opens Chrome
(or Edge) with a profile of the extension's own (`<dataDir>/signin/profile`, started with `--user-data-dir` and
`--remote-debugging-port=0`; the user's usual profile is never touched). When it returns ok, the app retries the
import, which then goes through the account (see below).

1. **Warning first, every time.** The window opens on a full-window red warning page, served by the extension on
   `127.0.0.1` under a random path, in the app's language. Its Continue button stays disabled until the user ticks
   "I understand the risk, and I'm using a separately created secondary account" (the server checks it too). Only
   then does it go on to YouTube's sign-in page. Closing the window cancels the sign-in. Nothing is read from the
   browser before the warning is accepted.
2. **The user signs in** there themselves, including any Google verification.
3. The extension waits (up to 15 minutes, cancellable) until youtube.com has a signed-in session, reads that window's
   youtube.com and google.com cookies with the DevTools protocol's `Storage.getCookies` (a small standard-library
   WebSocket client, no extra dependency), seals them in `<dataDir>/signin/cookies.dat` and closes the window.

The sign-in is kept: yt-dlp downloads with it and saves the cookies YouTube refreshes back to it after every call
(through a copy that is sealed again in one step, so a call killed mid-write can't break it). On Windows the file is
sealed with Windows' data protection, so it can't be opened by another Windows user or on another PC; elsewhere it is a
file only this user can read. This doesn't stop other programs running as the same user, and yt-dlp needs a readable
copy for the length of a call, which is deleted afterwards. Use a secondary account either way. `dataDir`
survives app restarts and extension updates, so there's no need to sign in again. When YouTube stops accepting it,
the error says so and offers the button again (warning first); the kept profile is usually still signed in, so the
window just closes again with fresh cookies. Only `action("signout")` deletes the profile and the cookies. Cookie values are never logged or put in messages, and only yt-dlp's requests to YouTube / Google
use them.

### Using the account sparingly

These limits keep account use to a minimum. They are responsible-use limits, not a way around YouTube's checks,
and they can't guarantee the account won't be restricted.

- **Anonymous first.** Searches, metadata, music videos and imports go without the account. Only an import that
  YouTube's bot check blocked without it is tried with it. The anonymous 5 / 15 / 30 s waits are skipped then.
- **Gently.** With the account: at most one retry (after 10 s), 1.5 s between requests (yt-dlp's
  `sleep_interval_requests`), and one call at a time (a lock in `<dataDir>/account/`).
- **A daily cap that asks.** After 30 imports a day with the account (`ACCOUNT_DAILY_CAP`), the next one opens the
  sign-in window on a local page: "Stop for today (recommended)" or "I understand the risk, continue anyway".
  Closing the window means stop, and the import says today's limit is reached, with stopping recommended.
  Continuing lifts the cap until local midnight only. It never lifts the 24-hour pause below.
- **Stop at warning signs.** If a signed-in call is still blocked, or YouTube reports account trouble (unusual
  traffic, HTTP 429 / too many requests, a suspended or terminated account), the account isn't used for 24 hours.
  The user is told to check it in a browser and not to keep retrying. A sign-in that yt-dlp reports as no longer
  valid isn't a warning sign: the next blocked import opens the sign-in window again.
- **Music videos never** use the account. A blocked music video falls back to the thumbnail / embed as before.

The counters live in `<dataDir>/account/usage.json`, which a sign-out keeps.

### Actions and older apps

An app that shows action buttons passes `context["features"] = ["actions"]`; it reads an error's `action`
attribute (`{"id", "label", "hint"}`, in the app's language) and calls `action(action_id, progress, cancel, options)`
(→ `{"ok", "message"}`, `timeouts.action` = 900 s). The manifest lists the ids as a top-level `"actions"` key, not as
a capability: protocol 1 adds new features as optional manifest keys, and apps that don't know the key ignore it,
so 1.0.5 still installs on them.
Older apps without `features` get the same sign-in from the import itself: a blocked import opens the sign-in window
(warning first) and goes on once the user signed in.

### Language

Error and progress messages are in English, Traditional Chinese, Japanese or Korean: the app's language when the app
passes it (`context["locale"]`), otherwise the system's display language.

## Settings

| Setting | |
|---|---|
| Download the music video | off: audio only, the stage shows the thumbnail |
| Use the uploader's subtitles as lyrics candidates | |

The sign-in needs no setting. The browser sign-in and cookies.txt settings of 1.0.2-1.0.4 are gone, and values
saved by those versions are ignored.

## Install

In KAREOKIE: **Extensions** → **Install from file** with a `kareokie-ext-youtube-<version>.kext` (or `.zip`) package
from this repository's releases.

## Updates

KAREOKIE checks the version in [`index.json`](index.json) (the manifest's `updateIndex`) and offers
an update on the Extensions page. The extension updates independently of the app.

## Build

```bash
python build.py                                              # unsigned zip (development)
python build.py --unsigned                                   # unsigned even with KAREOKIE_SIGNING_KEY set
python build.py --key keys/kareokie-2026.private.pem --key-id kareokie-2026 --index index.json \
  --base-url https://github.com/a8594755-maker/kareokie-ext-youtube/releases/download/v1.0.0/
```

`build.py` runs on Windows and macOS (tested with Python 3.12 on both) and never packs the Finder's `.DS_Store` / `._*` files.
It uses this repository's own `tools/kext.py` (the packaging tool of the [extension SDK](https://github.com/a8594755-maker/kareokie-extension-sdk)) and
nothing outside this repository. This is a community project: KAREOKIE doesn't build, sign or ship it, and no
private key is ever committed.

## Develop

The extension follows the public extension protocol, version 1 ([`PROTOCOL.md` in the extension SDK](https://github.com/a8594755-maker/kareokie-extension-sdk))
and nothing else of KAREOKIE: no app modules, files or settings outside `dataDir` and the song folder, and no
special treatment of its id. What a newer app can do is read from the context: `appVersion` (from 0.3.0 errors are
shown without the exception class, so the title goes into the message), `locale` and `features`. Each call runs in its
own process:
`search(query, limit)`, `resolve(item_or_url, dest_dir, progress, cancel, options)`,
`fetch_video(...)`, `metadata(item_or_url)`, `action(action_id, progress, cancel, options)`; `setup(context)`
receives the settings (and, from newer apps, the app's `locale` and `features`). Tests (no network, no browser):
`python -m unittest discover tests`.

## License

MIT — see [LICENSE](LICENSE).
