"""Bot-check retries in main._run and the messages' language (no network): python -m unittest discover tests"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import main  # noqa: E402

BLOCKED = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser"


class Cancelled(Exception):
    pass


def flaky(failures, error=BLOCKED):
    calls = []

    def call():
        calls.append(1)
        if len(calls) <= failures:
            raise RuntimeError(error)
        return {"ok": True}

    return call, calls


class BotRetryTest(unittest.TestCase):
    def setUp(self):
        self.saved = (main.BLOCKED_RETRY_DELAYS_SEC, main.BLOCKED_RETRY_DELAYS_QUICK_SEC, main.update_yt_dlp,
                      main._firefox_found)
        main.BLOCKED_RETRY_DELAYS_SEC = (0.01, 0.01, 0.01)
        main.BLOCKED_RETRY_DELAYS_QUICK_SEC = (0.01,)
        main.update_yt_dlp = lambda *a, **k: False  # no pip in tests
        main._firefox_found = lambda: False
        self.data = tempfile.TemporaryDirectory()
        main.setup({"settings": {}, "dataDir": self.data.name, "locale": "en"})

    def tearDown(self):
        (main.BLOCKED_RETRY_DELAYS_SEC, main.BLOCKED_RETRY_DELAYS_QUICK_SEC, main.update_yt_dlp,
         main._firefox_found) = self.saved
        self.data.cleanup()

    def test_import_goes_through_after_two_blocks(self):
        call, calls = flaky(2)
        messages = []
        self.assertEqual(main._run(call, lambda p, m: messages.append(m), update=True), {"ok": True})
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(messages), 2)
        self.assertIn("(1/3)", messages[0])

    def test_import_blocked_every_time_is_reported(self):
        call, calls = flaky(99)
        with self.assertRaises(main.YouTubeError):
            main._run(call, update=True)
        self.assertEqual(len(calls), 4)  # first try + 3 retries

    def test_search_tries_once_more(self):
        call, calls = flaky(1)
        self.assertEqual(main._run(call), {"ok": True})
        call, calls = flaky(99)
        with self.assertRaises(main.YouTubeError):
            main._run(call)
        self.assertEqual(len(calls), 2)

    def test_other_errors_are_not_retried(self):
        call, calls = flaky(99, "ERROR: [youtube] abc: Private video")
        with self.assertRaises(RuntimeError):
            main._run(call, update=True)
        self.assertEqual(len(calls), 1)

    def test_cancel_stops_the_wait(self):
        main.BLOCKED_RETRY_DELAYS_SEC = (30,)
        call, calls = flaky(99)
        polls = []

        def cancel():
            polls.append(1)
            if len(polls) > 1:
                raise Cancelled()

        with self.assertRaises(Cancelled):
            main._run(call, update=True, cancel=cancel)
        self.assertEqual(len(calls), 1)

    def test_video_blocked_through_every_retry_isnt_waited_for_again(self):
        call, calls = flaky(99)
        with self.assertRaises(main.YouTubeError) as first:
            main._run(call, update=True, key="YJfHuATJYsQ")
        self.assertEqual(len(calls), 4)
        self.assertIn("kept asking on every try", str(first.exception))
        call, calls = flaky(99)
        messages = []
        with self.assertRaises(main.YouTubeError) as again:
            main._run(call, lambda p, m: messages.append(m), update=True, key="YJfHuATJYsQ")
        self.assertEqual((len(calls), messages), (1, []))  # one try, no wait
        self.assertIn("blocked the last time too", str(again.exception))
        call, calls = flaky(99)
        with self.assertRaises(main.YouTubeError):
            main._run(call, update=True, key="dQw4w9WgXcQ")  # another video still gets the full wait
        self.assertEqual(len(calls), 4)

    def test_remembered_block_expires_and_success_forgets_it(self):
        main._remember_blocked("YJfHuATJYsQ", True)
        self.assertTrue(main._blocked_before("YJfHuATJYsQ"))
        call, calls = flaky(0)
        self.assertEqual(main._run(call, update=True, key="YJfHuATJYsQ"), {"ok": True})
        self.assertFalse(main._blocked_before("YJfHuATJYsQ"))
        main._remember_blocked("YJfHuATJYsQ", True)
        saved = main.BLOCKED_REMEMBER_SEC
        main.BLOCKED_REMEMBER_SEC = 0
        try:
            self.assertFalse(main._blocked_before("YJfHuATJYsQ"))
        finally:
            main.BLOCKED_REMEMBER_SEC = saved

    def test_video_key(self):
        for url in ("https://www.youtube.com/watch?v=YJfHuATJYsQ&t=5", "https://youtu.be/YJfHuATJYsQ",
                    "https://m.youtube.com/shorts/YJfHuATJYsQ", "https://www.youtube.com/watch?list=x&v=YJfHuATJYsQ"):
            self.assertEqual(main._video_key(url), "YJfHuATJYsQ")


class LanguageTest(unittest.TestCase):
    def setUp(self):
        self.saved = main._firefox_found
        main._firefox_found = lambda: False

    def tearDown(self):
        main._firefox_found = self.saved
        main.setup({"settings": {}})

    def blocked(self, locale=None, **settings):
        main.setup({"settings": settings, **({"locale": locale} if locale else {})})
        return main.readable_error("blocked", RuntimeError(BLOCKED))

    def test_app_locale_wins(self):
        for locale, lang in (("en", "en"), ("zh-TW", "zh-TW"), ("zh-Hant", "zh-TW"), ("ja", "ja"), ("ko", "ko"),
                             ("ko-KR", "ko"), ("de", "en")):
            main.setup({"settings": {}, "locale": locale})
            self.assertEqual(main._lang(), lang, locale)

    def test_without_app_locale_the_system_language_is_used(self):
        main.setup({"settings": {}})
        self.assertIn(main._lang(), ("en", "zh-TW", "ja", "ko"))

    def test_every_message_in_every_language(self):
        for table in (main.MESSAGES, main.BLOCKED_HINTS, main.PROGRESS):
            for key, texts in table.items():
                self.assertEqual(set(texts), {"en", "zh-TW", "ja", "ko"}, key)

    def test_retry_progress_follows_the_app(self):
        main.setup({"settings": {}, "locale": "zh-TW"})
        self.assertEqual(main._text("retry", delay=5, attempt=1, total=3), "YouTube 要求確認不是機器人，5 秒後重試（1/3）…")
        main.setup({"settings": {}, "locale": "ko"})
        self.assertIn("5초 후", main._text("retry", delay=5, attempt=1, total=3))

    def test_blocked_message_in_the_app_language(self):
        err = self.blocked("zh-TW")
        self.assertEqual(type(err).__name__, "YouTube 要求確認你不是機器人")
        self.assertIn("每次重試都被擋下", str(err))
        self.assertIn("這樣做：Windows", str(err))  # no space between Chinese sentences
        self.assertIn("選擇它。或者", str(err))
        self.assertIn("기다려", str(self.blocked("ko")))

    def test_blocked_without_firefox_on_windows_explains_cookies_txt(self):
        text = str(self.blocked("en"))
        if sys.platform == "win32":
            self.assertIn("Chrome and Edge can’t be used", text)
            self.assertIn("cookies.txt", text)
        else:
            self.assertIn("a browser you’re signed in", text)
        self.assertIn("another network", text)

    def test_blocked_with_firefox_points_to_the_setting(self):
        main._firefox_found = lambda: True
        text = str(self.blocked("en"))
        self.assertIn("Firefox is installed", text)
        self.assertNotIn("Chrome and Edge", text)

    def test_blocked_while_signed_in(self):
        self.assertIn("already uses your YouTube sign-in from Firefox", str(self.blocked("en", cookiesFromBrowser="firefox")))
        self.assertIn("already uses your cookies.txt", str(self.blocked("en", cookiesFile="C:\\c.txt")))
        self.assertIn("目前已經使用 Firefox", str(self.blocked("zh-TW", cookiesFromBrowser="firefox")))

    def test_firefox_check_reads_no_cookies(self):
        with tempfile.TemporaryDirectory() as home:
            place = {"win32": os.path.join(home, "Mozilla", "Firefox"),
                     "darwin": os.path.join(home, "Library", "Application Support", "Firefox")}.get(
                sys.platform, os.path.join(home, ".mozilla", "firefox"))
            env = {"APPDATA": home, "HOME": home, "USERPROFILE": home}
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                self.assertFalse(self.saved())
                os.makedirs(os.path.join(place, "Profiles", "abc.default-release"))
                with open(os.path.join(place, "profiles.ini"), "w"):
                    pass
                self.assertFalse(self.saved())  # a profile Firefox never used
                with open(os.path.join(place, "Profiles", "abc.default-release", "cookies.sqlite"), "w"):
                    pass
                self.assertTrue(self.saved())
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
