"""
KAREOKIE YouTube source extension (yt-dlp).

Implements the KAREOKIE extension protocol, version 1 (PROTOCOL.md in the public extension SDK,
https://github.com/a8594755-maker/kareokie-extension-sdk):
    search(query, limit)                               -> [{id, title, artist, durationSec, thumbnailUrl, url, extra}]
    resolve(item_or_url, dest_dir, progress, cancel)   -> {audio_path, subtitles, metadata}
    fetch_video(item_or_url, dest_dir, progress, cancel, options) -> {video_path} | None   (AV1 > VP9 > H.264)
    metadata(item_or_url)                              -> {title, artist, duration, thumbnail, source_url, query_hints}
    action(action_id, progress, cancel, options)       -> {ok, message}   (apps with context["features"] "actions")
URLs are routed here by the manifest's `urlPatterns`. Every call runs in its own process, started by the app.
The extension relies only on that protocol: what newer apps can do is read from the context (`appVersion`, `locale`,
`features`), and it writes only to `dataDir` and the song folder.

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
    """Called by the app before every call: {settings, dataDir, appVersion, extensionId, version}, and in newer
    apps {locale}: the app's language (en, zh-TW, ja, ko), and {features}: ["actions"] when the import panel shows
    an error's action as a button (see "Actions")."""
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
            progress(None, _text("updating", current=current, latest=latest))
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
            progress(None, _text("deno", version=DENO_VERSION))
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
            raise  # the user cancelled the import (the app's cancel())
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
# The app shows "YouTube couldn't finish: <message>"; apps before 0.3.0 put the exception class in front
# ("<class>: <message>", see the protocol's Errors). So a message is a short title and what to do: in the message
# from 0.3.0 on, as the class name before. Errors the app already explains (private, age-restricted, region,
# removed, 429, network) pass through unchanged. Messages and progress follow the app's language.
# ---------------------------------------------------------------------------

MESSAGES: Dict[str, Dict[str, Tuple[str, str]]] = {
    # the body is the start of the message: readable_error() adds what works on this computer (BLOCKED_HINTS)
    "blocked": {
        "en": ("YouTube asked to confirm you’re not a bot",
               "It kept asking on every try, so waiting a minute won’t help."),
        "zh-TW": ("YouTube 要求確認你不是機器人",
                  "每次重試都被擋下，再等一下也沒用。"),
        "ja": ("YouTube がボットではないことの確認を求めています",
               "毎回ブロックされたため、少し待っても解決しません。"),
        "ko": ("YouTube에서 로봇이 아닌지 확인을 요청했습니다",
               "매번 차단되어 조금 기다려도 해결되지 않습니다."),
    },
    "blocked-again": {
        "en": ("YouTube asked to confirm you’re not a bot",
               "This video was blocked the last time too, so it wasn’t tried again."),
        "zh-TW": ("YouTube 要求確認你不是機器人",
                  "這支影片上次也被擋下，所以這次沒有再等待重試。"),
        "ja": ("YouTube がボットではないことの確認を求めています",
               "この動画は前回もブロックされたため、待って再試行はしていません。"),
        "ko": ("YouTube에서 로봇이 아닌지 확인을 요청했습니다",
               "이 동영상은 지난번에도 차단되어 이번에는 기다렸다가 다시 시도하지 않았습니다."),
    },
    "signin-needed": {
        "en": ("YouTube asked to confirm you’re not a bot",
               "Downloading without a sign-in was blocked. Sign in with a secondary account and try again, or try "
               "another network later."),
        "zh-TW": ("YouTube 要求確認你不是機器人",
                  "不登入下載被擋下了。請用另外申請的小帳號登入後再試，或稍後換個網路。"),
        "ja": ("YouTube がボットではないことの確認を求めています",
               "ログインなしのダウンロードがブロックされました。サブアカウントでログインしてもう一度試すか、後で別の"
               "ネットワークで試してください。"),
        "ko": ("YouTube에서 로봇이 아닌지 확인을 요청했습니다",
               "로그인 없이 다운로드가 차단되었습니다. 보조 계정으로 로그인한 뒤 다시 시도하거나, 나중에 다른 네트워크에서 "
               "시도하세요."),
    },
    "signin-expired": {
        "en": ("Your saved YouTube sign-in no longer works", "Sign in again, then try again."),
        "zh-TW": ("已保存的 YouTube 登入已失效", "請重新登入後再試。"),
        "ja": ("保存された YouTube のログインが無効になりました", "もう一度ログインしてから再試行してください。"),
        "ko": ("저장된 YouTube 로그인이 더 이상 작동하지 않습니다", "다시 로그인한 뒤 다시 시도하세요."),
    },
    # apps without action buttons: the import opens the sign-in window itself
    "signin-expired-import": {
        "en": ("Your saved YouTube sign-in no longer works",
               "Import again: the sign-in window opens again (the warning first), where you can sign in again."),
        "zh-TW": ("已保存的 YouTube 登入已失效", "請再匯入一次，會重新開啟登入視窗（先顯示警告），在裡面重新登入即可。"),
        "ja": ("保存された YouTube のログインが無効になりました",
               "もう一度取り込んでください。ログインウィンドウが再び開く（最初に警告が表示されます）ので、そこで"
               "ログインし直せます。"),
        "ko": ("저장된 YouTube 로그인이 더 이상 작동하지 않습니다",
               "다시 가져오세요. 로그인 창이 다시 열리며(먼저 경고가 표시됨), 거기서 다시 로그인할 수 있습니다."),
    },
    "signin-browser": {
        "en": ("Signing in to YouTube needs Chrome or Edge",
               "Install Google Chrome or Microsoft Edge and try again, or try another network later."),
        "zh-TW": ("登入 YouTube 需要 Chrome 或 Edge",
                  "請安裝 Google Chrome 或 Microsoft Edge 後再試，或稍後換個網路。"),
        "ja": ("YouTube へのログインには Chrome か Edge が必要です",
               "Google Chrome か Microsoft Edge をインストールしてもう一度試すか、後で別のネットワークで試してください。"),
        "ko": ("YouTube 로그인에는 Chrome 또는 Edge가 필요합니다",
               "Google Chrome 또는 Microsoft Edge를 설치한 뒤 다시 시도하거나, 나중에 다른 네트워크에서 시도하세요."),
    },
    "signin-closed": {
        "en": ("YouTube sign-in was cancelled",
               "The sign-in window was closed before you signed in. Try again whenever you like."),
        "zh-TW": ("YouTube 登入已取消", "登入視窗在登入前就被關閉了，需要時可以再試一次。"),
        "ja": ("YouTube へのログインがキャンセルされました",
               "ログインする前にログインウィンドウが閉じられました。必要なときにもう一度試してください。"),
        "ko": ("YouTube 로그인이 취소되었습니다", "로그인하기 전에 로그인 창이 닫혔습니다. 필요할 때 다시 시도하세요."),
    },
    "signin-timeout": {
        "en": ("YouTube sign-in took too long",
               "Nobody signed in to YouTube in the sign-in window within 15 minutes, so it was closed. Try again "
               "whenever you like."),
        "zh-TW": ("YouTube 登入等太久了", "15 分鐘內沒有在登入視窗登入 YouTube，所以視窗已關閉。需要時再試一次即可。"),
        "ja": ("YouTube へのログインに時間がかかりすぎました",
               "15 分以内にログインウィンドウで YouTube にログインされなかったため、ウィンドウを閉じました。"
               "必要なときにもう一度試してください。"),
        "ko": ("YouTube 로그인 대기 시간이 초과되었습니다",
               "15분 안에 로그인 창에서 YouTube에 로그인하지 않아 창을 닫았습니다. 필요할 때 다시 시도하세요."),
    },
    "signin-failed": {
        "en": ("Couldn’t open the YouTube sign-in window", "{browser} didn’t start for signing in. Try again."),
        "zh-TW": ("無法開啟 YouTube 登入視窗", "{browser} 沒有成功啟動來登入，請再試一次。"),
        "ja": ("YouTube のログインウィンドウを開けませんでした", "ログイン用に {browser} を起動できませんでした。もう一度試してください。"),
        "ko": ("YouTube 로그인 창을 열지 못했습니다", "로그인을 위해 {browser} 브라우저를 시작하지 못했습니다. 다시 시도하세요."),
    },
    "account-cap": {
        "en": ("Reached today’s {cap} downloads with your YouTube sign-in",
               "Stopping for today is recommended so the account isn’t restricted. To continue anyway, import again "
               "and choose “I understand the risk, continue anyway” in the window that opens."),
        "zh-TW": ("今天用帳號下載已達 {cap} 首",
                  "建議今天先停，以免帳號被限制。如果仍要繼續，請再匯入一次，在跳出的視窗選擇「我了解風險，仍要繼續」。"),
        "ja": ("今日のアカウントでのダウンロードが {cap} 曲に達しました",
               "アカウントが制限されないよう、今日はここでやめることをおすすめします。それでも続ける場合は、もう一度取り込み、"
               "開いたウィンドウで「リスクを理解した上で続ける」を選んでください。"),
        "ko": ("오늘 계정으로 {cap}곡을 다운로드했습니다",
               "계정이 제한되지 않도록 오늘은 여기서 멈추는 것을 권장합니다. 그래도 계속하려면 다시 가져온 뒤 열리는 창에서 "
               "“위험을 이해했으며 계속합니다”를 선택하세요."),
    },
    "account-paused": {
        "en": ("Stopped using your YouTube sign-in for 24 hours",
               "YouTube still blocked the download with the sign-in, or reported trouble with that account, so the "
               "extension won’t use the account until {until}. Open YouTube with that account in a browser to check "
               "it’s fine, and don’t keep retrying. Imports still try without the sign-in meanwhile; another network "
               "may work."),
        "zh-TW": ("暫停使用 YouTube 登入 24 小時",
                  "登入後 YouTube 仍然擋下下載，或回報這個帳號有問題，所以擴充功能在 {until} 之前不會再使用這個帳號。"
                  "請用瀏覽器以該帳號開啟 YouTube 確認帳號正常，不要一直重試。這段期間匯入仍會在不登入的情況下嘗試；"
                  "換一個網路也可能可以。"),
        "ja": ("YouTube のログインの使用を 24 時間停止しました",
               "ログインしても YouTube にダウンロードをブロックされたか、アカウントの問題が報告されたため、{until} まで"
               "拡張機能はこのアカウントを使いません。ブラウザでそのアカウントの YouTube を開いて問題がないか確認し、"
               "再試行を繰り返さないでください。その間もログインなしでの取り込みは試みます。別のネットワークなら使える"
               "ことがあります。"),
        "ko": ("YouTube 로그인 사용을 24시간 중지했습니다",
               "로그인해도 YouTube가 다운로드를 차단했거나 해당 계정에 문제가 있다고 알려 와서, {until}까지 확장 프로그램은 "
               "이 계정을 사용하지 않습니다. 브라우저에서 그 계정으로 YouTube를 열어 문제가 없는지 확인하고, 계속 다시 "
               "시도하지 마세요. 그동안 가져오기는 로그인 없이 시도합니다. 다른 네트워크에서는 될 수도 있습니다."),
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
        "ko": ("YouTube의 변경 사항을 yt-dlp가 아직 처리하지 못합니다",
               "현재 yt-dlp {version} 버전으로는 이 동영상을 다운로드할 수 없습니다. 수정 버전이 나오면 확장 프로그램이 "
               "yt-dlp를 자동으로 업데이트합니다. 잠시 후 다시 시도하세요."),
    },
}

