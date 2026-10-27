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

Dependency: [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense), installed by KAREOKIE into this
extension's own folder (`pip install --target`), never into the app.

## Install

In KAREOKIE: search for a song → **Choose a music source** → YouTube → **Install**, or
**Extensions** → **Install from file** with a `kareokie-ext-youtube-<version>.kext` package.

## Updates

KAREOKIE checks the version in [`index.json`](index.json) (the manifest's `updateIndex`) and offers
an update on the Extensions page. The extension updates independently of the app.

## Build

```bash
python build.py                                              # unsigned zip (development)
python build.py --key keys/kareokie-2026.private.pem --key-id kareokie-2026 --index index.json \
  --base-url https://github.com/a8594755-maker/kareokie-ext-youtube/releases/download/v1.0.0/
```

`build.py` uses `tools/kext.py` (copied from the KAREOKIE repository's `extensions/tools/`). This is a
community project: KAREOKIE doesn't build, sign or ship it, and no private key is ever committed.

## Develop

The interface is documented in KAREOKIE's `docs/extensions.md`. Each call runs in its own process:
`search(query, limit)`, `resolve(item_or_url, dest_dir, progress, cancel, options)`,
`fetch_video(...)`, `metadata(item_or_url)`; `setup(context)` receives the settings.

## License

MIT — see [LICENSE](LICENSE).
