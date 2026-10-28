"""
KAREOKIE YouTube source extension (yt-dlp).

Implements the KAREOKIE source interface (docs/extensions.md in the KAREOKIE repository):
    search(query, limit)                               -> [{id, title, artist, durationSec, thumbnailUrl, url, extra}]
    resolve(item_or_url, dest_dir, progress, cancel)   -> {audio_path, subtitles, metadata}
    fetch_video(item_or_url, dest_dir, progress, cancel, options) -> {video_path} | None   (AV1 > VP9 > H.264)
    metadata(item_or_url)                              -> {title, artist, duration, thumbnail, source_url, query_hints}
URLs are routed here by the manifest's `urlPatterns`. Every call runs in its own process, started by the app.

yt-dlp has to keep up with YouTube, so the extension keeps its own copy up to date (see "yt-dlp updates") and
turns the yt-dlp errors people actually hit into advice they can act on (see "Readable errors").

For accessing content you have the rights to use; you must follow YouTube's Terms of Service.
"""

from __future__ import annotations

import glob
import importlib
import json
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

SUBTITLE_LANGS = ["zh-TW", "zh-Hant", "zh-HK", "zh-CN", "zh", "yue", "yue-HK", "nan", "nan-TW", "ja", "ko", "en"]
_settings: Dict[str, Any] = {}
_context: Dict[str, Any] = {}
HERE = os.path.dirname(os.path.abspath(__file__))


def setup(context: Dict[str, Any]) -> None:
    """Called by the app before every call: {settings, dataDir, appVersion, extensionId, version}."""
    _context.clear()
    _context.update(context or {})
    _settings.clear()
    _settings.update(context.get("settings") or {})


# ---------------------------------------------------------------------------
# yt-dlp updates
#
# The app installs yt-dlp into _deps/ once, when the extension is installed, and keeps it while the
# requirements stay the same. YouTube changes often and old yt-dlp versions get blocked or break, so
# imports check PyPI at most every UPDATE_EVERY_SEC (and again, sooner, after an error a newer yt-dlp may
# fix) and install a newer yt-dlp into <dataDir>/yt-dlp/<version>/. Whichever copy is newer is used.
# The updater never fails an import: any problem is logged and the current copy is kept.
# ---------------------------------------------------------------------------

UPDATE_EVERY_SEC = 12 * 3600
RECHECK_AFTER_ERROR_SEC = 30 * 60
PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
PIP_TIMEOUT_SEC = 300
LOCK_STALE_SEC = 600
_active: Dict[str, Any] = {"path": None}


def _vtuple(version: Optional[str]) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version or ""))


def _version_in(root: str) -> Optional[str]:
    """yt-dlp's version in a pip --target folder, read without importing it (None: no real yt-dlp there)."""
    try:
        with open(os.path.join(root, "yt_dlp", "version.py"), encoding="utf-8") as f:
            m = re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def _store() -> Optional[str]:
    data = _context.get("dataDir")
    return os.path.join(data, "yt-dlp") if data else None


def _read_state() -> Dict[str, Any]:
    store = _store()
    try:
        with open(os.path.join(store, "state.json"), encoding="utf-8") as f:  # type: ignore[arg-type]
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(state: Dict[str, Any]) -> None:
    store = _store()
    if not store:
        return
    os.makedirs(store, exist_ok=True)
    tmp = os.path.join(store, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, os.path.join(store, "state.json"))


def _updated_copy() -> Tuple[Optional[str], Optional[str]]:
    """(folder, version) of the yt-dlp this extension installed itself, if any."""
    store = _store()
    version = _read_state().get("version")
    if not store or not version:
        return None, None
    folder = os.path.join(store, str(version))
    found = _version_in(folder)
    return (folder, found) if found else (None, None)


def _bundled_version() -> Optional[str]:
    return _version_in(os.path.join(HERE, "_deps"))


def _active_version() -> Optional[str]:
    folder, version = _updated_copy()
    bundled = _bundled_version()
    return version if folder and _vtuple(version) > _vtuple(bundled) else bundled