# What else works against a bot check that doesn't pass (the sign-in is offered as the error's action)
BLOCKED_HINTS: Dict[str, Dict[str, str]] = {
    "network": {
        "en": "Switch to another network (a phone hotspot), turn off any VPN or proxy, or wait a few hours.",
        "zh-TW": "請換一個網路（例如手機熱點）、關閉 VPN 或 Proxy，或等幾個小時再試。",
        "ja": "別のネットワーク（スマホのテザリングなど）に切り替える、VPN やプロキシをオフにする、数時間待つ、"
              "のいずれかを試してください。",
        "ko": "다른 네트워크(휴대폰 핫스팟 등)로 바꾸거나, VPN이나 프록시를 끄거나, 몇 시간 기다려 보세요.",
    },
}

# the error's action: a button in the import panel (apps with features ["actions"])
ACTION_SIGNIN: Dict[str, Tuple[str, str]] = {  # label, hint
    "en": ("Sign in in a browser window, then try again",
           "A separate window opens for you to sign in to YouTube. Use a secondary account, not your main one."),
    "zh-TW": ("開啟網頁登入後再次嘗試", "會開啟一個獨立視窗讓你登入 YouTube，請用另外申請的小帳號，不要用主帳號。"),
    "ja": ("ブラウザでログインしてからもう一度試す",
           "YouTube にログインするための別ウィンドウが開きます。メインではなく、別に作成したサブアカウントを使ってください。"),
    "ko": ("브라우저에서 로그인한 뒤 다시 시도",
           "YouTube에 로그인할 별도 창이 열립니다. 주 계정이 아닌 별도로 만든 보조 계정을 사용하세요."),
}

# progress messages, shown by the app while an import runs
PROGRESS: Dict[str, Dict[str, str]] = {
    "signin-done": {
        "en": "Signed in to YouTube.",
        "zh-TW": "已登入 YouTube。",
        "ja": "YouTube にログインしました。",
        "ko": "YouTube에 로그인했습니다.",
    },
    "signout-done": {
        "en": "Signed out of YouTube; the sign-in window’s profile was deleted.",
        "zh-TW": "已登出 YouTube，並刪除登入視窗的設定檔。",
        "ja": "YouTube からログアウトし、ログインウィンドウのプロファイルを削除しました。",
        "ko": "YouTube에서 로그아웃하고 로그인 창의 프로필을 삭제했습니다.",
    },
    "account-try": {
        "en": "Blocked without a sign-in; trying once with your YouTube sign-in…",
        "zh-TW": "不登入時被擋下，改用你的 YouTube 登入狀態試一次…",
        "ja": "ログインなしではブロックされたため、YouTube のログインで 1 回試しています…",
        "ko": "로그인 없이 차단되어 YouTube 로그인으로 한 번 시도하는 중…",
    },
    "account-retry": {
        "en": "Still blocked with the sign-in; one last try in {delay} s…",
        "zh-TW": "登入後仍被擋下，{delay} 秒後再試最後一次…",
        "ja": "ログインしてもブロックされました。{delay} 秒後に最後にもう 1 回試します…",
        "ko": "로그인해도 차단되었습니다. {delay}초 후 마지막으로 한 번 더 시도합니다…",
    },
    "account-ask": {
        "en": "Reached today’s {cap} downloads with your YouTube sign-in: choose in the {browser} window whether to "
              "stop for today or continue…",
        "zh-TW": "今天用帳號下載已達 {cap} 首，請在跳出的 {browser} 視窗選擇今天先停或繼續…",
        "ja": "今日のアカウントでのダウンロードが {cap} 曲に達しました。開いた {browser} のウィンドウで、今日はやめるか"
              "続けるかを選んでください…",
        "ko": "오늘 계정으로 {cap}곡을 다운로드했습니다. 열린 {browser} 창에서 오늘은 멈출지 계속할지 선택하세요…",
    },
    "account-wait": {
        "en": "Waiting for another download with your YouTube sign-in to finish…",
        "zh-TW": "等待另一個使用 YouTube 登入的下載完成…",
        "ja": "YouTube のログインを使う別のダウンロードが終わるのを待っています…",
        "ko": "YouTube 로그인을 사용하는 다른 다운로드가 끝나기를 기다리는 중…",
    },
    "signin-wait": {
        "en": "{main} Read the warning in the {browser} window that opened, sign in to YouTube there, then close "
              "the window. Waiting…",
        "zh-TW": "{main}請先看完剛開啟的 {browser} 視窗裡的警告，在裡面登入 YouTube，登入後關閉視窗。等待中…",
        "ja": "{main}開いた {browser} のウィンドウの警告を読んでから、そこで YouTube にログインし、ログインしたら"
              "ウィンドウを閉じてください。待っています…",
        "ko": "{main} 열린 {browser} 창의 경고를 읽은 뒤 거기서 YouTube에 로그인하고, 로그인했으면 창을 닫으세요. "
              "기다리는 중…",
    },
    "retry": {
        "en": "YouTube asked to confirm this isn’t a bot; trying again in {delay} s ({attempt}/{total})…",
        "zh-TW": "YouTube 要求確認不是機器人，{delay} 秒後重試（{attempt}/{total}）…",
        "ja": "YouTube がボットではないことの確認を求めています。{delay} 秒後に再試行します（{attempt}/{total}）…",
        "ko": "YouTube에서 로봇이 아닌지 확인을 요청했습니다. {delay}초 후 다시 시도합니다({attempt}/{total})…",
    },
    "updating": {
        "en": "Updating yt-dlp {current} → {latest}…",
        "zh-TW": "正在更新 yt-dlp {current} → {latest}…",
        "ja": "yt-dlp を更新しています {current} → {latest}…",
        "ko": "yt-dlp 업데이트 중 {current} → {latest}…",
    },
    "deno": {
        "en": "Downloading the JavaScript runtime for YouTube (deno {version}, about 40 MB)…",
        "zh-TW": "正在下載 YouTube 需要的 JavaScript 執行環境（deno {version}，約 40 MB）…",
        "ja": "YouTube 用の JavaScript ランタイムをダウンロードしています（deno {version}、約 40 MB）…",
        "ko": "YouTube에 필요한 JavaScript 런타임을 다운로드하는 중(deno {version}, 약 40MB)…",
    },
}

