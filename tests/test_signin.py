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
    """subprocess.Popen of the sign-in window: the user picks `choice` on its page at poll `accepts_at` (None: never;
    on the warning, with the box ticked) and closes the window after `closes_after` polls (None: never)."""

    def __init__(self, accepts_at=1, closes_after=2, choice="continue"):
        self.accepts_at = accepts_at
        self.choice = choice
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
            status, headers, _ = request(self.url + "choose", "POST", f"choice={self.choice}&understood=yes")
            assert status == 303, (status, headers)
            self.went_to = headers["Location"]
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
                                                     "update_yt_dlp", "BLOCKED_RETRY_DELAYS_SEC",
                                                     "SIGNIN_TIMEOUT_SEC")}
        self.saved_ydl, self.saved_quick = main._ydl, main.BLOCKED_RETRY_DELAYS_QUICK_SEC
        main.SIGNIN_TIMEOUT_SEC = 5  # a test that never signs in can't hang
        self.data = tempfile.TemporaryDirectory()
        self.commands = []
        self.cookies = []  # what the browser has, per Storage.getCookies
        self.browser = FakeBrowser()
        main._chromium = lambda: ("Chrome", "chrome.exe")

        self.launches = []  # (debug, headless) of each browser started

        def launch(exe, profile, url, debug=True, headless=False):
            self.launches.append((debug, headless))
            if headless:  # reading the profile's cookies after the window closed: open until Browser.close
                self.reader = FakeBrowser(accepts_at=None, closes_after=None)
                return self.reader
            self.browser.url = url
            return self.browser

        main._launch_browser = launch
        main._devtools_url = lambda profile, proc, cancel: "ws://127.0.0.1:1/devtools/browser/x"
        main._wait = lambda seconds, cancel: cancel() if cancel else None
        main.update_yt_dlp = lambda *a, **k: False
        main.BLOCKED_RETRY_DELAYS_SEC = (0.01,)

        def cdp(url, method, params=None):
            self.commands.append(method)
            if method == "Browser.close":
                (self.reader if self.launches[-1][1] else self.browser).closed = True
                return {}
            return {"cookies": list(self.cookies.pop(0) if self.cookies else [])}

        main._cdp = cdp
        self.host()

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(main, k, v)
        main.setup({"settings": {}})
        self.data.cleanup()

    def host(self, locale="en", actions=True, settings=None):
        """setup() as the app calls it: an app with action buttons (features ["actions"]) or an older one."""
        main.setup({"settings": settings or {"downloadVideo": True, "subtitles": True}, "dataDir": self.data.name,
                    "locale": locale, **({"features": ["actions"]} if actions else {})})

    def sign_in(self):
        self.cookies = [SIGNED_IN, SIGNED_IN]
        return main.ensure_signin(None, lambda: None)