def _yt_dlp() -> Any:
    """Import yt_dlp from the newer of _deps/ and the updated copy (re-imported after an update)."""
    folder, version = _updated_copy()
    want = folder if folder and _vtuple(version) > _vtuple(_bundled_version()) else None
    if want != _active["path"]:
        if _active["path"] in sys.path:
            sys.path.remove(_active["path"])
        if want:
            sys.path.insert(0, want)
        for name in list(sys.modules):
            if name == "yt_dlp" or name.startswith(("yt_dlp.", "yt_dlp_ejs")):
                del sys.modules[name]
        importlib.invalidate_caches()
        _active["path"] = want
    import yt_dlp

    return yt_dlp


def _latest_release() -> Tuple[str, str]:
    """(latest yt-dlp version, the yt-dlp-ejs requirement that version asks for) from PyPI."""
    req = urllib.request.Request(PYPI_URL, headers={"User-Agent": "kareokie-ext-youtube"})
    with urllib.request.urlopen(req, timeout=10) as r:
        info = json.load(r)["info"]
    ejs = "yt-dlp-ejs"
    for spec in info.get("requires_dist") or []:
        m = re.match(r"^\s*yt-dlp-ejs\s*(==\s*[A-Za-z0-9.]+)?", spec)
        if m:
            ejs = "yt-dlp-ejs" + (m.group(1) or "").replace(" ", "")
            break
    return str(info["version"]), ejs


def _take_lock(store: str) -> Optional[str]:
    lock = os.path.join(store, ".lock")
    try:
        if time.time() - os.path.getmtime(lock) > LOCK_STALE_SEC:
            os.remove(lock)  # left behind by a call that was killed
    except OSError:
        pass
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return lock
    except OSError:
        return None  # another call is updating right now


def update_yt_dlp(progress: Optional[Callable[..., None]] = None, after_error: bool = False) -> bool:
    """Install a newer yt-dlp when one is out and it's time to look. True when a newer one was installed."""
    store = _store()
    bundled = _bundled_version()
    if not store or not bundled:
        return False  # no data folder, or no yt-dlp of our own (a development / test setup): leave it alone
    state = _read_state()
    since = time.time() - float(state.get("checkedAt") or 0)
    if since < (RECHECK_AFTER_ERROR_SEC if after_error else UPDATE_EVERY_SEC):
        return False
    os.makedirs(store, exist_ok=True)
    lock = _take_lock(store)
    if lock is None:
        return False
    try:
        latest, ejs = _latest_release()
        current = _active_version()
        _write_state(dict(state, checkedAt=time.time(), latest=latest))
        if _vtuple(latest) <= _vtuple(current):
            return False
        if progress is not None:
            progress(None, f"Updating yt-dlp {current} → {latest}…")
        target = os.path.join(store, latest)
        tmp = target + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        cmd = [sys.executable, "-m", "pip", "install", "--target", tmp, "--no-deps", "--upgrade", "--no-input",
               "--disable-pip-version-check", "--no-warn-script-location", "--progress-bar", "off",
               f"yt-dlp=={latest}", ejs]
        done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace", timeout=PIP_TIMEOUT_SEC,
                              creationflags=0x08000000 if sys.platform == "win32" else 0)
        if done.returncode != 0 or _version_in(tmp) != latest:
            print(f"[yt-dlp update] pip failed ({done.returncode}):\n{done.stdout[-2000:]}")
            shutil.rmtree(tmp, ignore_errors=True)
            return False
        shutil.rmtree(target, ignore_errors=True)
        os.replace(tmp, target)
        previous = state.get("version")
        _write_state(dict(_read_state(), version=latest, previousVersion=previous, updatedAt=time.time()))
        # keep the copy before this one: a call that started earlier may still be using it
        for name in os.listdir(store):
            path = os.path.join(store, name)
            if os.path.isdir(path) and name not in (latest, previous):
                shutil.rmtree(path, ignore_errors=True)
        print(f"[yt-dlp update] {current} → {latest}")
        return True
    except Exception as exc:  # noqa: BLE001 - offline, PyPI down, pip missing…: keep the current yt-dlp
        print(f"[yt-dlp update] skipped: {exc}")
        return False
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# JavaScript runtime
#
# yt-dlp solves YouTube's player challenges with deno, Node.js or Bun (and yt-dlp-ejs); without one it gets fewer
# formats. On Windows yt-dlp finds them on PATH. An app started from the Finder on macOS has only
# /usr/bin:/bin:/usr/sbin:/sbin on its PATH, so a runtime from Homebrew, the deno / Bun installers or nvm isn't found
# there: the extension looks in those places itself and gives yt-dlp the paths. When none of them is new enough,
# imports download the extension's own deno, pinned with its SHA-256, into <dataDir>/deno/<version>/.
# ---------------------------------------------------------------------------