_KINDS: List[Tuple[str, Tuple[str, ...]]] = [
    # the sign-in's own cookies.txt (handled as an expired sign-in)
    ("cookies", ("cookieloaderror", "cookies database", "cookie database", "failed to load cookies",
                 "netscape format cookies file")),
    ("blocked", ("not a bot",)),
    ("changed", ("requested format is not available", "signature extraction failed", "nsig extraction failed",
                 "only images are available", "http error 403")),
]


class YouTubeError(Exception):
    """A yt-dlp failure explained for the user (the original error is chained for the technical details)."""


def _lang_of(tag: str) -> str:
    tag = tag.lower()
    for prefix, lang in (("zh", "zh-TW"), ("ja", "ja"), ("ko", "ko")):
        if tag.startswith(prefix):
            return lang
    return "en"


def _desktop_app_language() -> str:
    """Apps before 0.3.0 don't pass their language: the desktop app's settings.json, a few folders above this
    extension's data folder, has the one the user picked ("" when there is none)."""
    folder = _context.get("dataDir")
    for _ in range(6):
        if not folder:
            return ""
        try:
            with open(os.path.join(folder, "settings.json"), encoding="utf-8") as f:
                language = json.load(f).get("language")
            if isinstance(language, str):  # "auto" or anything else unknown: the system's language decides
                return language if language.lower()[:2] in ("en", "zh", "ja", "ko") else ""
        except (OSError, ValueError, AttributeError):
            pass
        parent = os.path.dirname(folder)
        folder = parent if parent != folder else ""
    return ""


def _lang() -> str:
    """The app's language (context["locale"], from newer apps), else the system's display language."""
    if _context.get("locale"):
        return _lang_of(str(_context["locale"]))
    tag = _desktop_app_language()
    if tag:
        return _lang_of(tag)
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
    return _lang_of(tag)


def _text(key: str, **fmt: Any) -> str:
    """A progress message in the user's language."""
    return PROGRESS[key][_lang()].format(**fmt)


def _app_shows_class() -> bool:
    """Apps before 0.3.0 show an error as "<exception class>: <message>" (context["appVersion"]; "dev": a checkout)."""
    version = str(_context.get("appVersion") or "")
    return version != "dev" and _vtuple(version) < (0, 3, 0)


def _host_has_actions() -> bool:
    """The app shows an error's `action` as a button and calls action() (context["features"])."""
    return "actions" in (_context.get("features") or [])


def error_kind(exc: BaseException) -> Optional[str]:
    low = f"{type(exc).__name__}: {exc}".lower().replace("’", "'")
    for kind, keys in _KINDS:
        if any(k in low for k in keys):
            return kind
    return None


def readable_error(kind: str, exc: BaseException, again: bool = False, browser: Optional[str] = None,
                   extra: Optional[Dict[str, Any]] = None, action: Optional[str] = None) -> YouTubeError:
    """`action`: the error's action for the import panel (see "Actions")."""
    lang = _lang()
    title, body = MESSAGES["blocked-again" if kind == "blocked" and again else kind][lang]
    if kind == "blocked":
        # Chinese and Japanese sentences follow each other without a space
        body = ("" if lang in ("zh-TW", "ja") else " ").join([body, BLOCKED_HINTS["network"][lang]])
    fmt = {"browser": browser or "", "version": _active_version() or "?", "main": MAIN_ACCOUNT[lang], **(extra or {})}
    title, text = title.format(**fmt), body.format(**fmt)
    sentence = title + ("：" if lang in ("zh-TW", "ja") else ": ") + text
    if _app_shows_class():
        # a subclass named after the title, so the app's "<class>: <message>" reads as a sentence
        err: YouTubeError = type(title, (YouTubeError,), {})(text)
    else:
        err = YouTubeError(sentence)
    err.kind = kind  # type: ignore[attr-defined]
    err.sentence = sentence  # type: ignore[attr-defined]  # the whole message, for action() results
    if action == "signin":
        label, hint = ACTION_SIGNIN[lang]
        err.action = {"id": "signin", "label": label, "hint": hint}  # type: ignore[attr-defined]
    err.__cause__ = exc
    return err


# ---------------------------------------------------------------------------
# Sign-in in a window of its own
#
# When an import is blocked without a sign-in, the import panel offers "sign in" (the error's action; in apps
# without actions the import does it itself). It opens Chrome (or Edge) with a profile that belongs to the extension
# (<dataDir>/signin/profile, never the user's usual profile). It first shows a warning page, served on 127.0.0.1 by
# the extension, that the user must accept (a checkbox: "I'm using a secondary account, not my main one") before it
# goes on to YouTube's sign-in page; closing the window cancels. The user signs in there themselves; the extension
# waits until youtube.com has a signed-in session, asks the browser for its youtube.com / google.com cookies over the
# DevTools protocol (Chrome allows remote debugging only on a profile like this one), writes them to
# <dataDir>/signin/cookies.dat (sealed) and closes the window. Nothing is read before the warning is accepted.
#
# yt-dlp then downloads as that account and saves the cookies YouTube refreshes back to the file (through a copy that
# replaces it in one step), so the sign-in lasts across imports, app restarts and extension updates (the app keeps
# dataDir). When YouTube stops accepting it, the error offers the sign-in again (warning first); the kept profile is
# usually still signed in, so no password is needed. Only sign-out (the "signout" action) deletes the profile and
# the file. Cookie values are never logged or put in messages.
# ---------------------------------------------------------------------------

SIGNIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F"
SIGNIN_TIMEOUT_SEC = 15 * 60
SIGNIN_POLL_SEC = 2
SIGNIN_PROGRESS_EVERY_SEC = 20  # the app gives up on a call that stays quiet too long
SIGNIN_START_SEC = 30
SIGNIN_COOKIE_DOMAINS = ("youtube.com", "google.com")
SIGNED_IN_COOKIES = ("SAPISID", "__Secure-3PAPISID", "__Secure-3PSID")
WORK_COPY_STALE_SEC = 3600  # a yt-dlp call's readable copy of the cookies left by a call that was killed

# shown on the warning page and at the start of the sign-in progress message
MAIN_ACCOUNT: Dict[str, str] = {
    "en": "Don’t sign in with your main account: downloading with an account may make YouTube restrict or disable it. "
          "Use a separately created secondary account.",
    "zh-TW": "請勿登入主帳號：用帳號下載可能讓 YouTube 限制或停用該帳號，請使用另外申請的小帳號。",
    "ja": "メインのアカウントでログインしないでください。アカウントでダウンロードすると、YouTube がそのアカウントを制限"
          "または停止することがあります。別に作成したサブアカウントを使ってください。",
    "ko": "주 계정으로 로그인하지 마세요. 계정으로 다운로드하면 YouTube가 해당 계정을 제한하거나 정지할 수 있습니다. "
          "별도로 만든 보조 계정을 사용하세요.",
}

