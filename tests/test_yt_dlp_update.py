"""Offline updater regression tests using real subprocesses and fake pip."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date, timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


class PipProcessTest(unittest.TestCase):
    def test_pip_gets_eof_even_with_the_host_stdin_pipe_open(self):
        fake_pip = textwrap.dedent("""
            import os, sys
            assert sys.stdin.read(1) == ""
            assert os.environ["PIP_NO_INPUT"] == "1"
            assert os.environ["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
            assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
            assert os.environ["PIP_KEYRING_PROVIDER"] == "disabled"
            print("read EOF")
        """)
        run_pip = textwrap.dedent("""
            import main, sys
            main.PIP_TIMEOUT_SEC = 4
            result = main._run_pip([sys.executable, "-c", sys.argv[1]])
            assert (result.returncode, result.stdout.strip()) == (0, "read EOF"), result
        """)
        with subprocess.Popen([sys.executable, "-c", run_pip, fake_pip], stdin=subprocess.PIPE,
                              cwd=ROOT) as runner:
            try:
                self.assertEqual(runner.wait(timeout=8), 0)
            finally:
                if runner.poll() is None:
                    runner.kill()

    def test_timeout_kills_pip_its_child_and_grandchild(self):
        # Each process holds a connection open. EOF verifies it exited, even if its PID
        # remains visible briefly on POSIX while it is waiting to be reaped.
        tree = textwrap.dedent("""
            import socket, subprocess, sys
            port, depth = map(int, sys.argv[1:])
            with socket.create_connection(("127.0.0.1", port)) as conn:
                conn.sendall(bytes([depth]))
                if depth:
                    subprocess.Popen([sys.executable, __file__, str(port), str(depth - 1)],
                                     stdin=subprocess.DEVNULL)
                conn.settimeout(15)
                conn.recv(1)
        """)
        with tempfile.TemporaryDirectory() as folder, socket.create_server(("127.0.0.1", 0)) as server, \
             ThreadPoolExecutor(max_workers=1) as executor, ExitStack() as connections:
            script = Path(folder) / "tree.py"
            script.write_text(tree, encoding="utf-8")
            server.settimeout(2)
            cmd = [sys.executable, str(script), str(server.getsockname()[1]), "2"]
            with patch.object(main, "PIP_TIMEOUT_SEC", 4):
                pip = executor.submit(main._run_pip, cmd)
                sockets = {}
                for _ in range(3):
                    conn, _ = server.accept()
                    connections.enter_context(conn)
                    conn.settimeout(2)
                    sockets[conn.recv(1)] = conn
                self.assertEqual(sockets.keys(), {b"\x00", b"\x01", b"\x02"})
                with self.assertRaises(subprocess.TimeoutExpired):
                    pip.result(timeout=6)
            for conn in sockets.values():
                try:
                    self.assertEqual(conn.recv(1), b"", "a pip descendant survived the timeout")
                except ConnectionResetError:
                    pass  # Windows can close a killed process's socket with a reset.


class UpdateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.ext = self.root / "ext"
        package = self.ext / "_deps" / "yt_dlp"
        package.mkdir(parents=True)
        self.version = date.today().isoformat().replace("-", ".")
        self.latest = (date.today() + timedelta(days=1)).isoformat().replace("-", ".")
        (package / "version.py").write_text(f"__version__ = {self.version!r}", encoding="utf-8")
        self.addCleanup(main.setup, dict(main._context))
        main.setup({"dataDir": str(self.data)})
        self.addCleanup(patch.stopall)
        patch.object(main, "HERE", str(self.ext)).start()

    def test_recent_and_old_versions_use_different_check_intervals(self):
        state = {"checkedAt": time.time() - 3600}
        self.assertFalse(main._update_due(state))
        old = (date.today() - timedelta(days=main.RECENT_YT_DLP_DAYS + 1)).isoformat().replace("-", ".")
        with patch.object(main, "_active_version", return_value=old):
            self.assertTrue(main._update_due(state))

    def test_pip_failure_and_timeout_persist_and_back_off(self):
        for result in (subprocess.CompletedProcess([], 1, "fake pip failed"), subprocess.TimeoutExpired("pip", 90)):
            with self.subTest(result=result):
                clock = time.time()
                main._write_state({"checkedAt": 0})
                with patch.object(main.time, "time", return_value=clock) as now, \
                     patch.object(main, "_latest_release", return_value=(self.latest, "yt-dlp-ejs")) as latest, \
                     patch.object(main, "_run_pip") as pip:
                    def fake_pip(cmd):
                        Path(cmd[cmd.index("--target") + 1]).mkdir()
                        if isinstance(result, Exception):
                            raise result
                        return result

                    pip.side_effect = fake_pip
                    self.assertFalse(main.update_yt_dlp())
                    state = main._read_state()
                    self.assertEqual(state["checkedAt"], clock)
                    self.assertIn("updateError", state)
                    self.assertEqual(main._active_version(), self.version)
                    now.return_value = clock + main.RECHECK_AFTER_ERROR_SEC - 1
                    self.assertFalse(main.update_yt_dlp())
                    self.assertFalse(main.update_yt_dlp(after_error=True))
                    self.assertEqual(latest.call_count, 1)
                    now.return_value += 1
                    self.assertFalse(main.update_yt_dlp())
                    self.assertEqual(latest.call_count, 2)
                    self.assertFalse((Path(main._store()) / (self.latest + ".tmp")).exists())

    def test_import_retries_after_a_repairable_failure(self):
        with patch.object(main, "update_yt_dlp", return_value=True) as update:
            calls = []

            def attempt():
                calls.append("import")
                if len(calls) == 1:
                    update.assert_not_called()
                    raise RuntimeError("ERROR: Requested format is not available")
                update.assert_called_once_with(None, after_error=True)
                return "resolved"

            self.assertEqual(main._run(attempt, update=True), "resolved")

    def test_recent_version_resolves_while_the_background_pip_waits(self):
        release = self.root / "release.json"
        release.write_text(json.dumps({"info": {"version": self.latest}}), encoding="utf-8")
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        (self.ext / "main.py").write_text(source.replace(main.PYPI_URL, release.as_uri(), 1), encoding="utf-8")
        ready, go = self.root / "started", self.root / "proceed"
        song = self.root / "song"
        song.mkdir()
        fake_modules = self.root / "fake_modules"
        fake_modules.mkdir()
        (fake_modules / "pip.py").write_text(textwrap.dedent("""
            import sys, time
            from pathlib import Path

            Path({ready!r}).touch()
            for _ in range(600):
                if Path({go!r}).exists():
                    break
                time.sleep(0.02)
            else:
                sys.exit("test never released pip")
            target = Path(sys.argv[sys.argv.index("--target") + 1]) / "yt_dlp"
            target.mkdir(parents=True)
            (target / "version.py").write_text({version_file!r}, encoding="utf-8")
        """).format(ready=str(ready), go=str(go), version_file=f"__version__ = {self.latest!r}"), encoding="utf-8")
        runner = textwrap.dedent("""
            import sys
            from pathlib import Path
            from unittest.mock import patch
            import main

            main.setup({"dataDir": sys.argv[1]})
            output = Path(sys.argv[2]) / "raw_audio.webm"
            output.touch()
            def download(*args):
                assert main._active_version() == sys.argv[3]
                assert not Path(sys.argv[4]).exists()
                return {"requested_downloads": [{"filepath": str(output)}]}
            with patch.object(main, "_download", download), patch.object(main, "ensure_js_runtime"):
                result = main.resolve("YJfHuATJYsQ", sys.argv[2], lambda *a: None, lambda: None)
            assert result["audio_path"] == str(output)
            print("resolved")
        """)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(self.ext), str(fake_modules))))
        try:
            result = subprocess.run([sys.executable, "-c", runner, str(self.data), str(song),
                                     self.version, str(ready)], cwd=song, env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8, check=True)
            self.assertEqual(result.stdout.strip(), "resolved")
            self.assertTrue(wait_for(ready.is_file), "background pip did not start after the runner exited")
            self.assertEqual(main._active_version(), self.version)
            song.rename(self.root / "completed-song")
        finally:
            go.touch()
        self.assertTrue(wait_for(lambda: main._active_version() == self.latest))
        self.assertTrue(wait_for(lambda: not (Path(main._store()) / ".lock").exists()))
