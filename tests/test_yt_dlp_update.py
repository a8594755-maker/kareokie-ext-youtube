"""Offline updater regression tests using real subprocesses and fake pip."""

import io
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main


class Cancelled(Exception):
    pass


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
        self._check_tree_cleanup("timeout")

    def test_cancel_kills_pip_its_child_and_grandchild(self):
        self._check_tree_cleanup("cancel")

    def test_normal_exit_kills_leftover_descendants(self):
        self._check_tree_cleanup("success")

    def _check_tree_cleanup(self, exit_path):
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
        cancelled = Event()

        def cancel():
            if cancelled.is_set():
                raise Cancelled("import cancelled")

        with tempfile.TemporaryDirectory() as folder, socket.create_server(("127.0.0.1", 0)) as server, \
             ThreadPoolExecutor(max_workers=1) as executor, ExitStack() as connections:
            script = Path(folder) / "tree.py"
            script.write_text(tree, encoding="utf-8")
            server.settimeout(2)
            cmd = [sys.executable, str(script), str(server.getsockname()[1]), "2"]
            with patch.object(main, "PIP_TIMEOUT_SEC", 4 if exit_path == "timeout" else 12):
                pip = executor.submit(main._run_pip, cmd, cancel)
                sockets = {}
                for _ in range(3):
                    conn, _ = server.accept()
                    connections.enter_context(conn)
                    conn.settimeout(2)
                    sockets[conn.recv(1)] = conn
                self.assertEqual(sockets.keys(), {b"\x00", b"\x01", b"\x02"})
                if exit_path == "timeout":
                    with self.assertRaises(subprocess.TimeoutExpired):
                        pip.result(timeout=6)
                elif exit_path == "cancel":
                    cancelled.set()
                    with self.assertRaises(Cancelled):
                        pip.result(timeout=2)
                else:
                    sockets[b"\x02"].sendall(b"1")
                    self.assertEqual(pip.result(timeout=2).returncode, 0)
            for conn in sockets.values():
                try:
                    self.assertEqual(conn.recv(1), b"", "a pip descendant survived the timeout")
                except ConnectionResetError:
                    pass  # Windows can close a killed process's socket with a reset.

    def test_posix_group_is_killed_in_finally(self):
        cmd = ["pip"]
        timeout = subprocess.TimeoutExpired(cmd, 90)
        for outcome in (0, timeout, Cancelled(), KeyboardInterrupt()):
            with self.subTest(outcome=outcome), patch.object(main.sys, "platform", "darwin"), \
                 patch.object(main.subprocess, "Popen") as spawn, \
                 patch.object(main.os, "killpg", create=True) as killpg, \
                 patch.object(signal, "SIGKILL", 9, create=True):
                proc = spawn.return_value.__enter__.return_value
                proc.pid = 123
                if isinstance(outcome, BaseException):
                    proc.wait.side_effect = outcome
                    with self.assertRaises(type(outcome)):
                        main._run_pip(cmd)
                else:
                    killpg.side_effect = PermissionError
                    proc.wait.return_value = outcome
                    self.assertEqual(main._run_pip(cmd).returncode, outcome)
                killpg.assert_called_once_with(proc.pid, signal.SIGKILL)
                self.assertIs(spawn.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertTrue(spawn.call_args.kwargs["start_new_session"])


class UpdateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.ext = self.root / "ext"
        package = self.ext / "_deps" / "yt_dlp"
        package.mkdir(parents=True)
        today = datetime.now(timezone.utc).date()
        self.version = today.isoformat().replace("-", ".")
        self.latest = (today + timedelta(days=1)).isoformat().replace("-", ".")
        (package / "version.py").write_text(f"__version__ = {self.version!r}", encoding="utf-8")
        self.addCleanup(main.setup, dict(main._context))
        main.setup({"dataDir": str(self.data)})
        self.addCleanup(patch.stopall)
        patch.object(main, "HERE", str(self.ext)).start()

    def test_pip_failure_and_timeout_keep_the_107_recheck_intervals(self):
        for result in (subprocess.CompletedProcess([], 1, "fake pip failed"), subprocess.TimeoutExpired("pip", 90)):
            with self.subTest(result=result):
                clock = time.time()
                main._write_state({"checkedAt": 0})
                with patch.object(main.time, "time", return_value=clock) as now, \
                     patch.object(main, "_latest_release", return_value=(self.latest, "yt-dlp-ejs")) as latest, \
                     patch.object(main, "_run_pip") as pip:
                    def fake_pip(cmd, cancel):
                        Path(cmd[cmd.index("--target") + 1]).mkdir()
                        if isinstance(result, Exception):
                            raise result
                        return result

                    pip.side_effect = fake_pip
                    self.assertFalse(main.update_yt_dlp())
                    state = main._read_state()
                    self.assertEqual(state["checkedAt"], clock)
                    self.assertEqual(state["latest"], self.latest)
                    self.assertEqual(main._active_version(), self.version)
                    now.return_value = clock + main.RECHECK_AFTER_ERROR_SEC - 1
                    self.assertFalse(main.update_yt_dlp())
                    self.assertFalse(main.update_yt_dlp(after_error=True))
                    self.assertEqual(latest.call_count, 1)
                    now.return_value += 1
                    self.assertFalse(main.update_yt_dlp())
                    self.assertEqual(latest.call_count, 1)
                    self.assertFalse(main.update_yt_dlp(after_error=True))
                    self.assertEqual(latest.call_count, 2)
                    now.return_value += main.UPDATE_EVERY_SEC
                    self.assertFalse(main.update_yt_dlp())
                    self.assertEqual(latest.call_count, 3)
                    self.assertFalse(list(Path(main._store()).glob("*.tmp")))

    def test_import_retries_after_a_repairable_failure(self):
        with patch.object(main, "update_yt_dlp", return_value=True) as update:
            calls = []

            def attempt():
                calls.append("import")
                if len(calls) == 1:
                    update.assert_not_called()
                    raise RuntimeError("ERROR: Requested format is not available")
                update.assert_called_once_with(None, after_error=True, cancel=None)
                return "resolved"

            self.assertEqual(main._run(attempt, update=True), "resolved")

    def test_resolve_updates_before_downloading_and_retries_busy_replaces(self):
        output = self.root / "raw_audio.webm"
        output.touch()
        store = Path(main._store())
        other_stage = store / f"{self.latest}.{os.getpid() + 1}.tmp"
        other_stage.mkdir(parents=True)
        replace = os.replace
        replaced = {}

        def busy_replace(source, dest):
            self.assertTrue(source.endswith(f".{os.getpid()}.tmp"))
            replaced[dest] = replaced.get(dest, 0) + 1
            if replaced[dest] == 1:
                raise PermissionError("destination is still open")
            replace(source, dest)

        def fake_pip(cmd, cancel):
            target = Path(cmd[cmd.index("--target") + 1]) / "yt_dlp"
            target.mkdir(parents=True)
            (target / "version.py").write_text(f"__version__ = {self.latest!r}", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "")

        def download(*args):
            self.assertEqual(main._active_version(), self.latest)
            return {"requested_downloads": [{"filepath": str(output)}]}

        with io.TextIOWrapper(io.BytesIO(), encoding="cp1252") as log, redirect_stdout(log), \
             patch.object(main, "_latest_release", return_value=(self.latest, "yt-dlp-ejs")), \
             patch.object(main, "_run_pip", side_effect=fake_pip), patch.object(main, "_download", side_effect=download), \
             patch.object(main, "ensure_js_runtime"), patch.object(main.os, "replace", side_effect=busy_replace):
            result = main.resolve("YJfHuATJYsQ", str(self.root), lambda *a: None, lambda: None)
            log.seek(0)
            self.assertIn(f"{self.version} -> {self.latest}", log.read())
        self.assertEqual(result["audio_path"], str(output))
        self.assertEqual(replaced, {str(store / "state.json"): 3, str(store / self.latest): 2})
        self.assertEqual(list(store.glob("*.tmp")), [other_stage])

    def test_resolve_does_not_swallow_cancel_or_start_the_download(self):
        cancelled = Event()

        def cancel():
            if cancelled.is_set():
                raise Cancelled("import cancelled")

        def fake_pip(cmd, poll_cancel):
            Path(cmd[cmd.index("--target") + 1]).mkdir()
            cancelled.set()
            poll_cancel()

        with patch.object(main, "_latest_release", return_value=(self.latest, "yt-dlp-ejs")), \
             patch.object(main, "_run_pip", side_effect=fake_pip), patch.object(main, "_download") as download:
            with self.assertRaises(Cancelled):
                main.resolve("YJfHuATJYsQ", str(self.root), lambda *a: None, cancel)
            download.assert_not_called()
        store = Path(main._store())
        self.assertFalse((store / ".lock").exists())
        self.assertFalse(list(store.glob("*.tmp")))

    def test_cancel_during_pypi_lookup_releases_the_lock_without_starting_pip(self):
        started, release = Event(), Event()

        def latest():
            started.set()
            release.wait(5)
            return self.latest, "yt-dlp-ejs"

        def cancel():
            if started.is_set():
                raise Cancelled("import cancelled")

        try:
            with patch.object(main, "_latest_release", side_effect=latest), patch.object(main, "_run_pip") as pip:
                with self.assertRaises(Cancelled):
                    main.update_yt_dlp(cancel=cancel)
                pip.assert_not_called()
        finally:
            release.set()
        self.assertFalse((Path(main._store()) / ".lock").exists())

    def test_error_triggered_update_also_honors_cancel(self):
        def attempt():
            raise RuntimeError("ERROR: Requested format is not available")

        def cancel():
            raise Cancelled("import cancelled")

        with patch.object(main, "_latest_release") as latest:
            with self.assertRaises(Cancelled):
                main._run(attempt, update=True, cancel=cancel)
            latest.assert_not_called()