# asked in the sign-in window when today's ACCOUNT_DAILY_CAP is reached
CAP_PAGE: Dict[str, Dict[str, str]] = {
    "en": {
        "heading": "Reached today’s {cap} downloads with your YouTube sign-in",
        "recommend": "Stopping for today is recommended, so the account isn’t restricted.",
        "risk": "The more an account downloads, the more likely YouTube is to restrict or disable it.",
        "note": "If you continue, you won’t be asked again today; the count starts over tomorrow. Closing this window "
                "stops for today. Signs of trouble still stop the account for 24 hours.",
        "stop": "Stop for today (recommended)",
        "continue": "I understand the risk, continue anyway",
    },
    "zh-TW": {
        "heading": "今天用帳號下載已達 {cap} 首",
        "recommend": "建議今天先停，以免帳號被限制。",
        "risk": "帳號下載得越多，YouTube 越可能限制或停用這個帳號。",
        "note": "選擇繼續的話，今天剩下的時間不會再詢問，明天會重新計算。關閉這個視窗等於今天先停。"
                "發現異常時仍會暫停使用帳號 24 小時。",
        "stop": "今天先停（建議）",
        "continue": "我了解風險，仍要繼續",
    },
    "ja": {
        "heading": "今日のアカウントでのダウンロードが {cap} 曲に達しました",
        "recommend": "アカウントが制限されないよう、今日はここでやめることをおすすめします。",
        "risk": "ダウンロードが多いほど、YouTube がアカウントを制限または停止する可能性が高くなります。",
        "note": "続ける場合、今日はもう確認しません。明日になると回数はリセットされます。このウィンドウを閉じると今日は"
                "やめます。問題の兆候があれば、引き続きアカウントの使用を 24 時間停止します。",
        "stop": "今日はやめる（おすすめ）",
        "continue": "リスクを理解した上で続ける",
    },
    "ko": {
        "heading": "오늘 계정으로 {cap}곡을 다운로드했습니다",
        "recommend": "계정이 제한되지 않도록 오늘은 여기서 멈추는 것을 권장합니다.",
        "risk": "다운로드가 많을수록 YouTube가 계정을 제한하거나 정지할 가능성이 커집니다.",
        "note": "계속하면 오늘은 더 묻지 않으며, 내일 횟수가 다시 시작됩니다. 이 창을 닫으면 오늘은 멈춥니다. 문제 징후가 "
                "보이면 여전히 계정 사용을 24시간 중지합니다.",
        "stop": "오늘은 멈추기(권장)",
        "continue": "위험을 이해했으며 계속합니다",
    },
}

WARNING_PAGE: Dict[str, Dict[str, str]] = {
    "en": {
        "heading": "Don’t sign in with your main account",
        "risk": "The KAREOKIE YouTube extension downloads as the account you sign in with here. YouTube may restrict "
                "or disable accounts that download. If it is your main Google account, its Gmail, photos and files "
                "could be affected too.",
        "use": "Use a separately created secondary account.",
        "limits": "The extension keeps account use to a minimum (only when YouTube blocks a download without it, "
                  "and it asks you after {cap} a day), but can’t guarantee the account won’t be restricted.",
        "note": "This window uses a browser profile of the extension’s own, separate from your usual one. Once "
                "YouTube shows you signed in, close the window: the extension then saves the sign-in.",
        "check": "I understand the risk, and I’m using a separately created secondary account",
        "continue": "Continue to sign-in",
    },
    "zh-TW": {
        "heading": "請勿登入主帳號",
        "risk": "KAREOKIE 的 YouTube 擴充功能會用你在這裡登入的帳號下載。用帳號下載可能讓 YouTube 限制或停用該帳號；"
                "如果是你的主要 Google 帳號，同一個帳號裡的 Gmail、相片和雲端硬碟也可能受影響。",
        "use": "請使用另外申請的小帳號。",
        "limits": "擴充功能會盡量少用這個帳號（只在不登入下載被擋時才用，每天超過 {cap} 次會先問你），但無法保證帳號不會被限制。",
        "note": "這個視窗使用擴充功能專用的瀏覽器設定檔，和你平常用的分開。YouTube 顯示已登入後，關閉這個視窗，"
                "擴充功能就會保存登入狀態。",
        "check": "我了解風險，我使用的是另外申請的小帳號",
        "continue": "繼續前往登入",
    },
    "ja": {
        "heading": "メインのアカウントでログインしないでください",
        "risk": "KAREOKIE の YouTube 拡張機能は、ここでログインしたアカウントでダウンロードします。アカウントでダウンロード"
                "すると、YouTube がそのアカウントを制限または停止することがあります。メインの Google アカウントだと、"
                "同じアカウントの Gmail、フォト、ドライブにも影響する可能性があります。",
        "use": "別に作成したサブアカウントを使ってください。",
        "limits": "拡張機能はこのアカウントの使用を最小限にします（ログインなしのダウンロードがブロックされたときだけ、"
                  "1 日 {cap} 回を超えると確認します）が、制限されないことは保証できません。",
        "note": "このウィンドウは普段とは別の、拡張機能専用のブラウザプロファイルを使っています。YouTube で"
                "ログインできたらウィンドウを閉じてください。拡張機能がログイン状態を保存します。",
        "check": "リスクを理解しており、別に作成したサブアカウントを使います",
        "continue": "ログインに進む",
    },
    "ko": {
        "heading": "주 계정으로 로그인하지 마세요",
        "risk": "KAREOKIE의 YouTube 확장 프로그램은 여기서 로그인한 계정으로 다운로드합니다. 계정으로 다운로드하면 YouTube가 "
                "해당 계정을 제한하거나 정지할 수 있습니다. 주 Google 계정이라면 같은 계정의 Gmail, 사진, 드라이브에도 "
                "영향이 있을 수 있습니다.",
        "use": "별도로 만든 보조 계정을 사용하세요.",
        "limits": "확장 프로그램은 이 계정 사용을 최소화하지만(로그인 없이 다운로드가 차단될 때만, 하루 {cap}회가 넘으면 확인) "
                  "계정이 제한되지 않는다고 보장할 수는 없습니다.",
        "note": "이 창은 평소 프로필과 분리된 확장 프로그램 전용 브라우저 프로필을 사용합니다. YouTube에 로그인되면 "
                "창을 닫으세요. 그러면 확장 프로그램이 로그인 상태를 저장합니다.",
        "check": "위험을 이해했으며, 별도로 만든 보조 계정을 사용합니다",
        "continue": "로그인 계속",
    },
}


class SignInClosed(Exception):
    pass


def _signin_dir() -> Optional[str]:
    data = _context.get("dataDir")
    return os.path.join(data, "signin") if data else None


def _refresh_marker() -> str:
    """Present when YouTube stopped accepting the sign-in: the next import opens the window again."""
    return os.path.join(_signin_dir() or "", "refresh")


def _dpapi(data: bytes, seal: bool) -> bytes:
    """Windows' data protection: only this Windows user on this PC can open what it sealed."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    call = crypt32.CryptProtectData if seal else crypt32.CryptUnprotectData
    call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                     wintypes.DWORD, ctypes.POINTER(Blob)]
    call.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source = ctypes.create_string_buffer(data, len(data))
    given = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    if not call(ctypes.byref(given), None, None, None, None, 1, ctypes.byref(out)):  # 1: never show a prompt
        raise OSError(ctypes.get_last_error(), "Windows data protection refused")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _cookies_path() -> str:
    return os.path.join(_signin_dir() or "", "cookies.dat")


def _write_private(path: str, data: bytes) -> None:
    """In one step, and readable by this user only."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _write_cookies(text: str) -> None:
    """The sign-in's cookies at rest: sealed on Windows; elsewhere a file only this user can read."""
    data = text.encode("utf-8")
    _write_private(_cookies_path(), _dpapi(data, True) if sys.platform == "win32" else data)


def _read_cookies() -> str:
    with open(_cookies_path(), "rb") as f:
        data = f.read()
    return (_dpapi(data, False) if sys.platform == "win32" else data).decode("utf-8")


def _sweep_work_copies() -> None:
    for old in glob.glob(os.path.join(_signin_dir() or "", "cookies.*.work")):
        try:
            if time.time() - os.path.getmtime(old) > WORK_COPY_STALE_SEC:
                os.remove(old)
        except OSError:
            pass


