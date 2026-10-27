# Changelog

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
