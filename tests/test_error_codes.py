"""Error codes for the app's crash reports (app 0.3.2, PROTOCOL.md "Errors"), no network:
python -m unittest discover tests"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import main  # noqa: E402

# the app's rule for a code it accepts (runner.py ERROR_CODE)
APP_RULE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
BLOCKED = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser"


def raised(call, **kw):
    try:
        main._run(call, **kw)
    except Exception as exc:  # noqa: BLE001
        return exc
    raise AssertionError("no error")


def failing(message, cls=RuntimeError):
    def call():
        raise cls(message)
    return call


class ErrorCodes(unittest.TestCase):
    def setUp(self):
        main._context.clear()
        main._context.update({"locale": "en", "appVersion": "0.3.2"})

    def test_every_code_is_one_the_app_accepts(self):
        codes = [c for _, c in main._CHANGED_CODES] + [c for c, _ in main._CAUSE_CODES]
        codes += [k for k in main.MESSAGES if k not in ("retry", "updating", "deno", "signin-wait")]
        codes += ["changed", "blocked.again", main.NoAudio.error_code]
        for code in codes:
            self.assertRegex(code, APP_RULE)

    def test_a_change_says_which_sign(self):
        cases = {
            "ERROR: [youtube] abc: Requested format is not available. Use --list-formats": "changed.format",
            "ERROR: [youtube] abc: nsig extraction failed: You may experience throttling": "changed.nsig",
            "ERROR: unable to download video data: HTTP Error 403: Forbidden": "changed.http403",
            "ERROR: [youtube] abc: Only images are available for download": "changed.images",
        }
        for message, code in cases.items():
            err = raised(failing(message))
            self.assertIsInstance(err, main.YouTubeError, message)
            self.assertEqual(err.error_code, code, message)

    def test_a_bot_check_is_blocked(self):
        err = main.readable_error("blocked", RuntimeError(BLOCKED))
        self.assertEqual(err.error_code, "blocked")
        self.assertEqual(main.readable_error("blocked", RuntimeError(BLOCKED), again=True).error_code, "blocked.again")

    def test_errors_passed_through_get_their_cause(self):
        cases = {
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access": "private",
            "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate": "age_restricted",
            "ERROR: [youtube] abc: Join this channel to get access to members-only content": "members_only",
            "ERROR: [youtube] abc: The uploader has not made this video available in your country": "region_blocked",
            "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader": "unavailable",
            "ERROR: unable to download webpage: HTTP Error 429: Too Many Requests": "rate_limited",
            "ERROR: [youtube] abc: Premieres in 3 hours": "live_not_started",
            "<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed>": "certificate",
            "ERROR: unable to download webpage: <urlopen error timed out>": "network",
        }
        for message, code in cases.items():
            err = raised(failing(message))
            self.assertNotIsInstance(err, main.YouTubeError, message)  # unchanged: the app explains these
            self.assertEqual(str(err), message)
            self.assertEqual(getattr(err, "error_code", None), code, message)

    def test_an_unknown_error_stays_without_a_code(self):
        err = raised(failing("ERROR: something nobody has seen before"))
        self.assertIsNone(getattr(err, "error_code", None))

    def test_an_existing_code_is_kept(self):
        exc = RuntimeError("timed out")
        exc.error_code = "mine"
        self.assertEqual(main.tagged(exc).error_code, "mine")

    def test_old_apps_class_named_errors_carry_it_too(self):
        main._context["appVersion"] = "0.2.5"
        err = main.readable_error("changed", RuntimeError("nsig extraction failed"))
        self.assertNotEqual(type(err), main.YouTubeError)  # the subclass named after the title
        self.assertEqual(err.error_code, "changed.nsig")

    def test_the_sign_in_errors_carry_their_kind(self):
        err = main.readable_error("signin-expired", RuntimeError("cookies"), action="signin")
        self.assertEqual(err.error_code, "signin-expired")
        self.assertEqual(err.action["id"], "signin")


if __name__ == "__main__":
    unittest.main()