DENO_VERSION = "2.9.8"
DENO_DOWNLOADS = {  # (sys.platform, platform.machine()) -> (url, sha256 of the zip)
    ("darwin", "arm64"): (
        f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/deno-aarch64-apple-darwin.zip",
        "9f0bf660393da2eaff044cdc7444cb08c73f235849b61d224d825a194ce7c98e",
    ),
}
DENO_TIMEOUT_SEC = 900
# the oldest versions yt-dlp accepts (yt_dlp/utils/_jsruntime.py)
JS_MIN_VERSIONS = {"deno": (2, 3, 0), "node": (22, 0, 0), "bun": (1, 2, 11)}
MAC_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "~/.deno/bin", "~/.bun/bin",
                "~/.volta/bin", "~/.local/bin")


def _own_deno() -> Optional[str]:
    data = _context.get("dataDir")
    exe = os.path.join(data, "deno", DENO_VERSION, "deno") if data else ""
    return exe if exe and os.path.isfile(exe) and os.access(exe, os.X_OK) else None


def _exe_version(path: str) -> Tuple[int, ...]:
    """`<runtime> --version` as a tuple: deno 2.9.8 …, v24.11.1, 1.2.11 (() when it doesn't run)."""
    try:
        out = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace", timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    m = re.search(r"\d+(?:\.\d+)+", out or "")
    return _vtuple(m.group(0)) if m else ()


def _mac_runtimes() -> Dict[str, str]:
    """deno / node / bun the user installed, wherever macOS installers put them: name -> path of the first one that
    runs and is new enough for yt-dlp (a Homebrew node whose libraries were upgraded away doesn't start)."""
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    dirs += [os.path.expanduser(d) for d in MAC_BIN_DIRS]
    nvm = glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin"))
    dirs += sorted(nvm, key=lambda d: _vtuple(os.path.basename(os.path.dirname(d))), reverse=True)
    found: Dict[str, str] = {}
    for name in JS_MIN_VERSIONS:
        for d in dirs:
            exe = os.path.join(d, name)
            if os.path.isfile(exe) and os.access(exe, os.X_OK) and _exe_version(exe) >= JS_MIN_VERSIONS[name]:
                found[name] = exe
                break
    return found


def _js_runtimes() -> Dict[str, Dict[str, str]]:
    """yt-dlp's `js_runtimes`: on macOS with the paths found, elsewhere looked up on PATH by yt-dlp."""
    if sys.platform != "darwin":
        return {"deno": {}, "node": {}, "bun": {}}
    own = _own_deno()
    found = _mac_runtimes()
    if own:
        found["deno"] = own
    return {name: {"path": path} for name, path in found.items()} or {"deno": {}}


