# Changelog

## 1.0.3 — 2026-10-27

- macOS (Apple Silicon): the JavaScript runtime for YouTube's player challenges is found where Mac installers put it
  (Homebrew, MacPorts, the deno / Bun installers, Volta, nvm), since an app started from the Finder doesn't see the
  shell's PATH. Without a working one (Node.js 22+, deno 2.3+, Bun 1.2.11+) the first import downloads a pinned
  deno, checked against its SHA-256, into the extension's data folder. Windows is unchanged.
- macOS: readable errors follow the Mac's preferred language, and the browser sign-in error explains the keychain
  prompt instead of Windows' cookie encryption.
- `build.py --unsigned`, and the package never includes the Finder's `.DS_Store` / `._*` files.

## 1.0.2 — 2026-10-27

- yt-dlp keeps itself up to date: imports check PyPI at most every 12 hours (and again after an error a newer
  yt-dlp may fix, at most every 30 minutes) and install a newer yt-dlp into the extension's data folder. After a
  "confirm you're not a bot" or a broken-format error, the import is retried once with the new yt-dlp. Before,
  yt-dlp stayed at the version installed with the extension, and old versions get blocked by YouTube.
- Readable errors in English, Traditional Chinese and Japanese (Windows' display language) instead of raw yt-dlp
  output: blocked by YouTube's bot check (what to try: no VPN / proxy, another network, wait, browser sign-in),
  cookies that can't be read, and YouTube changes yt-dlp can't handle yet.
- New settings: "Use my YouTube sign-in from a browser" (Firefox, Chrome, Edge, Brave; off by default) and a
  cookies.txt file, for networks YouTube keeps blocking. Firefox is the one that works on Windows: Chrome, Edge
  and Brave encrypt their cookies.
- YouTube's player challenges are solved with a JavaScript runtime when one is installed (deno, Node.js or Bun)
  together with the new `yt-dlp-ejs` dependency, which gives yt-dlp the full list of formats.
- The video's language (the uploader's setting) goes to KAREOKIE with the title and artist. KAREOKIE 0.2.0 uses it
  to tell the original lyrics from the subtitles' translations when it can't recognize the sung language itself.

## 1.0.1 — 2026-10-26

- Released as a community extension: unsigned (KAREOKIE shows its usual "do you trust this extension" warning
  before installing), author "kareokie-ext-youtube contributors". yt-dlp stays unpinned so new yt-dlp releases
  are picked up when the extension is installed or updated.
- More reliable audio downloads: a plain HTTPS audio stream is preferred over segmented (HLS) audio, failed
  pieces are retried, and a download that still loses a piece fails instead of leaving a file with missing or
  no audio (reported as "Couldn't decode the audio … received no packets").

## 1.0.0 — 2026-10-24

- First release as a separate extension (the YouTube code previously built into KAREOKIE).
- Search YouTube without an API key (`ytsearch`, flat extraction; live streams skipped).
- Import: best audio stream as-is, the uploader's subtitles (lyrics candidates) and the music video
  (downloaded while KAREOKIE separates the audio, video-only, up to the app's `videoQuality` height, in the
  most space-efficient codec offered: AV1, then VP9, then H.264 → `video.mp4` / `video.webm`; none when off).
- Settings: download the music video, use subtitles.
- yt-dlp is installed into the extension's own folder, never into the app.