def _signin_cookies() -> Optional[str]:
    """The saved sign-in's file, or None. A plain cookies.txt from an earlier 1.0.5 test build is sealed and removed.
    A file this user can't open (copied from another PC or user) counts as no sign-in."""
    folder = _signin_dir()
    if not folder or not os.path.isdir(folder):
        return None
    _sweep_work_copies()
    path = _cookies_path()
    plain = os.path.join(folder, "cookies.txt")
    if os.path.isfile(plain):
        try:
            with open(plain, encoding="utf-8") as f:
                _write_cookies(f.read())
            os.remove(plain)
        except (OSError, ValueError):
            pass
    if not os.path.isfile(path):
        return None
    try:
        _read_cookies()
    except (OSError, ValueError):
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return path


def sign_out() -> None:
    folder = _signin_dir()
    if folder and os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
        print("[sign-in] signed out: deleted the sign-in profile and cookies")


def _chromium() -> Optional[Tuple[str, str]]:
    """(name, executable) of the installed Chrome, else Edge."""
    if sys.platform == "win32":
        roots = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        candidates = [("Chrome", os.path.join(r, "Google", "Chrome", "Application", "chrome.exe")) for r in roots]
        candidates += [("Edge", os.path.join(r, "Microsoft", "Edge", "Application", "msedge.exe")) for r in roots]
    elif sys.platform == "darwin":
        candidates = [(name, os.path.join(home, f"{app}.app", "Contents", "MacOS", app))
                      for name, app in (("Chrome", "Google Chrome"), ("Edge", "Microsoft Edge"))
                      for home in ("/Applications", os.path.expanduser("~/Applications"))]
    else:
        candidates = [(name, shutil.which(exe) or "") for name, exe in (
            ("Chrome", "google-chrome"), ("Chrome", "google-chrome-stable"), ("Chrome", "chromium"),
            ("Edge", "microsoft-edge"))]
    return next(((name, exe) for name, exe in candidates if exe and os.path.isfile(exe)), None)


class _WebSocket:
    """The little of RFC 6455 the DevTools protocol on 127.0.0.1 needs (text frames, standard library only)."""

    def __init__(self, url: str, timeout: float = 10) -> None:
        import base64
        import socket
        import urllib.parse

        u = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                          .encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("the browser closed the DevTools connection")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.split(b"\r\n", 1)[0].decode("latin-1"))
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def _exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("the browser closed the DevTools connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode: int, data: bytes) -> None:
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode("utf-8"))

    def recv(self) -> str:
        message = b""
        while True:
            b1, b2 = self._exact(2)
            n = b2 & 0x7F
            if n == 126:
                n = int.from_bytes(self._exact(2), "big")
            elif n == 127:
                n = int.from_bytes(self._exact(8), "big")
            mask = self._exact(4) if b2 & 0x80 else b""
            data = self._exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode = b1 & 0x0F
            if opcode == 0x8:
                raise ConnectionError("the browser closed the DevTools connection")
            if opcode == 0x9:
                self._frame(0xA, data)
                continue
            if opcode in (0x0, 0x1, 0x2):
                message += data
                if b1 & 0x80:
                    return message.decode("utf-8")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _cdp(ws_url: str, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One DevTools protocol command on the browser target."""
    ws = _WebSocket(ws_url)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            reply = json.loads(ws.recv())
            if reply.get("id") == 1:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result") or {}
    finally:
        ws.close()


def _page_html(lang: str, title: str, body: str, color: str, script: str = "") -> str:
    """A full-window page of the sign-in window (the text in `body` is escaped by the caller)."""
    return f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
html, body {{ margin: 0; min-height: 100%; background: {color}; color: #fff; }}
body {{ font: 18px/1.6 system-ui, "Segoe UI", "Microsoft JhengHei", "Yu Gothic UI", "Malgun Gothic", sans-serif;
       display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
main {{ max-width: 760px; padding: 48px 32px; }}
h1 {{ font-size: 48px; line-height: 1.2; margin: 0 0 24px; }}
.icon {{ font-size: 72px; line-height: 1; }}
p {{ margin: 0 0 16px; }}
.use {{ font-size: 24px; font-weight: 700; }}
.note {{ opacity: .85; font-size: 16px; }}
label {{ display: flex; gap: 12px; align-items: flex-start; margin: 28px 0; padding: 16px; font-weight: 700;
        background: rgba(0,0,0,.25); border: 2px solid #fff; border-radius: 8px; cursor: pointer; }}
input[type=checkbox] {{ width: 24px; height: 24px; flex: none; margin-top: 3px; }}
.buttons {{ display: flex; flex-wrap: wrap; gap: 16px; margin-top: 28px; }}
button {{ font: inherit; font-weight: 700; padding: 14px 28px; border: 0; border-radius: 8px;
         background: #fff; color: {color}; cursor: pointer; }}
button.secondary {{ background: transparent; color: #fff; border: 2px solid rgba(255,255,255,.7); }}
button:disabled {{ background: rgba(255,255,255,.35); color: rgba(255,255,255,.8); cursor: not-allowed; }}
</style></head>
<body><main>
<div class="icon" aria-hidden="true">&#9888;</div>
{body}
</main>{script}
</body></html>
"""


def _warning_html(lang: str) -> str:
    import html

    t = {k: html.escape(v.format(cap=ACCOUNT_DAILY_CAP)) for k, v in WARNING_PAGE[lang].items()}
    return _page_html(lang, t["heading"], f"""<h1>{t["heading"]}</h1>
<p>{t["risk"]}</p>
<p class="use">{t["use"]}</p>
<p>{t["limits"]}</p>
<p class="note">{t["note"]}</p>
<form method="post" action="choose">
<label><input type="checkbox" id="understood" name="understood" value="yes" required> {t["check"]}</label>
<button type="submit" id="continue" name="choice" value="continue">{t["continue"]}</button>
</form>""", "#b00020", """
<script>
const box = document.getElementById("understood"), button = document.getElementById("continue");
button.disabled = !box.checked;
box.addEventListener("change", () => { button.disabled = !box.checked; });
</script>""")


def _cap_html(lang: str) -> str:
    import html

    t = {k: html.escape(v.format(cap=ACCOUNT_DAILY_CAP)) for k, v in CAP_PAGE[lang].items()}
    return _page_html(lang, t["heading"], f"""<h1>{t["heading"]}</h1>
<p class="use">{t["recommend"]}</p>
<p>{t["risk"]}</p>
<p class="note">{t["note"]}</p>
<form method="post" action="choose" class="buttons">
<button type="submit" id="stop" name="choice" value="stop" autofocus>{t["stop"]}</button>
<button type="submit" id="continue" name="choice" value="continue" class="secondary">{t["continue"]}</button>
</form>""", "#a34700")


class _LocalPage:
    """A page of the sign-in window on 127.0.0.1 under a random path, and what the user chose on it: `then` maps
    each choice to where the window goes next ("done": a blank page; the extension closes the window). The choice
    "continue" also needs the form field `require` (a checkbox), checked here too."""

    def __init__(self, page: str, then: Dict[str, str], require: str = "") -> None:
        import http.server
        import secrets
        import threading
        import urllib.parse

        token = secrets.token_urlsafe(16)
        body = page.encode("utf-8")
        self.choice: Optional[str] = None
        self.chosen = threading.Event()
        me = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status: int, data: bytes = b"", headers: Optional[Dict[str, str]] = None) -> None:
                self.send_response(status)
                for k, v in {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                             "Content-Length": str(len(data)), **(headers or {})}.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == f"/{token}/":
                    self._send(200, body)
                elif self.path == f"/{token}/done":
                    self._send(200, b"<!doctype html><title>KAREOKIE</title>")
                else:
                    self._send(404)

            def do_POST(self) -> None:  # noqa: N802
                fields = urllib.parse.parse_qs(self.rfile.read(min(int(self.headers.get("Content-Length") or 0),
                                                                   4096)).decode("latin-1"))
                choice = (fields.get("choice") or [""])[0]
                if self.path != f"/{token}/choose":
                    self._send(404)
                elif choice not in then or (choice == "continue" and require and fields.get(require) != ["yes"]):
                    self._send(303, headers={"Location": f"/{token}/"})  # nothing chosen (box not ticked): again
                else:
                    me.choice = choice
                    me.chosen.set()
                    nxt = then[choice]
                    self._send(303, headers={"Location": f"/{token}/done" if nxt == "done" else nxt})

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/{token}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def accepted(self) -> bool:
        return self.choice == "continue"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _Warning(lang: str) -> _LocalPage:  # noqa: N802
    """The warning shown before every sign-in: continuing (with the box ticked) goes on to YouTube's sign-in page."""
    return _LocalPage(_warning_html(lang), {"continue": SIGNIN_URL}, require="understood")