def _no_quarantine(path: str) -> None:
    """Gatekeeper blocks quarantined executables; files written by this process shouldn't have it, but make sure."""
    subprocess.run(["/usr/bin/xattr", "-r", "-d", "com.apple.quarantine", path], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def ensure_js_runtime(progress: Optional[Callable[..., None]] = None,
                      cancel: Optional[Callable[[], None]] = None) -> Optional[str]:
    """macOS: the path of a JavaScript runtime yt-dlp accepts, downloading the pinned deno when there's none.
    Never fails an import (None: yt-dlp goes on without one). Elsewhere: nothing to do."""
    spec = DENO_DOWNLOADS.get((sys.platform, platform.machine()))
    data = _context.get("dataDir")
    if sys.platform != "darwin" or not _bundled_version():
        return None  # Windows finds runtimes on PATH; a development / test setup (no yt-dlp of our own) is left alone
    own = _own_deno() or next(iter(_mac_runtimes().values()), None)
    if own:
        return own
    if not spec or not data:
        return None
    root = os.path.join(data, "deno")
    os.makedirs(root, exist_ok=True)
    failed = os.path.join(root, ".failed")
    try:
        if time.time() - os.path.getmtime(failed) < RECHECK_AFTER_ERROR_SEC:
            return None  # the last download failed a moment ago (offline?): don't fetch 40 MB on every import
    except OSError:
        pass
    lock = _take_lock(root)
    if lock is None:
        return None  # another call is downloading it right now
    url, sha256 = spec
    tmp = os.path.join(root, DENO_VERSION + ".tmp")
    try:
        if progress is not None:
            progress(None, f"Downloading the JavaScript runtime for YouTube (deno {DENO_VERSION}, about 40 MB)…")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        archive = os.path.join(tmp, "deno.zip")
        digest = hashlib.sha256()
        started = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "kareokie-ext-youtube"})
        with urllib.request.urlopen(req, timeout=30) as r, open(archive, "wb") as f:
            while True:
                if cancel is not None:
                    cancel()
                if time.time() - started > DENO_TIMEOUT_SEC:
                    raise TimeoutError(f"took longer than {DENO_TIMEOUT_SEC} s")
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                f.write(chunk)
        if digest.hexdigest() != sha256:
            raise ValueError(f"{url}: SHA-256 {digest.hexdigest()} is not the pinned {sha256}")
        exe = os.path.join(tmp, "deno")
        with zipfile.ZipFile(archive) as z, z.open("deno") as src, open(exe, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(archive)
        os.chmod(exe, 0o755)
        _no_quarantine(tmp)
        if _exe_version(exe) < JS_MIN_VERSIONS["deno"]:
            raise RuntimeError("the downloaded deno doesn't run")
        target = os.path.join(root, DENO_VERSION)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(tmp, target)
        for name in os.listdir(root):  # older pinned versions
            path = os.path.join(root, name)
            if os.path.isdir(path) and name != DENO_VERSION:
                shutil.rmtree(path, ignore_errors=True)
        try:
            os.remove(failed)
        except OSError:
            pass
        print(f"[deno] installed {DENO_VERSION}")
        return os.path.join(target, "deno")
    except Exception as exc:  # noqa: BLE001 - offline, GitHub down…: yt-dlp goes on without a runtime
        if "cancel" in type(exc).__name__.lower():
            raise  # the user cancelled the import (the runner's cancel())
        print(f"[deno] download skipped: {exc}")
        with open(failed, "w", encoding="utf-8"):
            pass
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.remove(lock)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Readable errors
#
# The app shows "YouTube couldn't finish: <exception class>: <message>", so the class carries a short
# title and the message says what to do. Errors the app already explains (private, age-restricted,
# region, removed, 429, network) pass through unchanged.
# ---------------------------------------------------------------------------

BROWSERS = {"firefox": "Firefox", "chrome": "Chrome", "edge": "Edge", "brave": "Brave"}

MESSAGES: Dict[str, Dict[str, Tuple[str, str]]] = {
    "blocked": {
        "en": ("YouTube is blocking this network for now",
               "It asked to confirm you’re not a bot. Turn off any VPN or proxy, switch to another network "
               "(a phone hotspot works), or wait a few hours, then retry. If it keeps happening, choose your browser "
               "under “Use my YouTube sign-in from a browser” in this extension’s settings."),
        "zh-TW": ("YouTube 暫時擋下了這個網路",
                  "它要求確認你不是機器人。請關閉 VPN 或 Proxy、換一個網路（例如手機熱點），或等幾個小時後再重試。"
                  "一直發生的話，可以到擴充功能頁的 YouTube 設定，在「使用瀏覽器的 YouTube 登入狀態」選擇你的瀏覽器。"),
        "ja": ("YouTube がこのネットワークを一時的にブロックしています",
               "ボットではないことの確認を求められました。VPN やプロキシをオフにする、別のネットワーク（スマホのテザリングなど）に"
               "切り替える、または数時間待ってから再試行してください。続く場合は、拡張機能ページの YouTube の設定で"
               "「ブラウザの YouTube ログインを使う」からブラウザを選んでください。"),
    },
    "cookies": {
        "en": ("Couldn’t read your YouTube sign-in from {browser}",
               "Make sure {browser} is installed and signed in to YouTube, close it and retry. On Windows, Chrome, Edge "
               "and Brave encrypt their cookies, so they usually can’t be read: Firefox works best. Or set this "
               "extension’s “Use my YouTube sign-in from a browser” back to “Don’t use”."),
        "zh-TW": ("讀不到 {browser} 的 YouTube 登入狀態",
                  "請確認已安裝 {browser} 並登入 YouTube，關閉瀏覽器後再重試。Windows 上的 Chrome、Edge、Brave 會加密 Cookie，"
                  "通常讀不到，Firefox 最可靠。也可以把擴充功能設定的「使用瀏覽器的 YouTube 登入狀態」改回「不使用」。"),
        "ja": ("{browser} の YouTube ログインを読み取れませんでした",
               "{browser} がインストールされ YouTube にログインしていることを確認し、ブラウザを閉じてから再試行してください。"
               "Windows の Chrome・Edge・Brave は Cookie を暗号化しているため通常は読み取れません。Firefox が最も確実です。"
               "または拡張機能の設定の「ブラウザの YouTube ログインを使う」を「使わない」に戻してください。"),
    },
    "cookies-mac": {
        "en": ("Couldn’t read your YouTube sign-in from {browser}",
               "Make sure {browser} is installed and signed in to YouTube, close it and retry. For Chrome, Edge and "
               "Brave, macOS asks to let “security” use the browser’s “Safe Storage” item in your keychain: enter "
               "your Mac password and choose Allow. Or set this extension’s “Use my YouTube sign-in from a browser” back to “Don’t use”."),
        "zh-TW": ("讀不到 {browser} 的 YouTube 登入狀態",
                  "請確認已安裝 {browser} 並登入 YouTube，關閉瀏覽器後再重試。Chrome、Edge、Brave 的話，macOS 會詢問是否允許"
                  "「security」使用鑰匙圈裡該瀏覽器的「Safe Storage」項目，請輸入 Mac 密碼並選擇「允許」。也可以把擴充功能設定的「使用瀏覽器的 YouTube 登入狀態」改回「不使用」。"),
        "ja": ("{browser} の YouTube ログインを読み取れませんでした",
               "{browser} がインストールされ YouTube にログインしていることを確認し、ブラウザを閉じてから再試行してください。"
               "Chrome・Edge・Brave では、キーチェーンにあるブラウザの「Safe Storage」項目を「security」が使うことを macOS が確認するので、"
               "Mac のパスワードを入力して「許可」を選んでください。"
               "または拡張機能の設定の「ブラウザの YouTube ログインを使う」を「使わない」に戻してください。"),
    },
    "cookiefile": {
        "en": ("Couldn’t read the cookies.txt file",
               "Check the path in this extension’s settings: it must be a cookies.txt in Netscape format, exported "
               "from a browser that is signed in to YouTube. Or clear the setting."),
        "zh-TW": ("讀不到 cookies.txt 檔案",
                  "請檢查擴充功能設定裡的路徑：必須是從已登入 YouTube 的瀏覽器匯出的 cookies.txt（Netscape 格式）。或把設定清空。"),
        "ja": ("cookies.txt ファイルを読み取れませんでした",
               "拡張機能の設定のパスを確認してください。YouTube にログインしたブラウザから書き出した cookies.txt"
               "（Netscape 形式）である必要があります。または設定を空にしてください。"),
    },
    "changed": {
        "en": ("YouTube changed something yt-dlp can’t handle yet",
               "yt-dlp {version} couldn’t download this video. The extension updates yt-dlp by itself as soon as a "
               "fix is out; please try again later."),
        "zh-TW": ("YouTube 改了網站，yt-dlp 還跟不上",
                  "目前的 yt-dlp {version} 無法下載這支影片。修正版一推出，擴充會自動更新 yt-dlp，請晚一點再重試。"),
        "ja": ("YouTube の変更に yt-dlp がまだ対応していません",
               "現在の yt-dlp {version} ではこの動画をダウンロードできませんでした。修正版が出ると拡張機能が自動で"
               " yt-dlp を更新します。しばらくしてから再試行してください。"),
    },
}

_KINDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("cookies", ("cookieloaderror", "failed to decrypt with dpapi", "cookies database", "cookie database",
                 "failed to load cookies", "netscape format cookies file")),
    ("blocked", ("not a bot",)),
    ("changed", ("requested format is not available", "signature extraction failed", "nsig extraction failed",
                 "only images are available", "http error 403")),
]


