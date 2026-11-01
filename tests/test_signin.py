"""The sign-in window (no network, no browser: a fake browser that clicks through the real warning page on 127.0.0.1,
and a local WebSocket server for the DevTools client): python -m unittest discover tests"""

import base64
import hashlib
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import main  # noqa: E402

BLOCKED = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser"
SIGNED_IN = [
    {"domain": ".youtube.com", "name": "__Secure-3PSID", "value": "s3", "path": "/", "secure": True,
     "httpOnly": True, "expires": 1830000000.5, "session": False},
    {"domain": ".youtube.com", "name": "SAPISID", "value": "sap", "path": "/", "secure": True,
     "httpOnly": False, "expires": 1830000000, "session": False},
    {"domain": ".youtube.com", "name": "LOGIN_INFO", "value": "li", "path": "/", "secure": True,
     "httpOnly": True, "expires": 1830000000, "session": False},
    {"domain": ".google.com", "name": "SID", "value": "g", "path": "/", "secure": False,
     "httpOnly": False, "expires": -1, "session": True},
    {"domain": "www.example.com", "name": "other", "value": "x", "path": "/", "secure": False,
     "httpOnly": False, "expires": -1, "session": True},
]


class Cancelled(Exception):
    pass


def request(url, method="GET", body=None):
    """(status, headers, text) from the warning page's server, without following its redirect to Google."""
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    try:
        conn.request(method, u.path, body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read().decode("utf-8")
    finally:
        conn.close()


class FakeBrowser:
    """subprocess.Popen of the sign-in window: the user accepts the warning at poll `accepts_at` (None: never) and
    closes the window after `closes_after` polls (None: never)."""

    def __init__(self, accepts_at=1, closes_after=None):
        self.accepts_at = accepts_at
        self.closes_after = closes_after
        self.polls = 0
        self.closed = False
        self.url = None
        self.page = None

    def poll(self):
        self.polls += 1
        if self.polls == self.accepts_at and self.url:
            status, _, self.page = request(self.url)
            assert status == 200
            status, headers, _ = request(self.url + "accept", "POST", "understood=yes")
            assert status == 303 and headers["Location"] == main.SIGNIN_URL, (status, headers)
        if self.closes_after is not None and self.polls > self.closes_after:
            self.closed = True
        return 0 if self.closed else None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.closed = True


class SignInBase(unittest.TestCase):
    """A fake browser and DevTools; settings and a data folder of its own."""

    def setUp(self):
        self.saved = {k: getattr(main, k) for k in ("_chromium", "_launch_browser", "_devtools_url", "_cdp", "_wait",
                                                     "_firefox_found", "update_yt_dlp", "BLOCKED_RETRY_DELAYS_SEC",
                                                     "SIGNIN_TIMEOUT_SEC")}
        self.saved_ydl, self.saved_quick = main._ydl, main.BLOCKED_RETRY_DELAYS_QUICK_SEC
        main.SIGNIN_TIMEOUT_SEC = 5  # a test that never signs in can't hang
        self.data = tempfile.TemporaryDirectory()
        self.commands = []
        self.cookies = []  # what the browser has, per Storage.getCookies
        self.browser = FakeBrowser()
        main._chromium = lambda: ("Chrome", "chrome.exe")

        def launch(exe, profile, url):
            self.browser.url = url
            return self.browser

        main._launch_browser = launch
        main._devtools_url = lambda profile, proc, cancel: "ws://127.0.0.1:1/devtools/browser/x"
        main._wait = lambda seconds, cancel: cancel() if cancel else None
        main._firefox_found = lambda: False
        main.update_yt_dlp = lambda *a, **k: False
        main.BLOCKED_RETRY_DELAYS_SEC = (0.01,)

        def cdp(url, method, params=None):
            self.commands.append(method)
            if method == "Browser.close":
                self.browser.closed = True
                return {}
            return {"cookies": list(self.cookies.pop(0) if self.cookies else [])}

        main._cdp = cdp
        self.settings({"signIn": True})

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(main, k, v)
        main.setup({"settings": {}})
        self.data.cleanup()

    def settings(self, settings, locale="en"):
        main.setup({"settings": settings, "dataDir": self.data.name, "locale": locale})

    def sign_in(self):
        self.cookies = [SIGNED_IN, SIGNED_IN]
        return main.ensure_signin(None, lambda: None)


class SignInTest(SignInBase):
    def test_signs_in_writes_cookies_and_closes_the_window(self):
        self.cookies = [[], [SIGNED_IN[2]], SIGNED_IN, SIGNED_IN]  # Google first, then youtube.com
        messages = []
        path = main.ensure_signin(lambda p, m: messages.append(m), lambda: None)
        self.assertEqual(path, os.path.join(self.data.name, "signin", "cookies.txt"))
        self.assertEqual(main._signin_cookies(), path)
        self.assertEqual(self.commands[-1], "Browser.close")
        self.assertTrue(self.browser.closed)
        self.assertTrue(messages[0].startswith("Don’t sign in with your main account"))
        self.assertIn("Read the warning in the Chrome window", messages[0])
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "# Netscape HTTP Cookie File")
        self.assertIn("#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1830000000\t__Secure-3PSID\ts3", lines)
        self.assertIn(".youtube.com\tTRUE\t/\tTRUE\t1830000000\tSAPISID\tsap", lines)
        self.assertIn(".google.com\tTRUE\t/\tFALSE\t0\tSID\tg", lines)
        self.assertFalse(any("example.com" in line for line in lines))
        # signed in: later imports (and app restarts: it's a file in dataDir) don't open a window
        self.browser = FakeBrowser()
        self.settings({"signIn": True})
        self.assertEqual(main.ensure_signin(None, lambda: None), path)
        self.assertEqual(self.browser.polls, 0)

    def test_the_warning_page(self):
        self.settings({"signIn": True}, locale="zh-TW")
        self.sign_in()
        page = self.browser.page
        self.assertIn('<html lang="zh-TW">', page)
        self.assertIn("<h1>請勿登入主帳號</h1>", page)
        self.assertIn("我了解風險，我使用的是另外申請的小帳號", page)
        self.assertIn("請使用另外申請的小帳號。", page)
        self.assertIn('button.disabled = !box.checked;', page)  # Continue stays disabled until the box is ticked
        self.assertIn('name="understood" value="yes" required', page)
        for lang in ("en", "ja", "ko"):
            self.assertIn(f"<h1>{main.WARNING_PAGE[lang]['heading']}</h1>", main._warning_html(lang))

    def test_the_warning_cant_be_skipped(self):
        warning = main._Warning("en")
        try:
            status, headers, _ = request(warning.url + "accept", "POST", "")  # the box not ticked
            self.assertEqual((status, headers["Location"]), (303, urllib.parse.urlsplit(warning.url).path))
            self.assertEqual(request(warning.url.replace(warning.url.split("/")[3], "guess") + "accept", "POST",
                                     "understood=yes")[0], 404)
            self.assertFalse(warning.accepted.is_set())
            self.assertEqual(request(warning.url + "accept", "POST", "understood=yes")[0], 303)
            self.assertTrue(warning.accepted.is_set())
        finally:
            warning.close()

    def test_nothing_is_read_before_the_warning_is_accepted(self):
        self.browser = FakeBrowser(accepts_at=None, closes_after=4)
        with self.assertRaises(main.YouTubeError):
            main.ensure_signin(None, lambda: None)
        self.assertNotIn("Storage.getCookies", self.commands)

    def test_waiting_progress_is_in_the_app_language(self):
        self.settings({"signIn": True}, locale="zh-TW")
        self.cookies = [SIGNED_IN, SIGNED_IN]
        messages = []
        main.ensure_signin(lambda p, m: messages.append(m), lambda: None)
        self.assertTrue(messages[0].startswith(
            "請勿登入主帳號：用帳號下載可能讓 YouTube 限制或停用該帳號，請使用另外申請的小帳號。請先看完剛開啟的 Chrome 視窗裡的警告"))

    def test_window_closed_before_signing_in(self):
        self.browser = FakeBrowser(accepts_at=None, closes_after=2)
        self.settings({"signIn": True}, locale="zh-TW")
        with self.assertRaises(main.YouTubeError) as err:
            main.ensure_signin(None, lambda: None)
        self.assertEqual(type(err.exception).__name__, "YouTube 登入已取消")
        self.assertIsNone(main._signin_cookies())

    def test_cancel_closes_the_window(self):
        polls = []

        def cancel():
            polls.append(1)
            if len(polls) > 3:
                raise Cancelled()

        with self.assertRaises(Cancelled):
            main.ensure_signin(None, cancel)
        self.assertEqual(self.commands[-1], "Browser.close")
        self.assertTrue(self.browser.closed)

    def test_gives_up_after_the_timeout(self):
        saved = main.SIGNIN_TIMEOUT_SEC
        main.SIGNIN_TIMEOUT_SEC = -1
        try:
            with self.assertRaises(main.YouTubeError) as err:
                main.ensure_signin(None, lambda: None)
        finally:
            main.SIGNIN_TIMEOUT_SEC = saved
        self.assertIn("15 minutes", str(err.exception))
        self.assertTrue(self.browser.closed)

    def test_no_chrome_or_edge(self):
        main._chromium = lambda: None
        with self.assertRaises(main.YouTubeError) as err:
            main.ensure_signin(None, lambda: None)
        self.assertIn("needs Google Chrome or Microsoft Edge", str(err.exception))

    def test_off_or_own_cookies_open_nothing(self):
        for settings in ({"signIn": False}, {"signIn": True, "cookiesFile": "C:\\c.txt"},
                         {"signIn": True, "cookiesFromBrowser": "firefox"}):
            self.settings(settings)
            self.assertIsNone(main.ensure_signin(None, lambda: None))
        self.assertEqual(self.browser.polls, 0)

    def test_turning_the_setting_off_signs_out(self):
        self.sign_in()
        self.settings({"signIn": True})
        self.assertTrue(os.path.isdir(os.path.join(self.data.name, "signin", "profile")))
        self.settings({"signIn": False})
        self.assertFalse(os.path.exists(os.path.join(self.data.name, "signin")))

    def test_yt_dlp_saves_refreshed_cookies_through_a_copy(self):
        path = self.sign_in()
        with open(path, encoding="utf-8") as f:
            before = f.read()

        class FakeYDL:
            def __init__(self, cookiefile, write):
                self.cookiefile, self.write = cookiefile, write

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                with open(self.cookiefile, "w", encoding="utf-8") as f:  # yt-dlp's save, in place
                    f.write(self.write)

        refreshed = before.replace("\tsap", "\tsap2")
        work = main._cookie_work_copy()
        with main._SignedInYDL(FakeYDL(work, refreshed), work):
            pass
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), refreshed)
        self.assertFalse(os.path.exists(work))
        # a save cut short (an empty or broken file) leaves cookies.txt as it was
        for broken in ("", "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/"):
            work = main._cookie_work_copy()
            with main._SignedInYDL(FakeYDL(work, broken), work):
                pass
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), refreshed)
            self.assertFalse(os.path.exists(work))
        # signed out meanwhile: not brought back
        work = main._cookie_work_copy()
        os.remove(path)
        with main._SignedInYDL(FakeYDL(work, refreshed), work):
            pass
        self.assertFalse(os.path.exists(path))


