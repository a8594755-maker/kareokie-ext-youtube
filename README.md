# kareokie-ext-youtube: a YouTube source for KAREOKIE

A free, community-made **music source extension** for [KAREOKIE](https://github.com/a8594755-maker/KAREOKIE):
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
| URLs | `youtube.com/watch?v=…`, `youtu.be/…`, `/shorts/`, `/embed/`, `/live/`, `music.youtube.com` |

Dependencies: [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense) and `yt-dlp-ejs`, installed by KAREOKIE
into this extension's own folder (`pip install --target`), never into the app. YouTube changes often, so the
extension keeps yt-dlp up to date by itself: imports check PyPI at most every 12 hours and install a newer
yt-dlp into the extension's data folder (`<dataDir>/yt-dlp/<version>/`); the newer of the two copies is used.
An error a newer yt-dlp may fix ("confirm you're not a bot", missing formats, HTTP 403) checks again sooner and
retries the import once. A "confirm you're not a bot" check is often a passing flag on one request, so a
blocked import also waits and tries again (after 5, 15 and 30 s; searches once, after 3 s) before it reports
the error. That error says what works on this computer: a YouTube sign-in (the sign-in window below when Chrome or
Edge is installed, Firefox when it is; otherwise a cookies.txt, since on Windows yt-dlp can't read Chrome's and
Edge's encrypted cookies), another network, or waiting. Some blocks don't pass, so a video blocked through every retry is remembered for 6 hours and
importing it again reports at once instead of waiting (`<dataDir>/blocked.json`). With a JavaScript runtime installed (deno, Node.js or Bun), yt-dlp solves YouTube's
player challenges with it and finds every format.

### macOS (Apple Silicon)

The extension runs in KAREOKIE's own Python on the Mac too; nothing is Windows-only.

- **JavaScript runtime.** An app started from the Finder doesn't see your shell's PATH, so the extension looks for
  deno, Node.js (22+) and Bun in Homebrew (`/opt/homebrew/bin`, `/usr/local/bin`), MacPorts, `~/.deno/bin`,
  `~/.bun/bin`, Volta, `~/.local/bin` and nvm, and uses the first one that runs and is new enough. With none, the
  first import downloads deno (version and SHA-256 pinned in `main.py`, about 40 MB) into
  `<dataDir>/deno/<version>/`, executable and without `com.apple.quarantine`. When that download fails it is
  tried again 30 minutes later; yt-dlp works without a runtime meanwhile, with fewer formats.
- **ffmpeg** isn't needed: audio and video are downloaded as-is. yt-dlp would find the app's bundled ffmpeg on the
  PATH the app gives the extension.
- **Browser sign-in.** Firefox, Chrome, Edge and Brave work. For Chrome, Edge and Brave macOS asks once to let
  `security` read the browser's "Safe Storage" keychain item. Safari isn't offered: its cookies need Full Disk
  Access for KAREOKIE.
- With an app that doesn't pass its language, error messages follow the first of the Mac's preferred languages.

### Sign-in window

> **Don't sign in with your main account.** Downloading with an account may make YouTube restrict or disable it.
> Use a separately created secondary account.

With "Sign in to YouTube in a separate window" on, an import YouTube blocks without a sign-in opens Chrome (or
Edge) with a profile of the extension's own (`<dataDir>/signin/profile`, started with `--user-data-dir` and `--remote-debugging-port=0`; the
user's usual profile is never touched).

1. **Warning first, every time.** The window opens on a full-window red warning page, served by the extension on
   `127.0.0.1` under a random path, in the app's language. Its Continue button stays disabled until the user ticks
   "I understand the risk, and I'm using a separately created secondary account" (the server checks it too). Only
   then does it go on to YouTube's sign-in page. Closing the window cancels the sign-in. Nothing is read from the
   browser before the warning is accepted.
2. **The user signs in** there themselves, including any Google verification.
3. The extension waits (up to 15 minutes, cancellable) until youtube.com has a signed-in session, reads that window's
   youtube.com and google.com cookies with the DevTools protocol's `Storage.getCookies` (a small standard-library
   WebSocket client, no extra dependency), writes them to `<dataDir>/signin/cookies.txt` and closes the window.

The sign-in is kept: yt-dlp downloads with it and saves the cookies YouTube refreshes back to it after every call
(through a copy that replaces `cookies.txt` in one step, so a call killed mid-write can't break it). `dataDir`
survives app restarts and extension updates, so there's no need to sign in again. When YouTube stops accepting it,
the error says so and the next blocked import opens the window again (warning first); the kept profile is usually still
signed in, so it just closes again with fresh cookies. Only turning the setting off (sign-out) deletes the profile
and the cookies. Cookie values are never logged or put in messages, and only yt-dlp's requests to YouTube / Google
use them.

### Using the account sparingly

These limits keep account use to a minimum. They are responsible-use limits, not a way around YouTube's checks,
and they can't guarantee the account won't be restricted.

- **Anonymous first.** Searches, metadata, music videos and imports go without the account. Only an import that
  YouTube's bot check blocked without it is tried with it. The anonymous 5 / 15 / 30 s waits are skipped then.
- **Gently.** With the account: at most one retry (after 10 s), 1.5 s between requests (yt-dlp's
  `sleep_interval_requests`), and one call at a time (a lock in `<dataDir>/account/`).
- **A daily cap.** At most 30 imports a day with the account (`ACCOUNT_DAILY_CAP`). Past it, the import says
  today's limit is reached: try tomorrow or another network.
- **Stop at warning signs.** If a signed-in call is still blocked, or YouTube reports account trouble (unusual
  traffic, HTTP 429 / too many requests, a suspended or terminated account), the account isn't used for 24 hours.
  The user is told to check it in a browser and not to keep retrying. A sign-in that yt-dlp reports as no longer
  valid isn't a warning sign: the next blocked import opens the sign-in window again.
- **Music videos never** use the account. A blocked music video falls back to the thumbnail / embed as before.

The counters live in `<dataDir>/account/usage.json`, which a sign-out keeps.

### Language

Error and progress messages are in English, Traditional Chinese, Japanese or Korean: the app's language when the app
passes it (`context["locale"]`), otherwise the system's display language.

## Settings

| Setting | |
|---|---|
| Download the music video | off: audio only, the stage shows the thumbnail |
| Use the uploader's subtitles as lyrics candidates | |
| Sign in to YouTube in a separate window (Chrome / Edge) | off by default. See "Sign-in window" above. Your own cookies.txt or browser setting wins over it. |
| Use my YouTube sign-in from a browser | off by default. For networks YouTube keeps blocking with "confirm you're not a bot": yt-dlp reads that browser's YouTube cookies and downloads as you. On Windows only Firefox works reliably (Chrome, Edge and Brave encrypt their cookies); on a Mac all four work (macOS asks for your password for Chrome, Edge and Brave). YouTube may restrict accounts that download a lot. |
| Or a cookies.txt file | a Netscape-format cookies.txt exported from a signed-in browser |

## Install

In KAREOKIE: search for a song → **Choose a music source** → YouTube → **Install**, or
**Extensions** → **Install from file** with a `kareokie-ext-youtube-<version>.kext` package.

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
It uses `tools/kext.py` (copied from the KAREOKIE repository's `extensions/tools/`). This is a
community project: KAREOKIE doesn't build, sign or ship it, and no private key is ever committed.

## Develop

The interface is documented in KAREOKIE's `docs/extensions.md`. Each call runs in its own process:
`search(query, limit)`, `resolve(item_or_url, dest_dir, progress, cancel, options)`,
`fetch_video(...)`, `metadata(item_or_url)`; `setup(context)` receives the settings (and, from newer
apps, the app's `locale`). Tests (no network): `python -m unittest discover tests`.

## License

MIT — see [LICENSE](LICENSE).