class YouTubeError(Exception):
    """A yt-dlp failure explained for the user (the original error is chained for the technical details)."""


def _lang() -> str:
    """The user's language (the app doesn't pass its own; the system's display language is the best guess)."""
    tag = ""
    if sys.platform == "win32":
        try:
            import ctypes
            import locale

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            tag = locale.windows_locale.get(lcid, "")
        except Exception:  # noqa: BLE001
            tag = ""
    elif sys.platform == "darwin":
        # an app started from the Finder has no LANG: the first of the user's preferred languages, e.g. "zh-Hant-TW"
        try:
            out = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleLanguages"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5).stdout
            m = re.search(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*", out or "")
            tag = m.group(0) if m else ""
        except (OSError, subprocess.SubprocessError):
            tag = ""
    tag = tag or os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    tag = tag.lower()
    if tag.startswith("zh"):
        return "zh-TW"
    if tag.startswith("ja"):
        return "ja"
    return "en"


def error_kind(exc: BaseException) -> Optional[str]:
    low = f"{type(exc).__name__}: {exc}".lower().replace("’", "'")
    for kind, keys in _KINDS:
        if any(k in low for k in keys):
            return kind
    return None


def readable_error(kind: str, exc: BaseException) -> YouTubeError:
    browser = BROWSERS.get(str(_settings.get("cookiesFromBrowser")))
    if kind == "cookies" and not browser:
        kind = "cookiefile"
    elif kind == "cookies" and sys.platform == "darwin":
        kind = "cookies-mac"  # no DPAPI there: the keychain holds Chromium's cookie key
    title, body = MESSAGES[kind][_lang()]
    fmt = {"browser": browser or "", "version": _active_version() or "?"}
    # a subclass named after the title, so the app's "<class>: <message>" reads as a sentence
    cls = type(title.format(**fmt), (YouTubeError,), {})
    err = cls(body.format(**fmt))
    err.__cause__ = exc
    return err


# ---------------------------------------------------------------------------
# yt-dlp options shared by every call
# ---------------------------------------------------------------------------


def _ydl(opts: Dict[str, Any]) -> Any:
    yt_dlp = _yt_dlp()
    base: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # YouTube's player challenges are solved with a JavaScript runtime when one is installed
        # (deno, Node.js or Bun) and the yt-dlp-ejs package; without one yt-dlp falls back to fewer formats
        # (a flat search never runs YouTube's player, so it doesn't look for one: searches stay fast)
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}} if opts.get("extract_flat") else _js_runtimes(),
    }
    browser = str(_settings.get("cookiesFromBrowser") or "off")
    if browser in BROWSERS:
        base["cookiesfrombrowser"] = (browser, None, None, None)
    cookie_file = str(_settings.get("cookiesFile") or "").strip().strip('"')
    if cookie_file:
        if not os.path.isfile(cookie_file):
            raise readable_error("cookiefile", FileNotFoundError(cookie_file))
        base["cookiefile"] = cookie_file
    return yt_dlp.YoutubeDL({**base, **opts})


