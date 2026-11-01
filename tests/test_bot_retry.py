"""Bot-check retries in main._run (no network): python -m unittest discover tests"""

import os
import sys
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
        main._settings.clear()

    def tearDown(self):
        main.BLOCKED_RETRY_DELAYS_SEC, main.BLOCKED_RETRY_DELAYS_QUICK_SEC, main.update_yt_dlp = self.saved

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


if __name__ == "__main__":
    unittest.main()