class AccountTest(SignInBase):
    """Using the sign-in window's account sparingly: anonymous first, one retry, a daily cap, a pause on warning
    signs. `call_with(account, logger)` stands for yt-dlp; the sign-in itself goes through the fake browser."""

    def setUp(self):
        super().setUp()
        self.calls = []  # True / False: with the account or not
        self.anonymous = [BLOCKED]  # what each call without the account raises (None: works)
        self.signed = [None]  # with the account
        self.warn = []  # yt-dlp warnings on calls with the account

    def call_with(self, account, logger):
        self.calls.append(account)
        outcomes = self.signed if account else self.anonymous
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if account and logger is not None:
            for w in self.warn:
                logger.warning(w)
        if outcome:
            raise RuntimeError(outcome)
        return {"ok": account}

    def download(self, progress=None, cancel=lambda: None):
        self.cookies = [SIGNED_IN, SIGNED_IN]
        return main._download(self.call_with, progress, cancel, "YJfHuATJYsQ")

    def test_anonymous_works_the_account_isnt_used(self):
        self.anonymous = [None]
        self.assertEqual(self.download(), {"ok": False})
        self.assertEqual(self.calls, [False])
        self.assertIsNone(self.browser.url)  # no sign-in window
        self.assertEqual(main._account_uses_today(), 0)

    def test_blocked_without_goes_on_to_the_account_at_once(self):
        messages = []
        self.assertEqual(self.download(lambda p, m: messages.append(m)), {"ok": True})
        self.assertEqual(self.calls, [False, True])  # no anonymous 5 / 15 / 30 s waits first
        self.assertIn("trying once with your YouTube sign-in", messages[-1])
        self.assertEqual(main._account_uses_today(), 1)

    def test_other_errors_dont_use_the_account(self):
        self.anonymous = ["ERROR: [youtube] abc: Private video"]
        with self.assertRaises(RuntimeError):
            self.download()
        self.assertEqual(self.calls, [False])

    def test_off_never_uses_the_account(self):
        self.settings({"signIn": False})
        main.BLOCKED_RETRY_DELAYS_SEC = (0.01,)
        with self.assertRaises(main.YouTubeError):
            self.download()
        self.assertEqual(self.calls, [False, False])  # the anonymous retry, nothing else

    def test_at_most_one_retry_then_a_24_hour_pause(self):
        self.signed = [BLOCKED]
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(self.calls, [False, True, True])
        self.assertEqual(err.exception.kind, "account-paused")
        self.assertIn("won’t use the account until", str(err.exception))
        self.assertIn("don’t keep retrying", str(err.exception))
        # paused: the next import doesn't touch the account
        self.calls.clear()
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(self.calls, [False])
        self.assertEqual(err.exception.kind, "account-paused")
        # 24 hours later it may be used again
        state = main._account_state()
        state["pausedUntil"] = 1
        main._save_account_state(state)
        self.signed = [None]
        self.calls.clear()
        self.assertEqual(self.download(), {"ok": True})

    def test_account_trouble_pauses_without_a_retry(self):
        self.signed = ["ERROR: [youtube] abc: HTTP Error 429: Too Many Requests"]
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(self.calls, [False, True])
        self.assertEqual(err.exception.kind, "account-paused")
        self.assertGreater(main._account_paused_until(), 0)

    def test_an_invalid_sign_in_is_refreshed_not_paused(self):
        path = os.path.join(self.data.name, "signin", "cookies.txt")
        self.signed = [BLOCKED]
        self.warn = ["The provided YouTube account cookies are no longer valid. They have likely been rotated"]
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(self.calls, [False, True])
        self.assertIn("YouTube didn’t accept it", str(err.exception))
        self.assertNotIn("sap", str(err.exception))
        self.assertEqual(main._account_paused_until(), 0)
        self.assertTrue(os.path.isfile(path))  # kept until the refresh replaces it
        self.assertTrue(os.path.exists(main._refresh_marker()))
        # the next blocked import opens the window again, warning first (the profile is still signed in)
        self.browser = FakeBrowser()
        self.signed, self.warn = [None], []
        self.assertEqual(self.download(), {"ok": True})
        self.assertEqual(self.browser.page.count("<h1>"), 1)
        self.assertFalse(os.path.exists(main._refresh_marker()))

    def test_daily_cap(self):
        main._save_account_state({"day": main._today(), "count": main.ACCOUNT_DAILY_CAP - 1})
        self.assertEqual(self.download(), {"ok": True})
        self.calls.clear()
        self.settings({"signIn": True}, locale="zh-TW")
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(self.calls, [False])
        self.assertEqual(type(err.exception).__name__, "今天用帳號下載的次數已達上限")
        self.assertIn("明天再試或換網路", str(err.exception))
        # a new day
        main._save_account_state({"day": "2000-01-01", "count": 99})
        self.calls.clear()
        self.assertEqual(self.download(), {"ok": True})

    def test_one_call_with_the_account_at_a_time(self):
        os.makedirs(main._account_dir(), exist_ok=True)
        with open(os.path.join(main._account_dir(), ".lock"), "w"):
            pass  # another import is using the account
        polls = []

        def cancel():
            polls.append(1)
            if len(polls) > 5:
                raise Cancelled()

        with self.assertRaises(Cancelled):
            self.download(cancel=cancel)
        self.assertEqual(self.calls, [False])

    def test_searches_metadata_and_music_videos_never_use_the_account(self):
        seen = []

        def ydl(opts, account=False, logger=None):
            seen.append(account)
            raise RuntimeError(BLOCKED)

        main._ydl = ydl
        main.BLOCKED_RETRY_DELAYS_QUICK_SEC = (0.01,)
        try:
            with self.assertRaises(main.YouTubeError):
                main.search("擱淺", 5)
            with self.assertRaises(main.YouTubeError):
                main.metadata("YJfHuATJYsQ")
            self.assertIsNone(main.fetch_video("YJfHuATJYsQ", self.data.name, lambda *a: None, lambda: None,
                                               {"videoMaxHeight": 720}))
        finally:
            main._ydl = self.saved_ydl
            main.BLOCKED_RETRY_DELAYS_QUICK_SEC = self.saved_quick
        self.assertEqual(seen, [False] * len(seen))
        self.assertGreaterEqual(len(seen), 5)
        self.assertIsNone(self.browser.url)

    def test_warning_page_says_use_is_limited_but_not_guaranteed(self):
        self.download()
        self.assertIn(f"at most {main.ACCOUNT_DAILY_CAP} a day", self.browser.page)
        self.assertIn("can’t guarantee", self.browser.page)
        self.assertIn("無法保證帳號不會被限制", main._warning_html("zh-TW"))