def _run(call: Callable[[], Any], progress: Optional[Callable[..., None]] = None, update: bool = False) -> Any:
    """Run a yt-dlp call with readable errors. With `update` (imports, which have time for pip): after an
    error a newer yt-dlp may fix, update yt-dlp and try once more."""
    try:
        return call()
    except YouTubeError:
        raise
    except Exception as exc:  # noqa: BLE001
        kind = error_kind(exc)
        if update and kind in ("blocked", "changed") and update_yt_dlp(progress, after_error=True):
            try:
                return call()
            except Exception as exc2:  # noqa: BLE001
                exc, kind = exc2, error_kind(exc2)
        if kind is None:
            raise exc
        raise readable_error(kind, exc) from exc


def _url(item_or_url: Any) -> str:
    if isinstance(item_or_url, dict):
        if item_or_url.get("url"):
            return str(item_or_url["url"])
        if item_or_url.get("id"):
            return f"https://www.youtube.com/watch?v={item_or_url['id']}"
    url = str(item_or_url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return f"https://www.youtube.com/watch?v={url}"
    return url


# ---------------------------------------------------------------------------
# Search (no API key): ytsearchN with flat extraction, so only the results page is fetched (about 1-2 s)
# ---------------------------------------------------------------------------


def search(query: str, limit: int) -> List[Dict[str, Any]]:
    def call() -> Dict[str, Any]:
        # ask for a few extra so skipped live streams don't shrink the list
        with _ydl({"skip_download": True, "extract_flat": "in_playlist"}) as ydl:
            return ydl.extract_info(f"ytsearch{limit + 4}:{query}", download=False) or {}

    info = _run(call)

    out: List[Dict[str, Any]] = []
    for e in info.get("entries") or []:
        vid = e.get("id")
        # live streams / upcoming premieres can't be imported as songs
        if not vid or len(vid) != 11 or e.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue
        channel = e.get("channel") or e.get("uploader") or ""
        out.append(
            {
                "id": vid,
                "title": e.get("title") or vid,
                "artist": channel,
                "durationSec": float(e["duration"]) if e.get("duration") else None,
                "thumbnailUrl": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={vid}",
                "extra": {
                    "channel": channel,
                    "viewCount": int(e["view_count"]) if e.get("view_count") else None,
                    "verified": bool(e.get("channel_is_verified")),
                },
            }
        )
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Resolve: the best audio stream as-is + the uploader's subtitles
# ---------------------------------------------------------------------------


def _hook(progress: Callable[..., None], cancel: Callable[[], None]) -> Callable[[Dict[str, Any]], None]:
    def hook(d: Dict[str, Any]) -> None:
        cancel()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(100.0 * float(d.get("downloaded_bytes") or 0) / float(total), "download")

    return hook


def _metadata(info: Dict[str, Any], url: str) -> Dict[str, Any]:
    vid = info.get("id") or ""
    return {
        "title": info.get("title") or "",
        "artist": info.get("artist") or info.get("creator") or info.get("uploader") or "",
        "duration": float(info.get("duration") or 0.0),
        "thumbnail": info.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""),
        "source_url": info.get("webpage_url") or url,
        "item_id": vid,
        # what KAREOKIE's lyrics search reads to build its queries
        "query_hints": {
            k: v for k, v in {
                "title": info.get("title"),
                "track": info.get("track"),
                "artist": info.get("artist"),
                "creator": info.get("creator"),
                "uploader": info.get("uploader"),
                "channel": info.get("channel"),
                "description": (info.get("description") or "")[:5000],
                # the uploader's language of the video: the sung language when Whisper can't tell (app 0.2.0+)
                "language": info.get("language"),
                "duration": info.get("duration"),
            }.items() if v
        },
    }


