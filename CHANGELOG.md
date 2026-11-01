# Changelog

## 1.0.5 — 2026-11-01

- Messages follow the app's language: newer KAREOKIE versions pass it to extensions (`locale`: English,
  Traditional Chinese, Japanese, Korean); older ones fall back to the system's display language as before. Korean
  is new. The progress shown while an import waits out a bot check (and while yt-dlp updates) is translated too.
- When YouTube's "confirm you're not a bot" check blocks every try, the error now says what works on this computer:
  with Firefox installed, signing in to YouTube there and choosing it in the settings; on Windows without Firefox,
  exporting a cookies.txt (Chrome and Edge encrypt their cookies, so yt-dlp can't use them there); when a sign-in is
  already set, checking it or exporting a fresh cookies.txt; and another network or waiting. The Firefox check only
  looks for its profile files and reads no cookies.
- Some blocks don't pass: one video can be blocked on every try while others play from the same network. A video
  blocked through every retry is remembered for 6 hours, and importing it again tries once and reports at once
  instead of waiting about a minute. A successful import forgets it.
- New setting "Sign in to YouTube in a separate window (Chrome / Edge)", off by default, with a prominent warning
  not to use a main account (YouTube may restrict or disable accounts that download). The next import opens Chrome
  (or Edge) with a profile of the extension's own, never the usual one. Every time, it first shows a red full-window
  warning page (served on 127.0.0.1, in the app's language) whose Continue button stays disabled until the user
  ticks "I understand the risk, and I'm using a separately created secondary account"; closing the window cancels.
  Then the user signs in to YouTube themselves, and the extension takes that window's youtube.com / google.com
  cookies over the browser's DevTools connection, saves them as a cookies.txt in its data folder, closes the window
  and downloads as that account.
- The sign-in is kept across imports, app restarts and extension updates; yt-dlp's refreshed cookies are saved back
  after every call through a copy that replaces the file in one step. When YouTube stops accepting it, the error
  says so and the next import opens the window again (warning first; the kept profile is usually still signed in).
  Only turning the setting off signs out (deletes the profile and the cookies). The user's own cookies.txt or
  browser setting wins over it. The bot-check error points to it when Chrome or Edge is installed.
- The account is used as little and as gently as possible: searches, metadata, music videos and imports go
  without it, and only an import the bot check blocked without it is tried with it (at most one retry, 1.5 s
  between requests, one at a time). After 30 imports a day with it, the sign-in window asks: stop for today
  (recommended) or "I understand the risk, continue anyway", which holds until local midnight; closing the window
  stops. If it is still blocked while signed
  in, or YouTube reports account trouble (unusual traffic, too many requests, a suspended account), it isn't used
  for 24 hours and the user is told to check the account and not to keep retrying. The warning page says use is
  kept to a minimum but restriction can't be ruled out.

## 1.0.4 — 2026-11-01

- YouTube's "confirm you're not a bot" check no longer fails an import at once. It is often a passing flag on one
  request (the same import asked for again 10-40 s later usually goes through), so a blocked import now waits and
  tries again after 5, 15 and 30 s, showing the wait in its progress; cancelling the import stops the wait.
  Searches and metadata look-ups try once more after 3 s. Only when every try is blocked does the error appear.

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
