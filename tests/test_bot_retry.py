"""Bot-check retries in main._run, the messages' language and the sign-in window (no network, no browser):
python -m unittest discover tests"""

import os
import sys
import tempfile
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
        self.saved = (main.BLOCKED_RETRY_DELAYS_SEC, main.BLOCKED_RETRY_DELAYS_QUICK_SEC, main.update_yt_dlp)
        main.BLOCKED_RETRY_DELAYS_SEC = (0.01, 0.01, 0.01)
        main.BLOCKED_RETRY_DELAYS_QUICK_SEC = (0.01,)
        main.update_yt_dlp = lambda *a, **k: False  # no pip in tests
        self.chromium, main._chromium = main._chromium, lambda: None
        self.data = tempfile.TemporaryDirectory()
        main.setup({"settings": {}, "dataDir": self.data.name, "locale": "en"})

    def tearDown(self):
        main.BLOCKED_RETRY_DELAYS_SEC, main.BLOCKED_RETRY_DELAYS_QUICK_SEC, main.update_yt_dlp = self.saved
        main._chromium = self.chromium
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
    def tearDown(self):
        main.setup({"settings": {}})

    def blocked(self, locale=None):
        main.setup({"settings": {}, **({"locale": locale} if locale else {})})
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
        langs = {"en", "zh-TW", "ja", "ko"}
        for table in (main.MESSAGES, main.BLOCKED_HINTS, main.PROGRESS):
            for key, by_lang in table.items():
                self.assertEqual(set(by_lang), langs, key)
        for by_lang in (main.ACTION_SIGNIN, main.WARNING_PAGE, main.CAP_PAGE, main.MAIN_ACCOUNT):
            self.assertEqual(set(by_lang), langs)

    def test_retry_progress_follows_the_app(self):
        main.setup({"settings": {}, "locale": "zh-TW"})
        self.assertEqual(main._text("retry", delay=5, attempt=1, total=3), "YouTube 要求確認不是機器人，5 秒後重試（1/3）…")
        main.setup({"settings": {}, "locale": "ko"})
        self.assertIn("5초 후", main._text("retry", delay=5, attempt=1, total=3))

    def test_blocked_message_in_the_app_language(self):
        err = self.blocked("zh-TW")
        self.assertEqual(type(err).__name__, "YouTube 要求確認你不是機器人")
        self.assertEqual(str(err), "每次重試都被擋下，再等一下也沒用。請換一個網路（例如手機熱點）、關閉 VPN 或 Proxy，或等幾個小時再試。")
        self.assertIn("기다려", str(self.blocked("ko")))
        self.assertEqual(str(self.blocked("en")), "It kept asking on every try, so waiting a minute won’t help. Switch to "
                         "another network (a phone hotspot), turn off any VPN or proxy, or wait a few hours.")

    def test_title_in_the_message_from_app_0_2_8(self):
        # 0.2.8 and later show only the message (the extension protocol's Errors); "dev" is a source checkout
        for version in ("0.2.8", "0.3.0", "dev"):
            main.setup({"settings": {}, "locale": "zh-TW", "appVersion": version})
            err = main.readable_error("blocked", RuntimeError(BLOCKED))
            self.assertEqual(type(err), main.YouTubeError, version)
            self.assertTrue(str(err).startswith("YouTube 要求確認你不是機器人：每次重試都被擋下"), version)
        main.setup({"settings": {}, "locale": "en", "appVersion": "0.2.8"})
        self.assertTrue(str(main.readable_error("blocked", RuntimeError(BLOCKED))).startswith(
            "YouTube asked to confirm you’re not a bot: It kept asking"))
        # older apps put the class name in front: the title stays the class name there
        for version in ("0.2.7", "0.1.0", None):
            main.setup({"settings": {}, "locale": "zh-TW", **({"appVersion": version} if version else {})})
            err = main.readable_error("blocked", RuntimeError(BLOCKED))
            self.assertEqual(type(err).__name__, "YouTube 要求確認你不是機器人", version)
            self.assertTrue(str(err).startswith("每次重試都被擋下"), version)
            self.assertTrue(err.sentence.startswith("YouTube 要求確認你不是機器人：每次"), version)

    def test_no_settings_left_to_point_to(self):
        for lang in ("en", "zh-TW", "ja", "ko"):
            for kind, texts in main.MESSAGES.items():
                for text in texts[lang]:
                    for gone in ("Use my YouTube sign-in", "使用瀏覽器的 YouTube 登入狀態", "cookies.txt ファイル",
                                 "브라우저의 YouTube 로그인 사용", "separate window”", "在獨立視窗登入 YouTube」"):
                        self.assertNotIn(gone, text, (kind, lang))


if __name__ == "__main__":
    unittest.main()
