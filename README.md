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
retries the import once. With a JavaScript runtime installed (deno, Node.js or Bun), yt-dlp solves YouTube's
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
- Error messages follow the first of the Mac's preferred languages.

## Settings

| Setting | |
|---|---|
| Download the music video | off: audio only, the stage shows the thumbnail |
| Use the uploader's subtitles as lyrics candidates | |
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
`fetch_video(...)`, `metadata(item_or_url)`; `setup(context)` receives the settings.

## License

MIT — see [LICENSE](LICENSE).