class SignInTest(SignInBase):
    def test_signs_in_writes_cookies_and_closes_the_window(self):
        self.cookies = [SIGNED_IN]
        messages = []
        path = main.ensure_signin(lambda p, m: messages.append(m), lambda: None)
        self.assertEqual(path, os.path.join(self.data.name, "signin", "cookies.txt"))
        self.assertEqual(main._signin_cookies(), path)
        # the user signs in in a plain window (no DevTools: Google refuses a driven browser) and closes it; then a
        # headless browser on the profile reads the cookies and closes
        self.assertEqual(self.launches, [(False, False), (True, True)])
        self.assertEqual(self.commands, ["Storage.getCookies", "Browser.close"])
        self.assertTrue(self.reader.closed)
        self.assertTrue(messages[0].startswith("Don’t sign in with your main account"))
        self.assertIn("sign in to YouTube there, then close the window", messages[0])
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "# Netscape HTTP Cookie File")
        self.assertIn("#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1830000000\t__Secure-3PSID\ts3", lines)
        self.assertIn(".youtube.com\tTRUE\t/\tTRUE\t1830000000\tSAPISID\tsap", lines)
        self.assertIn(".google.com\tTRUE\t/\tFALSE\t0\tSID\tg", lines)
        self.assertFalse(any("example.com" in line for line in lines))
        # signed in: later imports (and app restarts: it's a file in dataDir) don't open a window
        self.browser = FakeBrowser()
        self.host()
        self.assertEqual(main.ensure_signin(None, lambda: None), path)
        self.assertEqual(self.browser.polls, 0)

    def test_the_warning_page(self):
        self.host("zh-TW")
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
            page = urllib.parse.urlsplit(warning.url).path
            for body in ("", "choice=continue", "understood=yes", "choice=stop&understood=yes"):
                status, headers, _ = request(warning.url + "choose", "POST", body)  # the box not ticked, or no choice
                self.assertEqual((status, headers["Location"]), (303, page), body)
            self.assertEqual(request(warning.url.replace(warning.url.split("/")[3], "guess") + "choose", "POST",
                                     "choice=continue&understood=yes")[0], 404)
            self.assertFalse(warning.accepted)
            status, headers, _ = request(warning.url + "choose", "POST", "choice=continue&understood=yes")
            self.assertEqual((status, headers["Location"]), (303, main.SIGNIN_URL))
            self.assertTrue(warning.accepted)
        finally:
            warning.close()

    def test_nothing_is_read_before_the_warning_is_accepted(self):
        self.browser = FakeBrowser(accepts_at=None, closes_after=4)
        with self.assertRaises(main.YouTubeError):
            main.ensure_signin(None, lambda: None)
        self.assertNotIn("Storage.getCookies", self.commands)

    def test_waiting_progress_is_in_the_app_language(self):
        self.host("zh-TW")
        self.cookies = [SIGNED_IN, SIGNED_IN]
        messages = []
        main.ensure_signin(lambda p, m: messages.append(m), lambda: None)
        self.assertTrue(messages[0].startswith(
            "請勿登入主帳號：用帳號下載可能讓 YouTube 限制或停用該帳號，請使用另外申請的小帳號。請先看完剛開啟的 Chrome 視窗裡的警告"))

    def test_window_closed_without_signing_in(self):
        self.cookies = [[SIGNED_IN[3]]]  # accepted the warning, closed on Google's page
        self.host("zh-TW")
        with self.assertRaises(main.YouTubeError) as err:
            main.ensure_signin(None, lambda: None)
        self.assertEqual(type(err.exception).__name__, "YouTube 登入已取消")
        self.assertIsNone(main._signin_cookies())
        self.assertTrue(self.reader.closed)

    def test_the_browser_follows_the_app_language(self):
        saved, made = main.subprocess.Popen, []
        main._launch_browser = self.saved["_launch_browser"]
        main.subprocess.Popen = lambda args, **kw: made.append(args)
        try:
            for locale, flag in (("zh-TW", "--lang=zh-TW"), ("en", "--lang=en-US"), ("ja", "--lang=ja")):
                self.host(locale)
                main._launch_browser("chrome.exe", os.path.join(self.data.name, "p"), "http://x/", debug=False)
                self.assertIn(flag, made[-1])
                self.assertFalse(any(a.startswith("--remote-debugging") for a in made[-1]))
            main._launch_browser("chrome.exe", os.path.join(self.data.name, "p"), "about:blank", headless=True)
            self.assertIn("--remote-debugging-port=0", made[-1])
            self.assertIn("--headless=new", made[-1])
        finally:
            main.subprocess.Popen = saved

    def test_window_closed_before_signing_in(self):
        self.browser = FakeBrowser(accepts_at=None, closes_after=2)
        self.host("zh-TW")
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
        self.assertEqual(self.commands, [])
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
        self.assertEqual(type(err.exception).__name__, "Signing in to YouTube needs Chrome or Edge")

    def test_settings_from_older_versions_are_ignored(self):
        old = {"cookiesFromBrowser": "chrome", "cookiesFile": "C:\\missing\\cookies.txt", "signIn": False}
        self.host(settings=old)
        path = self.sign_in()  # setup() with signIn off no longer signs out
        self.host(settings=old)
        self.assertEqual(main._signin_cookies(), path)
        made = []

        class FakeYtDlp:
            class YoutubeDL:
                def __init__(self, params):
                    made.append(params)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    pass

        saved = main._yt_dlp
        main._yt_dlp = lambda: FakeYtDlp
        try:
            main._ydl({"skip_download": True})
            with main._ydl({}, account=True, logger="log"):
                pass
        finally:
            main._yt_dlp = saved
        self.assertFalse({"cookiefile", "cookiesfrombrowser"} & set(made[0]))  # anonymous: no cookies at all
        self.assertTrue(made[1]["cookiefile"].endswith(".work"))  # the account: a copy of the sign-in's cookies
        self.assertEqual((made[1]["sleep_interval_requests"], made[1]["logger"]), (main.ACCOUNT_SLEEP_REQUESTS_SEC, "log"))
        self.assertNotIn("cookiesfrombrowser", made[1])

    def test_signin_action(self):
        self.cookies = [SIGNED_IN, SIGNED_IN]
        messages = []
        self.assertEqual(main.action("signin", lambda p, m: messages.append(m), lambda: None),
                         {"ok": True, "message": "Signed in to YouTube."})
        self.assertTrue(messages[0].startswith("Don’t sign in with your main account"))
        self.assertIsNotNone(main._signin_cookies())
        # asked again while signed in: the window opens again (to switch accounts), the warning first
        self.browser = FakeBrowser()
        self.cookies = [SIGNED_IN, SIGNED_IN]
        self.assertTrue(main.action("signin", None, lambda: None)["ok"])
        self.assertIn("<h1>Don’t sign in with your main account</h1>", self.browser.page)

    def test_signin_action_cancelled(self):
        self.host("zh-TW")
        self.browser = FakeBrowser(accepts_at=None, closes_after=2)
        result = main.action("signin", None, lambda: None)
        self.assertEqual(result, {"ok": False, "message": "YouTube 登入已取消：登入視窗在登入前就被關閉了，需要時可以再試一次。"})

    def test_signout_action(self):
        self.sign_in()
        self.host("ko")
        self.assertEqual(main.action("signout", None, lambda: None),
                         {"ok": True, "message": "YouTube에서 로그아웃하고 로그인 창의 프로필을 삭제했습니다."})
        self.assertFalse(os.path.exists(os.path.join(self.data.name, "signin")))
        with self.assertRaises(ValueError):
            main.action("dance", None, lambda: None)

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

    def test_blocked_without_a_sign_in_offers_it_as_the_errors_action(self):
        self.host("zh-TW")
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(self.calls, [False])  # no anonymous 5 / 15 / 30 s waits, no window, no account
        self.assertIsNone(self.browser.url)
        self.assertEqual(err.exception.kind, "signin-needed")
        self.assertEqual(type(err.exception).__name__, "YouTube 要求確認你不是機器人")
        self.assertEqual(err.exception.action, {
            "id": "signin", "label": "開啟網頁登入後再次嘗試",
            "hint": "會開啟一個獨立視窗讓你登入 YouTube，請用另外申請的小帳號，不要用主帳號。"})
        self.assertEqual(main._account_uses_today(), 0)
        # the import panel's button: action("signin"), then the app retries the import
        self.cookies = [SIGNED_IN, SIGNED_IN]
        self.assertTrue(main.action("signin", None, lambda: None)["ok"])
        self.calls.clear()
        self.assertEqual(self.download(), {"ok": True})
        self.assertEqual(self.calls, [False, True])
        self.assertEqual(main._account_uses_today(), 1)

    def test_signed_in_a_blocked_import_goes_on_to_the_account_at_once(self):
        self.sign_in()
        messages = []
        self.assertEqual(self.download(lambda p, m: messages.append(m)), {"ok": True})
        self.assertEqual(self.calls, [False, True])
        self.assertIn("trying once with your YouTube sign-in", messages[-1])
        self.assertEqual(main._account_uses_today(), 1)

    def test_older_apps_open_the_sign_in_window_from_the_import(self):
        self.host(actions=False)
        self.assertEqual(self.download(), {"ok": True})  # the warning, the sign-in, then the account
        self.assertEqual(self.calls, [False, True])
        self.assertIn("<h1>Don’t sign in with your main account</h1>", self.browser.page)

    def test_other_errors_dont_use_the_account(self):
        self.anonymous = ["ERROR: [youtube] abc: Private video"]
        with self.assertRaises(RuntimeError):
            self.download()
        self.assertEqual(self.calls, [False])

    def test_without_chrome_or_edge_and_no_sign_in_it_waits_anonymously(self):
        main._chromium = lambda: None
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(self.calls, [False, False])  # the anonymous retry, nothing else
        self.assertEqual(err.exception.kind, "blocked")
        self.assertFalse(hasattr(err.exception, "action"))

    def test_at_most_one_retry_then_a_24_hour_pause(self):
        self.sign_in()
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
        self.sign_in()
        self.signed = ["ERROR: [youtube] abc: HTTP Error 429: Too Many Requests"]
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(self.calls, [False, True])
        self.assertEqual(err.exception.kind, "account-paused")
        self.assertGreater(main._account_paused_until(), 0)

    def test_an_invalid_sign_in_is_refreshed_not_paused(self):
        path = self.sign_in()
        self.signed = [BLOCKED]
        self.warn = ["The provided YouTube account cookies are no longer valid. They have likely been rotated"]
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(self.calls, [False, True])
        self.assertEqual(err.exception.kind, "signin-expired")
        self.assertEqual(err.exception.action["id"], "signin")
        self.assertEqual(str(err.exception), "Sign in again, then try again.")
        self.assertEqual(main._account_paused_until(), 0)
        self.assertTrue(os.path.isfile(path))  # kept until the sign-in replaces it
        self.assertTrue(os.path.exists(main._refresh_marker()))
        # until then a blocked import offers the sign-in again, without trying the stale one
        self.calls.clear()
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual((self.calls, err.exception.kind), ([False], "signin-expired"))
        # the button: the window opens (warning first), the kept profile is still signed in
        self.browser = FakeBrowser()
        self.cookies = [SIGNED_IN, SIGNED_IN]
        self.assertTrue(main.action("signin", None, lambda: None)["ok"])
        self.assertFalse(os.path.exists(main._refresh_marker()))
        self.signed, self.warn = [None], []
        self.calls.clear()
        self.assertEqual(self.download(), {"ok": True})

    def test_an_invalid_sign_in_in_older_apps(self):
        self.sign_in()
        self.host("zh-TW", actions=False)
        self.signed = [BLOCKED]
        self.warn = ["The provided YouTube account cookies are no longer valid."]
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertFalse(hasattr(err.exception, "action"))
        self.assertIn("請再匯入一次，會重新開啟登入視窗", str(err.exception))

    def at_cap(self, choice="stop", **browser):
        """Signed in already, today's cap reached; the next window is the cap question."""
        self.sign_in()
        main._save_account_state({"day": main._today(), "count": main.ACCOUNT_DAILY_CAP})
        self.browser = FakeBrowser(choice=choice, **browser)

    def test_daily_cap_asks_and_stopping_is_the_default(self):
        self.host("zh-TW")
        self.at_cap("stop")
        messages = []
        with self.assertRaises(main.YouTubeError) as err:
            self.download(lambda p, m: messages.append(m))
        self.assertEqual(self.calls, [False])  # the account wasn't used
        self.assertEqual(type(err.exception).__name__, f"今天用帳號下載已達 {main.ACCOUNT_DAILY_CAP} 首")
        self.assertIn("建議今天先停，以免帳號被限制。如果仍要繼續", str(err.exception))
        self.assertIn("請在跳出的 Chrome 視窗選擇今天先停或繼續", messages[0])
        page = self.browser.page
        self.assertIn(f"<h1>今天用帳號下載已達 {main.ACCOUNT_DAILY_CAP} 首</h1>", page)
        self.assertIn('value="stop" autofocus>今天先停（建議）', page)
        self.assertIn('value="continue" class="secondary">我了解風險，仍要繼續', page)
        self.assertTrue(self.browser.closed)
        self.assertNotEqual(main._account_state().get("overCap"), main._today())

    def test_daily_cap_closing_the_window_stops(self):
        self.at_cap(accepts_at=None, closes_after=2)
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(err.exception.kind, "account-cap")
        self.assertEqual(self.calls, [False])

    def test_daily_cap_continue_holds_until_midnight(self):
        self.at_cap("continue")
        self.assertEqual(self.download(), {"ok": True})
        self.assertEqual(self.calls, [False, True])
        self.assertEqual(self.browser.went_to.rsplit("/", 1)[1], "done")
        self.assertEqual(main._account_state()["overCap"], main._today())
        # the rest of today: not asked again
        self.browser = FakeBrowser(choice="stop")
        self.calls.clear()
        self.assertEqual(self.download(), {"ok": True})
        self.assertIsNone(self.browser.url)
        # a new day starts over: count 0, and the next cap asks again
        main._save_account_state(dict(main._account_state(), day="2000-01-01"))
        self.assertEqual(main._account_uses_today(), 0)
        main._save_account_state({"day": main._today(), "count": main.ACCOUNT_DAILY_CAP, "overCap": "2000-01-01"})
        with self.assertRaises(main.YouTubeError):
            self.download()
        self.assertIsNotNone(self.browser.url)

    def test_continuing_past_the_cap_doesnt_lift_the_pause(self):
        self.at_cap("continue")
        main._save_account_state(dict(main._account_state(), overCap=main._today()))
        main._pause_account("test")
        with self.assertRaises(main.YouTubeError) as err:
            self.download()
        self.assertEqual(err.exception.kind, "account-paused")
        self.assertEqual(self.calls, [False])
        self.assertIsNone(self.browser.url)

    def test_under_the_cap_nothing_is_asked(self):
        self.sign_in()
        self.browser = FakeBrowser(choice="stop")
        self.assertEqual(self.download(), {"ok": True})
        self.assertIsNone(self.browser.url)

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
        self.sign_in()
        self.assertIn(f"it asks you after {main.ACCOUNT_DAILY_CAP} a day", self.browser.page)
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