def resolve(item_or_url: Any, dest_dir: str, progress: Callable[..., None], cancel: Callable[[], None],
            options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    options = options or {}
    url = _url(item_or_url)
    want_subs = _settings.get("subtitles", True)
    update_yt_dlp(progress)
    ensure_js_runtime(progress, cancel)
    # 1. Download the highest bitrate audio stream as-is (no re-encode; KAREOKIE's separator
    #    decodes it once to 44.1 kHz float32) + subtitles
    out_tmpl = os.path.join(dest_dir, "raw_audio.%(ext)s")
    ydl_audio_opts = {
        # A plain HTTPS stream first: segmented (HLS) audio can lose segments on a bad connection.
        "format": "bestaudio[protocol^=http]/bestaudio/best",
        # A failed piece fails the download instead of leaving a file with missing or no audio.
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": False,
        "outtmpl": out_tmpl,
        "writesubtitles": bool(want_subs),
        "subtitleslangs": list(options.get("subtitleLanguages") or SUBTITLE_LANGS),
        "subtitlesformat": "vtt",
        "progress_hooks": [_hook(progress, cancel)],
    }

    def call() -> Dict[str, Any]:
        with _ydl(ydl_audio_opts) as ydl:
            return ydl.extract_info(url, download=True) or {}

    info = _run(call, progress, update=True)

    raw = ""
    for d in info.get("requested_downloads") or []:
        fp = d.get("filepath")
        if fp and os.path.exists(fp):
            raw = fp
            break
    if not raw:
        files = [w for w in glob.glob(os.path.join(dest_dir, "raw_audio.*"))
                 if not w.endswith((".vtt", ".part", ".ytdl", ".json"))]
        if not files:
            raise RuntimeError("Couldn't download audio from that YouTube URL. Check that the URL is valid.")
        raw = files[0]

    subtitles = []
    for path in sorted(glob.glob(os.path.join(dest_dir, "raw_audio.*.vtt"))):
        m = re.search(r"\.([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*)\.vtt$", path)
        subtitles.append({"lang": m.group(1) if m else "und", "path": path, "kind": "subtitles"})
    return {"audio_path": raw, "video_path": None, "subtitles": subtitles, "metadata": _metadata(info, url)}


# ---------------------------------------------------------------------------
# Music video (downloaded by the app while it separates the audio)
# ---------------------------------------------------------------------------


def format_selector(height: int) -> str:
    """yt-dlp format: video-only at most `height`, AV1 > VP9 > H.264, then anything at that height.
    AV1 / H.264 come as video.mp4, VP9 as video.webm (all play in Chromium / Electron)."""
    h = f"[height<={int(height)}]"
    return "/".join([
        f"bestvideo{h}[vcodec^=av01]",
        f"bestvideo{h}[vcodec^=vp09]",
        f"bestvideo{h}[vcodec=vp9]",
        f"bestvideo{h}[vcodec^=avc1]",
        f"bestvideo{h}",
        f"best{h}",
    ])


PLAYABLE = (".mp4", ".webm")


def _video_in(dest_dir: str) -> Optional[str]:
    for name in sorted(os.listdir(dest_dir)):
        if name.startswith("video.") and name.endswith(PLAYABLE):
            return os.path.join(dest_dir, name)
    return None


def fetch_video(item_or_url: Any, dest_dir: str, progress: Callable[..., None], cancel: Callable[[], None],
                options: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The music video, video-only, at most the height the app asks for (its `videoQuality` setting)."""
    if not _settings.get("downloadVideo", True):
        return None
    height = int((options or {}).get("videoMaxHeight") or 0)
    if height <= 0:
        return None  # the app's videoQuality is "off"
    existing = _video_in(dest_dir)
    if existing:
        return {"video_path": existing}
    try:
        opts = {
            "format": format_selector(height),
            "outtmpl": os.path.join(dest_dir, "video.%(ext)s"),
            "progress_hooks": [_hook(progress, cancel)],
        }
        with _ydl(opts) as ydl:
            ydl.download([_url(item_or_url)])
    except Exception as exc:  # noqa: BLE001 - the stage falls back to the YouTube embed / thumbnail
        print(f"[Video Download Note] Using YouTube embed fallback: {exc}")
    path = _video_in(dest_dir)
    if path is None:
        # a container the player can't use (e.g. .mkv from a merge): drop it
        for name in os.listdir(dest_dir):
            if name.startswith("video.") and not name.endswith((".part", ".ytdl")):
                try:
                    os.remove(os.path.join(dest_dir, name))
                except OSError:
                    pass
    return {"video_path": path} if path else None


def metadata(item_or_url: Any) -> Dict[str, Any]:
    url = _url(item_or_url)

    def call() -> Dict[str, Any]:
        with _ydl({"skip_download": True}) as ydl:
            return ydl.extract_info(url, download=False) or {}

    return _metadata(_run(call), url)