class WebSocketTest(unittest.TestCase):
    """main._cdp against a local server speaking just enough RFC 6455."""

    def test_cdp_command_and_reply(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received = {}
        big = {"id": 1, "result": {"cookies": [dict(SIGNED_IN[0], value="v" * 70000)]}}

        def frame(opcode, data):
            n = len(data)
            head = bytes([0x80 | opcode])
            if n < 126:
                head += bytes([n])
            elif n < 1 << 16:
                head += bytes([126]) + n.to_bytes(2, "big")
            else:
                head += bytes([127]) + n.to_bytes(8, "big")
            return head + data

        def serve():
            conn, _ = server.accept()
            with conn:
                head = b""
                while b"\r\n\r\n" not in head:
                    head += conn.recv(1024)
                key = [line.split(b": ", 1)[1] for line in head.split(b"\r\n") if line.lower().startswith(
                    b"sec-websocket-key")][0]
                accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                b1, b2 = conn.recv(2)
                n = b2 & 0x7F
                mask = conn.recv(4)
                data = b""
                while len(data) < n:
                    data += conn.recv(n - len(data))
                received["text"] = bytes(b ^ mask[i % 4] for i, b in enumerate(data)).decode()
                received["masked"] = bool(b2 & 0x80)
                conn.sendall(frame(0x9, b"ping"))  # answered with a pong
                conn.sendall(frame(0x1, json.dumps({"method": "Target.targetCreated", "params": {}}).encode()))
                reply = json.dumps(big).encode()
                conn.sendall(bytes([0x01, 100]) + reply[:100])  # first fragment: text, no FIN
                conn.sendall(frame(0x0, reply[100:]))
                pong = b""
                while len(pong) < 10:  # header, mask, "ping"
                    pong += conn.recv(10 - len(pong))
                received["pong"] = pong[0] & 0x0F
                while conn.recv(1024):  # until the client closes (closing first with unread bytes resets it)
                    pass

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        result = main._cdp(f"ws://127.0.0.1:{port}/devtools/browser/abc", "Storage.getCookies")
        t.join(5)
        server.close()
        self.assertEqual(json.loads(received["text"]), {"id": 1, "method": "Storage.getCookies", "params": {}})
        self.assertTrue(received["masked"])
        self.assertEqual(received["pong"], 0xA)
        self.assertEqual(len(result["cookies"][0]["value"]), 70000)


if __name__ == "__main__":
    unittest.main()