BROWSER_LANG = {"en": "en-US", "zh-TW": "zh-TW", "ja": "ja", "ko": "ko"}


def _launch_browser(exe: str, profile: str, url: str, debug: bool = True, headless: bool = False) -> Any:
    """The browser on the extension's profile, in the user's language. `debug`: with a DevTools address (never for
    the sign-in window: Google refuses to sign in a browser it sees being driven)."""
    os.makedirs(profile, exist_ok=True)
    args = [exe, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            f"--lang={BROWSER_LANG[_lang()]}"]
    if debug:
        try:
            os.remove(os.path.join(profile, "DevToolsActivePort"))  # left by an earlier window
        except OSError:
            pass
        args.append("--remote-debugging-port=0")
    args += ["--headless=new", "--mute-audio"] if headless else ["--new-window"]
    return subprocess.Popen(args + [url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _profile_cookies(exe: str, profile: str, cancel: Optional[Callable[[], None]]) -> List[Dict[str, Any]]:
    """The cookies the profile saved, read by a headless browser on it after the sign-in window closed."""
    proc = _launch_browser(exe, profile, "about:blank", headless=True)
    ws_url = ""
    try:
        ws_url = _devtools_url(profile, proc, cancel)
        return _cdp(ws_url, "Storage.getCookies").get("cookies") or []
    finally:
        _close_browser(proc, ws_url)


def _devtools_url(profile: str, proc: Any, cancel: Optional[Callable[[], None]]) -> str:
    """The browser target's DevTools address, from the DevToolsActivePort file the browser writes in the profile."""
    port_file = os.path.join(profile, "DevToolsActivePort")
    end = time.monotonic() + SIGNIN_START_SEC
    while time.monotonic() < end:
        if cancel is not None:
            cancel()
        try:
            with open(port_file, encoding="utf-8") as f:
                port, path = f.read().split("\n")[:2]
            if port.strip() and path.strip():
                return f"ws://127.0.0.1:{int(port)}{path.strip()}"
        except (OSError, ValueError):
            pass
        if proc.poll() is not None:
            raise SignInClosed()
        time.sleep(0.3)
    raise RuntimeError(f"no DevToolsActivePort after {SIGNIN_START_SEC} s")


def _signed_in(cookies: List[Dict[str, Any]]) -> bool:
    """What yt-dlp counts as signed in: youtube.com's LOGIN_INFO and one of its SAPISID cookies."""
    names = {c.get("name") for c in cookies if str(c.get("domain", "")).endswith("youtube.com")}
    return "LOGIN_INFO" in names and bool(names & set(SIGNED_IN_COOKIES))


def _netscape(cookies: List[Dict[str, Any]]) -> str:
    """youtube.com and google.com cookies as a Netscape cookies.txt (the format yt-dlp's `cookiefile` reads)."""
    lines = ["# Netscape HTTP Cookie File", "# YouTube sign-in made in the KAREOKIE YouTube extension's window", ""]
    for c in cookies:
        domain = str(c.get("domain") or "")
        if not domain.lstrip(".").endswith(SIGNIN_COOKIE_DOMAINS):
            continue
        expires = 0 if c.get("session") or float(c.get("expires") or -1) < 0 else int(float(c["expires"]))
        lines.append("\t".join([
            ("#HttpOnly_" if c.get("httpOnly") else "") + domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(c.get("path") or "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(expires),
            str(c.get("name") or ""),
            str(c.get("value") or ""),
        ]))
    return "\n".join(lines) + "\n"


def _signed_in_now() -> bool:
    """A saved sign-in YouTube hasn't stopped accepting."""
    return _signin_cookies() is not None and not os.path.exists(_refresh_marker())


def ensure_signin(progress: Optional[Callable[..., None]] = None, cancel: Optional[Callable[[], None]] = None,
                  force: bool = False) -> Optional[str]:
    """Without a working sign-in (or with `force`): open the sign-in window (the warning first) and wait for the
    user. The cookies file (None: no data folder)."""
    folder = _signin_dir()
    if not folder:
        return None
    if _signed_in_now() and not force:
        return _signin_cookies()
    found = _chromium()
    if not found:
        raise readable_error("signin-browser", FileNotFoundError("Chrome / Edge"))
    name, exe = found
    profile = os.path.join(folder, "profile")
    os.makedirs(profile, exist_ok=True)
    print(f"[sign-in] opening {name} with the extension's own profile")
    lang = _lang()
    warning = _Warning(lang)
    proc: Any = None
    try:
        # a plain window, nothing attached: the user signs in, then closes it
        proc = _launch_browser(exe, profile, warning.url, debug=False)
        start = last_note = time.monotonic()
        note = _text("signin-wait", browser=name, main=MAIN_ACCOUNT[lang])
        if progress is not None:
            progress(None, note)
        while proc.poll() is None:
            if cancel is not None:
                cancel()
            if time.monotonic() - start > SIGNIN_TIMEOUT_SEC:
                raise readable_error("signin-timeout", TimeoutError(f"{SIGNIN_TIMEOUT_SEC} s"))
            if progress is not None and time.monotonic() - last_note > SIGNIN_PROGRESS_EVERY_SEC:
                progress(None, note)
                last_note = time.monotonic()
            _wait(SIGNIN_POLL_SEC, cancel)
        proc = None
        if not warning.accepted:  # nothing is read before the user accepted the warning
            raise SignInClosed()
        cookies = _profile_cookies(exe, profile, cancel)
        if not _signed_in(cookies):
            raise SignInClosed()
        _write_cookies(_netscape(cookies))
        path = _cookies_path()
        try:
            os.remove(_refresh_marker())
        except OSError:
            pass
        print("[sign-in] signed in: saved the sign-in's cookies")
        return path
    except SignInClosed as exc:
        raise readable_error("signin-closed", exc) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise readable_error("signin-failed", exc, browser=name) from exc
    finally:
        warning.close()
        _close_browser(proc, "")


def _close_browser(proc: Any, ws_url: str) -> None:
    if proc is not None and proc.poll() is None:
        if not ws_url:  # nothing to ask it to close with
            proc.terminate()
            return
        try:
            _cdp(ws_url, "Browser.close")
        except Exception:  # noqa: BLE001 - it may close before it answers
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - the window didn't close by itself
            proc.terminate()


def _ask_over_cap(progress: Optional[Callable[..., None]], cancel: Optional[Callable[[], None]]) -> bool:
    """Today's ACCOUNT_DAILY_CAP is reached: ask in the sign-in window whether to stop (recommended) or go on.
    True: go on. Closing the window or waiting it out means stop."""
    found = _chromium()
    folder = _signin_dir()
    if not found or not folder:
        return False
    name, exe = found
    lang = _lang()
    page = _LocalPage(_cap_html(lang), {"continue": "done", "stop": "done"})
    proc: Any = None
    ws_url = ""
    try:
        proc = _launch_browser(exe, os.path.join(folder, "profile"), page.url)
        ws_url = _devtools_url(os.path.join(folder, "profile"), proc, cancel)
        note = _text("account-ask", cap=ACCOUNT_DAILY_CAP, browser=name)
        start = last_note = time.monotonic()
        if progress is not None:
            progress(None, note)
        while not page.chosen.is_set():
            if cancel is not None:
                cancel()
            if proc.poll() is not None or time.monotonic() - start > SIGNIN_TIMEOUT_SEC:
                return False
            if progress is not None and time.monotonic() - last_note > SIGNIN_PROGRESS_EVERY_SEC:
                progress(None, note)
                last_note = time.monotonic()
            _wait(0.5, cancel)
        print(f"[account] today's cap: the user chose {page.choice}")
        return page.accepted
    except SignInClosed:
        return False
    except (OSError, RuntimeError, ValueError) as exc:
        raise readable_error("signin-failed", exc, browser=name) from exc
    finally:
        page.close()
        _close_browser(proc, ws_url)


def _cookie_work_copy() -> str:
    """A readable copy of the cookies for one yt-dlp call, which needs a cookies.txt and rewrites it in place when it
    closes. Removed when the call ends (_keep_cookies), or by the sweep if the call was killed."""
    folder = _signin_dir() or ""
    _sweep_work_copies()
    work = os.path.join(folder, f"cookies.{os.getpid()}.{os.urandom(4).hex()}.work")
    _write_private(work, _read_cookies().encode("utf-8"))
    return work


def _keep_cookies(work: str) -> None:
    """Seal the cookies yt-dlp saved (YouTube refreshes them) in place of the kept ones, in one step. Not after a
    sign-out, and not a file a killed or failed save left empty or broken."""
    try:
        with open(work, encoding="utf-8") as f:
            text = f.read()
        ok = any(line.split("\t")[0].removeprefix("#HttpOnly_").lstrip(".").endswith("youtube.com")
                 and line.count("\t") == 6 for line in text.splitlines())
        if ok and os.path.isfile(_cookies_path()):
            _write_cookies(text)
    except (OSError, ValueError):
        pass
    try:
        os.remove(work)
    except OSError:
        pass


class _SignedInYDL:
    """A YoutubeDL using a readable copy of the sign-in's cookies; what it saves is sealed again when it is done."""

    def __init__(self, ydl: Any, work: str) -> None:
        self.ydl = ydl
        self.work = work

    def __enter__(self) -> Any:
        return self.ydl.__enter__()

    def __exit__(self, *exc: Any) -> Any:
        try:
            return self.ydl.__exit__(*exc)
        finally:
            _keep_cookies(self.work)


# ---------------------------------------------------------------------------
# yt-dlp options shared by every call
# ---------------------------------------------------------------------------


def _ydl(opts: Dict[str, Any], account: bool = False, logger: Any = None) -> Any:
    """A YoutubeDL. `account`: a call with the sign-in window's account (see "Using the account sparingly")."""
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
    # (settings from 1.0.2-1.0.4, cookiesFromBrowser and cookiesFile, are ignored: the sign-in window replaced them)
    if account:
        work = _cookie_work_copy()
        base["cookiefile"] = work
        base["sleep_interval_requests"] = ACCOUNT_SLEEP_REQUESTS_SEC  # gently, one request at a time
        if logger is not None:
            base["logger"] = logger
        return _SignedInYDL(yt_dlp.YoutubeDL({**base, **opts}), work)
    return yt_dlp.YoutubeDL({**base, **opts})


# YouTube's "confirm you're not a bot" check is often a passing flag on one request: the same import asked for
# again 10-40 s later usually goes through. So a blocked call waits and tries again before it is reported.
# Imports have time for the full wait; searches and metadata (the app gives them 40 s) try once more.
# Some blocks don't pass, though: one video can be blocked on every try while others play from the same network,
# and nothing in the error tells the two apart. So an import blocked through every retry is remembered for
# BLOCKED_REMEMBER_SEC, and importing that video again tries once and reports what works without the wait.
BLOCKED_RETRY_DELAYS_SEC = (5, 15, 30)
BLOCKED_RETRY_DELAYS_QUICK_SEC = (3,)
BLOCKED_REMEMBER_SEC = 6 * 3600


def _cancellable(fn: Callable[[], Any], cancel: Optional[Callable[[], None]]) -> Any:
    """Run fn in a thread while this one polls cancel(): yt-dlp only notices a cancel in its download hook, not while
    it reads YouTube's pages, and the app kills a call that keeps running 3 s after a cancel."""
    if cancel is None:
        return fn()
    import threading

    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)  # a cancelled call's process ends with it
    worker.start()
    while worker.is_alive():
        cancel()
        worker.join(0.2)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _wait(seconds: float, cancel: Optional[Callable[[], None]]) -> None:
    """Sleep, polling the app's cancel() so a cancelled import stops right away."""
    end = time.monotonic() + seconds
    while True:
        if cancel is not None:
            cancel()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.5, left))


def _blocked_file() -> Optional[str]:
    data = _context.get("dataDir")
    return os.path.join(data, "blocked.json") if data else None


def _read_blocked() -> Dict[str, float]:
    path = _blocked_file()
    try:
        with open(path, encoding="utf-8") as f:  # type: ignore[arg-type]
            seen = json.load(f)
        return {str(k): float(v) for k, v in seen.items()} if isinstance(seen, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _blocked_before(key: str) -> bool:
    """Whether this video was blocked through every retry in the last BLOCKED_REMEMBER_SEC."""
    return time.time() - _read_blocked().get(key, 0.0) < BLOCKED_REMEMBER_SEC


def _remember_blocked(key: str, blocked: bool) -> None:
    path = _blocked_file()
    seen = _read_blocked()
    if not path or (not blocked and key not in seen):
        return
    now = time.time()
    seen = {k: t for k, t in seen.items() if now - t < BLOCKED_REMEMBER_SEC and k != key}
    if blocked:
        seen[key] = now
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(seen, f)
        os.replace(path + ".tmp", path)
    except OSError as exc:
        print(f"[bot check] couldn't save {path}: {exc}")


def _run(call: Callable[[], Any], progress: Optional[Callable[..., None]] = None, update: bool = False,
         cancel: Optional[Callable[[], None]] = None, key: Optional[str] = None, wait_blocked: bool = True) -> Any:
    """Run a yt-dlp call with readable errors. With `update` (imports, which have time for pip): after an
    error a newer yt-dlp may fix, update yt-dlp and try once more. A call YouTube's bot check blocked is
    tried again after a pause (see BLOCKED_RETRY_DELAYS_SEC), unless the video `key` was blocked through every
    retry a short while ago, or `wait_blocked` is off (the sign-in is tried next)."""

    def attempt() -> Any:
        result = call()
        if key:
            _remember_blocked(key, False)
        return result

    try:
        return attempt()
    except YouTubeError:
        raise
    except Exception as exc:  # noqa: BLE001
        kind = error_kind(exc)
        if update and kind in ("blocked", "changed") and update_yt_dlp(progress, after_error=True):
            try:
                return attempt()
            except Exception as exc2:  # noqa: BLE001
                exc, kind = exc2, error_kind(exc2)
        again = kind == "blocked" and key is not None and _blocked_before(key)
        delays = () if again or not wait_blocked else BLOCKED_RETRY_DELAYS_SEC if update else BLOCKED_RETRY_DELAYS_QUICK_SEC
        if again:
            print("[bot check] blocked, and blocked through every retry last time: not waiting again")
        for n, delay in enumerate(delays, 1):
            if kind != "blocked":
                break
            print(f"[bot check] blocked, retry {n}/{len(delays)} in {delay} s")
            if progress is not None:
                progress(None, _text("retry", delay=delay, attempt=n, total=len(delays)))
            _wait(delay, cancel)
            try:
                return attempt()
            except Exception as exc2:  # noqa: BLE001
                exc, kind = exc2, error_kind(exc2)
        if kind is None:
            raise exc
        if kind == "blocked" and key:
            _remember_blocked(key, True)
        raise readable_error(kind, exc, again=again) from exc


def _video_key(url: str) -> str:
    """The video id of a YouTube link (the same video under another link form is the same video), else the link."""
    m = re.search(r"(?:[?&]v=|youtu\.be/|/(?:shorts|embed|live)/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else url


# ---------------------------------------------------------------------------
# Using the account sparingly
#
# The sign-in window's account is used as little and as gently as possible (limits for responsible use; nothing here
# hides anything from YouTube, and no limit can promise the account won't be restricted):
# - Anonymous first: searches, metadata, music videos and imports go without it. Only an import YouTube's bot check
#   blocked without it is tried with it, right away instead of the anonymous 5 / 15 / 30 s waits.
# - With the account: at most one retry, a pause between requests, one call at a time (a lock across processes).
# - ACCOUNT_DAILY_CAP imports a day with it. Past that, the sign-in window asks: stop for today (recommended) or go
#   on; going on holds until local midnight. Closing the window means stop.
# - Warning signs stop it for ACCOUNT_PAUSE_SEC: still blocked while signed in, or account trouble (unusual traffic,
#   too many requests, a suspended or terminated account). The user is told to check the account, not to retry.
#   A sign-in yt-dlp reports as no longer valid isn't a warning sign: the next blocked import opens the window again.
# Counters live in <dataDir>/account/, which a sign-out keeps.
# ---------------------------------------------------------------------------

ACCOUNT_DAILY_CAP = 30
ACCOUNT_PAUSE_SEC = 24 * 3600
ACCOUNT_RETRY_DELAYS_SEC = (10,)
ACCOUNT_SLEEP_REQUESTS_SEC = 1.5
ACCOUNT_LOCK_WAIT_SEC = 25 * 60
ACCOUNT_TROUBLE = ("unusual traffic", "http error 429", "too many requests", "your account has been",
                   "account has been suspended", "account has been terminated", "account has been disabled")
ACCOUNT_EXPIRED = ("cookies are no longer valid",)


def _account_dir() -> str:
    return os.path.join(_context.get("dataDir") or "", "account")


def _account_state() -> Dict[str, Any]:
    try:
        with open(os.path.join(_account_dir(), "usage.json"), encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_account_state(state: Dict[str, Any]) -> None:
    folder = _account_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "usage.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(path + ".tmp", path)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _account_uses_today() -> int:
    state = _account_state()
    return int(state.get("count") or 0) if state.get("day") == _today() else 0


def _count_account_use() -> None:
    _save_account_state(dict(_account_state(), day=_today(), count=_account_uses_today() + 1))


def _account_paused_until() -> float:
    until = float(_account_state().get("pausedUntil") or 0)
    return until if until > time.time() else 0.0


def _pause_account(reason: str) -> float:
    until = time.time() + ACCOUNT_PAUSE_SEC
    _save_account_state(dict(_account_state(), pausedUntil=until, pausedFor=reason))
    print(f"[account] not using the sign-in for {ACCOUNT_PAUSE_SEC // 3600} h: {reason}")
    return until


def _until_text(until: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(until))


class _AccountLog:
    """yt-dlp's logger on calls with the account: keeps its warnings to notice an invalid sign-in."""

    def __init__(self) -> None:
        self.warnings: List[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.warnings.append(str(msg))
        print(f"[account] WARNING: {msg}")

    def error(self, msg: str) -> None:
        print(f"[account] {msg}")

    def says(self, keys: Tuple[str, ...]) -> bool:
        return any(k in w.lower() for w in self.warnings for k in keys)


def _account_on() -> bool:
    """A sign-in is possible: a data folder, and a saved sign-in or Chrome / Edge to make one."""
    return bool(_context.get("dataDir")) and (_signin_cookies() is not None or _chromium() is not None)


def _with_account(call_with: Callable[[bool, Any], Any], progress: Optional[Callable[..., None]],
                  cancel: Optional[Callable[[], None]], blocked: YouTubeError) -> Any:
    """An import the bot check blocked without the sign-in, tried with it within the limits above."""
    cause = blocked.__cause__ or blocked
    paused = _account_paused_until()
    if paused:  # warning signs: no way around this one
        raise readable_error("account-paused", cause, extra={"until": _until_text(paused)}) from cause
    folder = _account_dir()
    os.makedirs(folder, exist_ok=True)
    end = last_note = time.monotonic()
    end += ACCOUNT_LOCK_WAIT_SEC
    lock = _take_lock(folder)
    while lock is None:  # one call with the account at a time
        if time.monotonic() > end:
            raise readable_error("blocked", cause)
        if progress is not None and time.monotonic() - last_note > SIGNIN_PROGRESS_EVERY_SEC:
            progress(None, _text("account-wait"))
            last_note = time.monotonic()
        _wait(1, cancel)
        lock = _take_lock(folder)
    try:
        if _account_uses_today() >= ACCOUNT_DAILY_CAP and _account_state().get("overCap") != _today():
            if not _ask_over_cap(progress, cancel):
                raise readable_error("account-cap", cause, extra={"cap": ACCOUNT_DAILY_CAP}) from cause
            _save_account_state(dict(_account_state(), overCap=_today()))  # until local midnight
        if not _signed_in_now():
            expired = _signin_cookies() is not None
            if _host_has_actions():  # the import panel offers the sign-in; it retries the import afterwards
                raise readable_error("signin-expired" if expired else "signin-needed", cause, action="signin") from cause
            ensure_signin(progress, cancel)  # older apps: the import opens the sign-in window itself
        _count_account_use()
        if progress is not None:
            progress(None, _text("account-try"))
        print(f"[account] blocked without the sign-in: trying with it ({_account_uses_today()}/{ACCOUNT_DAILY_CAP} today)")
        delays = (0,) + tuple(ACCOUNT_RETRY_DELAYS_SEC)
        exc: BaseException = cause
        for n, delay in enumerate(delays):
            if n:
                if progress is not None:
                    progress(None, _text("account-retry", delay=delay))
                _wait(delay, cancel)
            log = _AccountLog()
            try:
                return call_with(True, log)
            except YouTubeError:
                raise
            except Exception as err:  # noqa: BLE001
                exc = err
            low = str(exc).lower()
            if log.says(ACCOUNT_EXPIRED) or error_kind(exc) == "cookies":
                # not the account's fault: signing in again (the profile is usually still signed in) fixes it
                with open(_refresh_marker(), "w", encoding="utf-8"):
                    pass
                if _host_has_actions():
                    raise readable_error("signin-expired", exc, action="signin") from exc
                raise readable_error("signin-expired-import", exc) from exc
            if any(k in low for k in ACCOUNT_TROUBLE) or log.says(ACCOUNT_TROUBLE):
                break
            if error_kind(exc) != "blocked":
                kind = error_kind(exc)
                if kind is None:
                    raise exc
                raise readable_error(kind, exc) from exc
        until = _pause_account(str(exc).splitlines()[0][:200])
        raise readable_error("account-paused", exc, extra={"until": _until_text(until)}) from exc
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _download(call_with: Callable[[bool, Any], Any], progress: Optional[Callable[..., None]],
              cancel: Optional[Callable[[], None]], key: str) -> Any:
    """An import: anonymous first; a bot check goes on to the account (signing in first when there's none)."""
    if not _account_on():
        return _run(lambda: call_with(False, None), progress, update=True, cancel=cancel, key=key)
    try:
        return _run(lambda: call_with(False, None), progress, update=True, cancel=cancel, key=key, wait_blocked=False)
    except YouTubeError as err:
        if getattr(err, "kind", None) != "blocked":
            raise
        return _with_account(call_with, progress, cancel, err)


# ---------------------------------------------------------------------------
# Actions
#
# Apps with context["features"] containing "actions" show an error's `action` ({id, label, hint}) as a button in the
# import panel. Clicking it calls action(action_id, progress, cancel, options) in its own process (timeouts.action) and,
# when it returns ok, retries the import. The manifest lists the ids under "actions" (a top-level key: older apps
# reject unknown capabilities but ignore unknown keys).
#   signin   the sign-in window (warning first): sign in, or sign in again when YouTube stopped accepting the saved one
#   signout  delete the sign-in window's profile and the saved sign-in
# ---------------------------------------------------------------------------


def action(action_id: str, progress: Optional[Callable[..., None]] = None, cancel: Optional[Callable[[], None]] = None,
           options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if action_id == "signin":
        try:
            ensure_signin(progress, cancel, force=True)
        except YouTubeError as err:
            return {"ok": False, "message": err.sentence}  # type: ignore[attr-defined]
        return {"ok": True, "message": _text("signin-done")}
    if action_id == "signout":
        sign_out()
        return {"ok": True, "message": _text("signout-done")}
    raise ValueError(f"unknown action {action_id!r}")


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
        # what the app's lyrics search reads to build its queries
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
    _cancellable(lambda: update_yt_dlp(progress), cancel)
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

    def call_with(account: bool, logger: Any) -> Dict[str, Any]:
        def run() -> Dict[str, Any]:
            with _ydl(ydl_audio_opts, account=account, logger=logger) as ydl:
                return ydl.extract_info(url, download=True) or {}

        return _cancellable(run, cancel)

    info = _download(call_with, progress, cancel, _video_key(url))

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
    """The music video, video-only, at most the height the app asks for (its `videoQuality` setting). Never with the
    sign-in window's account: a blocked video falls back to the thumbnail / YouTube embed."""
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
        def run() -> None:
            with _ydl(opts) as ydl:
                ydl.download([_url(item_or_url)])

        _cancellable(run, cancel)
    except Exception as exc:  # noqa: BLE001 - the stage falls back to the YouTube embed / thumbnail
        if "cancel" in type(exc).__name__.lower():
            raise  # the user cancelled the import (the app's cancel()), not a failed download
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
