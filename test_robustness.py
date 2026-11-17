# -*- coding: utf-8 -*-
"""Chrome 端口锁、断线重连、看门狗补跑。不启动真实浏览器。

运行：
    python -m unittest test_robustness -v
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from unittest import mock

import chrome_lock
import collect
import login
import match_engine
import run_daily
import run_state
import settings
import watchdog


def _hold_file_lock(lock_path: str, ready: str, release: str, log_path: str) -> None:
    """子进程：拿住文件锁，直到父进程写下 release 文件。"""
    lock = chrome_lock.ChromeLock(
        port=9333,
        timeout=10,
        kind="file",
        lock_path=Path(lock_path),
        log_path=Path(log_path),
        echo=False,
        poll=0.05,
    )
    lock.acquire()
    Path(ready).write_text("ready", encoding="utf-8")
    deadline = time.time() + 20
    while time.time() < deadline and not Path(release).exists():
        time.sleep(0.02)
    lock.release()
    Path(release + ".done").write_text("1", encoding="utf-8")


class _Clock:
    def __init__(self, at: datetime):
        self.dt = at
        self.mono = 0.0

    def now(self) -> datetime:
        return self.dt

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        seconds = float(seconds or 0)
        self.mono += seconds
        self.dt += timedelta(seconds=seconds)


class _ScriptedBackend:
    def __init__(self, results: list[bool]):
        self.results = list(results)
        self.abandoned = False
        self.releases = 0

    def try_acquire(self, timeout: float) -> bool:
        ok = self.results.pop(0) if self.results else False
        if not ok and timeout > 0:
            time.sleep(timeout)
        return ok

    def release(self) -> None:
        self.releases += 1


class _PageSet:
    class _LoadMode:
        def eager(self):
            return None

    load_mode = _LoadMode()

    def timeouts(self, **kwargs):
        return None


class _FakePage:
    def __init__(self, options):
        self.set = _PageSet()
        self.options = options

    def quit(self, timeout=0, force=False):
        return None


class TestChromeLock(unittest.TestCase):
    def _lock(self, directory: Path, **kwargs) -> chrome_lock.ChromeLock:
        events: list[dict] = []
        kwargs.setdefault("port", 9333)
        kwargs.setdefault("kind", "file")
        kwargs.setdefault("echo", False)
        kwargs.setdefault("poll", 0.05)
        kwargs.setdefault("lock_path", directory / "chrome.lock")
        kwargs.setdefault("log_path", directory / "chrome_lock.log")
        kwargs.setdefault("on_event", events.append)
        lock = chrome_lock.ChromeLock(**kwargs)
        lock.events = events  # type: ignore[attr-defined]
        return lock

    def test_immediate_acquire_logs_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            lock = self._lock(directory, timeout=2)
            lock.acquire()
            self.assertTrue(lock.held_by_current_thread())
            self.assertTrue(lock.owner_path.exists())
            lock.release()
            self.assertFalse(lock.held_by_current_thread())
            self.assertFalse(lock.owner_path.exists())
            names = [row["event"] for row in lock.events]  # type: ignore[attr-defined]
            self.assertIn("acquire", names)
            self.assertIn("release", names)
            self.assertNotIn("timeout", names)
            text = (directory / "chrome_lock.log").read_text(encoding="utf-8")
            self.assertIn('"event": "acquire"', text)
            self.assertIn('"event": "release"', text)

    def test_reenter_keeps_the_os_lock_until_the_outer_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            lock = self._lock(directory, timeout=1)
            waiter = self._lock(directory, timeout=0.2, poll=0.05, log_path=directory / "waiter.log")
            lock.acquire()
            lock.acquire()
            with self.assertRaises(chrome_lock.ChromeLockTimeout):
                waiter.acquire()
            lock.release()
            with self.assertRaises(chrome_lock.ChromeLockTimeout):
                waiter.acquire()
            lock.release()
            waiter.acquire()
            waiter.release()
            self.assertFalse(lock.held_by_current_thread())
            timeout_events = [row["event"] for row in waiter.events]  # type: ignore[attr-defined]
            self.assertIn("timeout", timeout_events)
            self.assertIn("wait", timeout_events)

    def test_scripted_backend_logs_wait_then_acquire(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            backend = _ScriptedBackend([False, True])
            lock = self._lock(directory, timeout=2, poll=0.2, backend=backend)
            lock.acquire()
            names = [row["event"] for row in lock.events]  # type: ignore[attr-defined]
            self.assertEqual(names[0], "wait")
            self.assertIn("acquire", names)
            lock.release()
            self.assertEqual(backend.releases, 1)

    def test_scripted_backend_timeout_does_not_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            backend = _ScriptedBackend([False, False, False, False, False])
            lock = self._lock(directory, timeout=0.25, poll=0.1, backend=backend)
            with self.assertRaises(chrome_lock.ChromeLockTimeout):
                lock.acquire()
            names = [row["event"] for row in lock.events]  # type: ignore[attr-defined]
            self.assertIn("wait", names)
            self.assertIn("timeout", names)
            self.assertNotIn("acquire", names)
            self.assertEqual(backend.releases, 0)
            text = (directory / "chrome_lock.log").read_text(encoding="utf-8")
            self.assertIn('"event": "timeout"', text)

    def test_other_process_blocks_until_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            lock_path = directory / "chrome.lock"
            ready = directory / "ready"
            release = directory / "release"
            holder_log = directory / "holder.log"
            ctx = get_context("spawn")
            proc = ctx.Process(
                target=_hold_file_lock,
                args=(str(lock_path), str(ready), str(release), str(holder_log)),
            )
            proc.start()
            try:
                deadline = time.time() + 15
                while time.time() < deadline and not ready.exists():
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "holder never acquired the lock")

                events: list[dict] = []

                def let_go():
                    time.sleep(0.4)
                    release.write_text("go", encoding="utf-8")

                threading.Thread(target=let_go, daemon=True).start()
                waiter = chrome_lock.ChromeLock(
                    port=9333,
                    timeout=5,
                    kind="file",
                    lock_path=lock_path,
                    log_path=directory / "waiter.log",
                    echo=False,
                    poll=0.05,
                    on_event=events.append,
                )
                started = time.monotonic()
                waiter.acquire()
                waited = time.monotonic() - started
                self.assertGreaterEqual(waited, 0.3)
                names = [row["event"] for row in events]
                self.assertIn("wait", names)
                self.assertIn("acquire", names)
                waiter.release()
                self.assertIn("release", [row["event"] for row in events])
                text = (directory / "waiter.log").read_text(encoding="utf-8")
                for event in ("wait", "acquire", "release"):
                    self.assertIn(f'"event": "{event}"', text)
            finally:
                release.write_text("go", encoding="utf-8")
                proc.join(5)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(2)


class TestDisconnectRecovery(unittest.TestCase):
    def test_classifier(self):
        self.assertTrue(chrome_lock.is_chrome_disconnect(
            ConnectionError("[WinError 10061] 127.0.0.1:9333")))
        self.assertTrue(chrome_lock.is_chrome_disconnect(ConnectionRefusedError("refused")))

        class PageDisconnectedError(Exception):
            pass

        self.assertTrue(chrome_lock.is_chrome_disconnect(PageDisconnectedError("tab closed")))
        self.assertTrue(chrome_lock.is_chrome_disconnect(
            RuntimeError("WebSocket disconnected"), port_alive=True))
        self.assertTrue(chrome_lock.is_chrome_disconnect(ValueError("boom"), port_alive=False))
        self.assertFalse(chrome_lock.is_chrome_disconnect(ValueError("boom"), port_alive=True))
        self.assertFalse(chrome_lock.is_chrome_disconnect(collect.BossBlocked(37, "环境异常")))
        self.assertFalse(chrome_lock.is_chrome_disconnect(collect.BossLoginRequired("login")))

    def test_restart_continues_remaining_keywords(self):
        new_page = object()
        seen: list[tuple[object, str]] = []

        def fake_search(page, keyword, page_no=1, timeout=15.0):
            seen.append((page, keyword))
            if keyword == "金融大模型" and page is not new_page:
                raise ConnectionError("Connection refused: 127.0.0.1:9333")
            return [{"title": keyword, "company": "c"}]

        old_page = object()
        with mock.patch.object(collect.time, "sleep"), \
             mock.patch.object(collect, "search", side_effect=fake_search), \
             mock.patch.object(collect, "restart_browser", return_value=new_page) as restart:
            jobs, diag = collect.collect_keywords(
                old_page,
                ["AI解决方案", "金融大模型", "解决方案总监"],
                delay=(0, 0),
                block_retries=0,
            )
        restart.assert_called_once()
        self.assertEqual(
            [job["title"] for job in jobs],
            ["AI解决方案", "金融大模型", "解决方案总监"],
        )
        self.assertEqual(diag["chrome_restarts"], 1)
        self.assertFalse(diag["chrome_lost"])
        by_kw: dict[str, list] = {}
        for page, keyword in seen:
            by_kw.setdefault(keyword, []).append(page)
        self.assertIs(by_kw["金融大模型"][0], old_page)
        self.assertIs(by_kw["金融大模型"][1], new_page)
        self.assertIs(by_kw["解决方案总监"][0], new_page)
        self.assertEqual(len(diag["keywords"]), 3)

    def test_risk_control_does_not_restart_chrome(self):
        def fake_search(page, keyword, page_no=1, timeout=15.0):
            if keyword == "中":
                raise collect.BossBlocked(37, "环境异常", keyword)
            return [{"title": keyword}]

        with mock.patch.object(collect.time, "sleep"), \
             mock.patch.object(collect, "search", side_effect=fake_search), \
             mock.patch.object(collect, "restart_browser") as restart:
            jobs, diag = collect.collect_keywords(
                object(), ["前", "中", "后"], delay=(0, 0), block_retries=0,
            )
        restart.assert_not_called()
        self.assertEqual(diag["chrome_restarts"], 0)
        self.assertEqual([job["title"] for job in jobs], ["前", "后"])
        self.assertEqual(diag["blocked"][0]["keyword"], "中")

    def test_restart_failure_returns_partial_results(self):
        def fake_search(page, keyword, page_no=1, timeout=15.0):
            if keyword == "B":
                raise ConnectionError("127.0.0.1:9333")
            return [{"title": keyword}]

        def boom(page, log=None):
            raise RuntimeError("port still dead")

        with mock.patch.object(collect.time, "sleep"), \
             mock.patch.object(collect, "search", side_effect=fake_search), \
             mock.patch.object(collect, "restart_browser", side_effect=boom):
            jobs, diag = collect.collect_keywords(
                object(), ["A", "B", "C"], delay=(0, 0), block_retries=0,
            )
        self.assertEqual([job["title"] for job in jobs], ["A"])
        self.assertTrue(diag["chrome_lost"])
        self.assertTrue(any("重启失败" in err for err in diag["errors"]))

    def test_restart_under_held_lock_can_launch_a_fresh_browser(self):
        """前任已关闭端口时，仍持锁的一方要能自己再拉起 Chrome。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            lock = chrome_lock.ChromeLock(
                port=collect.PORT,
                timeout=2,
                kind="file",
                lock_path=directory / "chrome.lock",
                log_path=directory / "chrome_lock.log",
                echo=False,
                poll=0.05,
            )
            chrome_lock.reset_for_tests()
            chrome_lock._SINGLETONS[collect.PORT] = lock
            collect._IMPLICIT_LOCK = False
            collect._ACTIVE_PAGE = None
            launched: list[list[str]] = []

            def ready():
                # 第一次（restart 自己看）和 ensure 的第一次：端口是空的。
                # ensure 清完残留后再看一次仍是空的，然后 Popen，循环里变为就绪。
                ready.n += 1
                return ready.n >= 4

            ready.n = 0

            def popen(argv, close_fds=True):
                launched.append(list(argv))
                return mock.Mock()

            lock.acquire()
            try:
                with mock.patch.object(collect, "_browser_ready", side_effect=ready), \
                     mock.patch.object(collect, "_close_profile_chrome", return_value=0), \
                     mock.patch.object(collect, "find_chrome", return_value="chrome.exe"), \
                     mock.patch.object(collect.subprocess, "Popen", side_effect=popen), \
                     mock.patch.object(collect, "ChromiumPage", _FakePage), \
                     mock.patch.object(collect.time, "sleep"):
                    page = collect.restart_browser(page=None, log=lambda msg: None)
                self.assertIsInstance(page, _FakePage)
                self.assertTrue(lock.held_by_current_thread())
                self.assertEqual(lock._depth, 1)
                self.assertTrue(launched)
                joined = " ".join(launched[0])
                self.assertIn(f"--remote-debugging-port={collect.PORT}", joined)
                self.assertIn("--user-data-dir=", joined)
            finally:
                if lock.held_by_current_thread():
                    while lock.held_by_current_thread():
                        lock.release()
                collect._IMPLICIT_LOCK = False
                collect._ACTIVE_PAGE = None
                chrome_lock.reset_for_tests()

    def test_ensure_browser_reuses_a_live_port(self):
        with mock.patch.object(collect, "_browser_ready", return_value=True), \
             mock.patch.object(collect.subprocess, "Popen") as popen, \
             mock.patch.object(collect, "_close_profile_chrome") as killed:
            collect.ensure_browser()
        popen.assert_not_called()
        killed.assert_not_called()


class TestRunOrchestration(unittest.TestCase):
    def _args(self, **overrides):
        values = dict(slot="12:30", dry_run=True, demo=False, keep_open=False, force=True, profile="")
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_browser_closes_before_the_lock_is_released(self):
        events: list[str] = []

        class Lock:
            def acquire(self):
                events.append("acquire")

            def release(self):
                events.append("release")

        page = object()
        diag = {
            "boss": 1, "keywords": [], "blocked": [], "errors": [],
            "login": None, "verify": False, "circuit": False,
            "chrome_restarts": 0, "chrome_lost": False,
        }
        job = {"title": "解决方案总监", "company": "阿里云", "score": 90, "job_id": "j1"}

        def attach():
            events.append("attach")
            return page

        def gather(page_obj, slot):
            events.append("gather")
            return [job], diag

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"JOB_LOG_DIR": tmp, "JOB_OUT_DIR": tmp}), \
             mock.patch.object(run_daily.collect, "chrome_session", return_value=Lock()), \
             mock.patch.object(run_daily.collect, "attach", side_effect=attach), \
             mock.patch.object(run_daily.collect, "close_browser",
                               side_effect=lambda p: events.append("close")), \
             mock.patch.object(run_daily.collect, "active_page", return_value=None), \
             mock.patch.object(run_daily, "gather", side_effect=gather), \
             mock.patch.object(run_daily.match_engine, "rank", side_effect=lambda jobs: jobs), \
             mock.patch.object(run_daily.store, "filter_new", side_effect=lambda jobs: jobs), \
             mock.patch.object(run_daily.mailer, "send",
                               side_effect=lambda *a, **k: events.append("mail") or "SENT:dry"):
            run_daily.run(self._args())
        self.assertEqual(
            [item for item in events if item in ("acquire", "attach", "gather", "mail", "close", "release")],
            ["acquire", "attach", "gather", "mail", "close", "release"],
        )

    def test_gather_error_still_closes_before_release(self):
        events: list[str] = []

        class Lock:
            def acquire(self):
                events.append("acquire")

            def release(self):
                events.append("release")

        def gather(page, slot):
            events.append("gather")
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"JOB_LOG_DIR": tmp}), \
             mock.patch.object(run_daily.collect, "chrome_session", return_value=Lock()), \
             mock.patch.object(run_daily.collect, "attach",
                               side_effect=lambda: events.append("attach") or object()), \
             mock.patch.object(run_daily.collect, "close_browser",
                               side_effect=lambda p: events.append("close")), \
             mock.patch.object(run_daily, "gather", side_effect=gather):
            with self.assertRaises(RuntimeError):
                run_daily.run(self._args())
        self.assertEqual(events, ["acquire", "attach", "gather", "close", "release"])

    def test_close_error_still_releases_the_lock(self):
        events: list[str] = []

        class Lock:
            def acquire(self):
                events.append("acquire")

            def release(self):
                events.append("release")

        diag = {
            "boss": 0, "keywords": [], "blocked": [], "errors": [],
            "login": None, "verify": False, "circuit": False,
        }

        def close(page):
            events.append("close")
            raise RuntimeError("quit failed")

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"JOB_LOG_DIR": tmp}), \
             mock.patch.object(run_daily.collect, "chrome_session", return_value=Lock()), \
             mock.patch.object(run_daily.collect, "attach", return_value=object()), \
             mock.patch.object(run_daily.collect, "close_browser", side_effect=close), \
             mock.patch.object(run_daily.collect, "active_page", return_value=None), \
             mock.patch.object(run_daily, "gather", return_value=([], diag)), \
             mock.patch.object(run_daily.mailer, "send_alert", return_value="SENT:alert"):
            with self.assertRaises(RuntimeError):
                run_daily.run(self._args())
        self.assertEqual(events, ["acquire", "close", "release"])

    def test_lock_timeout_does_not_attach_or_close(self):
        events: list[str] = []

        class Lock:
            def acquire(self):
                events.append("acquire")
                raise run_daily.collect.ChromeLockTimeout(9333, 5, 5)

            def release(self):
                events.append("release")

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"JOB_LOG_DIR": tmp}), \
             mock.patch.object(run_daily.collect, "chrome_session", return_value=Lock()), \
             mock.patch.object(run_daily.collect, "attach",
                               side_effect=lambda: events.append("attach")), \
             mock.patch.object(run_daily.collect, "close_browser",
                               side_effect=lambda p: events.append("close")), \
             mock.patch.object(run_daily.mailer, "send_alert", return_value="SENT:alert") as alert:
            with self.assertRaises(run_daily.collect.ChromeLockTimeout):
                run_daily.run(self._args())
        self.assertEqual(events, ["acquire"])
        alert.assert_called_once()
        self.assertIn("跨进程锁", alert.call_args[0][0])

    def test_keep_open_releases_the_lock_without_closing(self):
        events: list[str] = []

        class Lock:
            def acquire(self):
                events.append("acquire")

            def release(self):
                events.append("release")

        diag = {
            "boss": 0, "keywords": [], "blocked": [], "errors": [],
            "login": None, "verify": False, "circuit": False,
        }
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"JOB_LOG_DIR": tmp}), \
             mock.patch.object(run_daily.collect, "chrome_session", return_value=Lock()), \
             mock.patch.object(run_daily.collect, "attach", return_value=object()), \
             mock.patch.object(run_daily.collect, "close_browser",
                               side_effect=lambda p: events.append("close")), \
             mock.patch.object(run_daily.collect, "active_page", return_value=None), \
             mock.patch.object(run_daily, "gather", return_value=([], diag)), \
             mock.patch.object(run_daily.mailer, "send_alert", return_value="SENT:alert"):
            run_daily.run(self._args(keep_open=True))
        self.assertIn("release", events)
        self.assertNotIn("close", events)

    def test_demo_does_not_take_the_chrome_lock(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"JOB_LOG_DIR": tmp, "JOB_OUT_DIR": tmp}), \
             mock.patch.object(run_daily.collect, "chrome_session") as session, \
             mock.patch.object(run_daily.mailer, "send", return_value="SENT:dry"):
            run_daily.run(self._args(demo=True))
        session.assert_not_called()

    def test_notes_mention_restart_and_incomplete_run(self):
        notes = run_daily._notes(
            {"chrome_restarts": 1, "chrome_lost": True, "blocked": [],
             "login": None, "verify": False, "circuit": False},
            [{"title": "x"}],
        )
        self.assertTrue(any("重启" in note for note in notes))
        self.assertTrue(any("没有跑完" in note for note in notes))


class TestLoginLock(unittest.TestCase):
    def test_check_holds_the_lock_around_attach(self):
        events: list[str] = []

        class Lock:
            def __enter__(self):
                events.append("acquire")
                return self

            def __exit__(self, exc_type, exc, tb):
                events.append("release")

        with mock.patch.object(sys, "argv", ["login.py", "--check"]), \
             mock.patch.object(login.collect, "chrome_session", return_value=Lock()), \
             mock.patch.object(login.collect, "ensure_browser",
                               side_effect=lambda **k: events.append("ensure")), \
             mock.patch.object(login.collect, "attach",
                               side_effect=lambda: events.append("attach") or object()), \
             mock.patch.object(login, "check", side_effect=lambda page: events.append("check") or 0):
            code = login.main()
        self.assertEqual(code, 0)
        self.assertEqual(events, ["acquire", "ensure", "attach", "check", "release"])


class TestRunState(unittest.TestCase):
    def test_marker_round_trip_and_liveness(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = run_state.mark_run_started("12:30", "default", phase="starting", directory=directory)
            data = run_state.read_run_marker(path)
            self.assertEqual(data["slot"], "12:30")
            self.assertEqual(data["pid"], os.getpid())
            self.assertEqual(data["phase"], "starting")
            run_state.update_run(phase="waiting-chrome", directory=directory)
            data = run_state.read_run_marker(path)
            self.assertEqual(data["phase"], "waiting-chrome")
            self.assertEqual(data["slot"], "12:30")
            now = datetime.now()
            self.assertTrue(run_state.marker_is_live(data, now=now))
            self.assertFalse(run_state.marker_is_live(data, now=now, alive=lambda pid: False))
            yesterday = {
                "pid": os.getpid(),
                "started": (now - timedelta(days=1)).isoformat(timespec="seconds"),
            }
            self.assertFalse(run_state.marker_is_live(yesterday, now=now, alive=lambda pid: True))
            run_state.clear_run_marker(directory)
            self.assertFalse(path.exists())
            self.assertIsNone(run_state.read_run_marker(path))


class TestWatchdogCatchup(unittest.TestCase):
    def test_success_prefixes(self):
        started = datetime(2026, 11, 17, 19, 3, 42)
        ts, row, healthy = watchdog.success_in_window(
            [
                {"ts": "2026-11-17T12:40:00", "mail": "SKIP:no-new"},
                {"ts": "2026-11-17T18:00:00", "fatal": "x"},
            ],
            started=started,
        )
        self.assertTrue(healthy)
        self.assertEqual(row["mail"], "SKIP:no-new")
        _, _, yesterday = watchdog.success_in_window(
            [{"ts": "2026-11-16T12:40:00", "mail": "SENT:old"}],
            started=started,
        )
        self.assertFalse(yesterday)
        self.assertIsNotNone(ts)

    def test_already_ok_does_not_sleep(self):
        clock = _Clock(datetime(2026, 11, 17, 13, 30))
        slept = {"n": 0}

        def sleep(seconds):
            slept["n"] += 1
            clock.sleep(seconds)

        outcome = watchdog.poll_until_settled(
            lambda: [{"ts": "2026-11-17T12:40:00", "mail": "SENT:9"}],
            lambda: None,
            lambda pid: False,
            grace=60,
            wait=60,
            poll=5,
            monotonic=clock.monotonic,
            sleep=sleep,
            now_fn=clock.now,
        )
        self.assertEqual(outcome["reason"], "already-ok")
        self.assertFalse(outcome["should_alert"])
        self.assertEqual(slept["n"], 0)

    def test_catchup_waits_then_accepts_sent(self):
        """2026-11-17 19:03 的形态：昨天的成功还在，今天的 SENT 随后落下。"""
        clock = _Clock(datetime(2026, 11, 17, 19, 3, 42))
        entries = [{"ts": "2026-11-16T12:40:01", "mail": "SENT:yesterday"}]
        marker: dict = {}

        def sleep(seconds):
            clock.sleep(seconds)
            if clock.mono >= 5 and not marker:
                marker.update({
                    "pid": 4242,
                    "started": "2026-11-17T19:03:43",
                    "phase": "collecting",
                })
            if clock.mono >= 20:
                entries.append({
                    "ts": "2026-11-17T19:04:59",
                    "mail": "SENT:12",
                    "slot": "12:45",
                })
                marker.clear()

        outcome = watchdog.poll_until_settled(
            lambda: list(entries),
            lambda: dict(marker) if marker else None,
            lambda pid: bool(marker),
            grace=60,
            wait=45 * 60,
            poll=5,
            tail=15,
            monotonic=clock.monotonic,
            sleep=sleep,
            now_fn=clock.now,
        )
        self.assertFalse(outcome["should_alert"])
        self.assertEqual(outcome["reason"], "success-during-wait")
        self.assertGreaterEqual(outcome["waited_s"], 20)

    def test_success_landing_during_grace_skips_alert(self):
        clock = _Clock(datetime(2026, 11, 17, 19, 3, 42))
        entries = [{"ts": "2026-11-16T12:40:00", "mail": "SENT:old"}]

        def sleep(seconds):
            clock.sleep(seconds)
            if clock.mono >= 2 and len(entries) == 1:
                entries.append({"ts": clock.now().isoformat(timespec="seconds"), "mail": "SKIP:no-new"})

        outcome = watchdog.poll_until_settled(
            lambda: list(entries),
            lambda: None,
            lambda pid: False,
            grace=10,
            wait=30,
            poll=1,
            monotonic=clock.monotonic,
            sleep=sleep,
            now_fn=clock.now,
        )
        self.assertFalse(outcome["should_alert"])
        self.assertEqual(outcome["reason"], "success-during-grace")

    def test_no_run_in_grace_still_alerts(self):
        clock = _Clock(datetime(2026, 11, 17, 19, 3, 42))
        outcome = watchdog.poll_until_settled(
            lambda: [{"ts": "2026-11-16T12:40:00", "mail": "SENT:old"}],
            lambda: None,
            lambda pid: False,
            grace=3,
            wait=30,
            poll=1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            now_fn=clock.now,
        )
        self.assertTrue(outcome["should_alert"])
        self.assertEqual(outcome["reason"], "no-run-started")
        self.assertGreaterEqual(outcome["waited_s"], 3)

    def test_dead_run_without_success_alerts_after_tail(self):
        clock = _Clock(datetime(2026, 11, 17, 19, 3, 42))
        alive = {"on": True}
        marker = {"pid": 7, "started": "2026-11-17T19:03:42", "phase": "collecting"}

        def sleep(seconds):
            clock.sleep(seconds)
            if clock.mono >= 2:
                alive["on"] = False

        outcome = watchdog.poll_until_settled(
            lambda: [],
            lambda: dict(marker),
            lambda pid: alive["on"],
            grace=30,
            wait=60,
            poll=1,
            tail=1,
            monotonic=clock.monotonic,
            sleep=sleep,
            now_fn=clock.now,
        )
        self.assertTrue(outcome["should_alert"])
        self.assertEqual(outcome["reason"], "run-finished-without-success")

    def test_live_run_past_the_wait_budget_alerts(self):
        clock = _Clock(datetime(2026, 11, 17, 19, 3, 42))
        marker = {"pid": 7, "started": "2026-11-17T19:03:42", "phase": "waiting-chrome"}
        outcome = watchdog.poll_until_settled(
            lambda: [],
            lambda: dict(marker),
            lambda pid: True,
            grace=1,
            wait=3,
            poll=1,
            tail=5,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            now_fn=clock.now,
        )
        self.assertTrue(outcome["should_alert"])
        self.assertEqual(outcome["reason"], "wait-timeout")

    def test_main_skips_mail_when_today_already_succeeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            now = datetime.now().replace(microsecond=0)
            (log_dir / "daily.log").write_text(
                json.dumps({"ts": now.isoformat(timespec="seconds"), "mail": "SENT:3"},
                           ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(watchdog, "LOG_DIR", log_dir), \
                 mock.patch.object(sys, "argv", ["watchdog.py", "--force", "--catchup-grace", "0"]), \
                 mock.patch("mailer.send_alert") as send:
                code = watchdog.main()
        self.assertEqual(code, 0)
        send.assert_not_called()

    def test_main_alerts_when_nothing_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            with mock.patch.object(watchdog, "LOG_DIR", log_dir), \
                 mock.patch.object(sys, "argv", [
                     "watchdog.py", "--force", "--dry-run",
                     "--catchup-grace", "0", "--catchup-wait", "0",
                 ]), \
                 mock.patch("mailer.send_alert", return_value="DRY") as send:
                code = watchdog.main()
        self.assertEqual(code, 0)
        send.assert_called_once()
        self.assertIn("没有任何成功记录", send.call_args[0][0])

    def test_main_suppresses_alert_when_daily_is_in_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            daily = log_dir / "daily.log"
            daily.write_text(
                json.dumps({"ts": "2026-11-16T12:40:00", "mail": "SENT:yesterday"},
                           ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            run_state.mark_run_started("12:30", "default", phase="collecting", directory=log_dir)

            def finish():
                time.sleep(0.25)
                with daily.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "mail": "SKIP:no-new",
                        "slot": "12:30",
                    }, ensure_ascii=False) + "\n")
                run_state.clear_run_marker(log_dir)

            threading.Thread(target=finish, daemon=True).start()
            with mock.patch.object(watchdog, "LOG_DIR", log_dir), \
                 mock.patch.object(sys, "argv", [
                     "watchdog.py", "--force",
                     "--catchup-grace", "2",
                     "--catchup-wait", "3",
                     "--poll-interval", "0.1",
                     "--catchup-tail", "0.2",
                 ]), \
                 mock.patch("mailer.send_alert") as send:
                code = watchdog.main()
        self.assertEqual(code, 0)
        send.assert_not_called()


class TestDefaultProfilePolicy(unittest.TestCase):
    """默认画像收紧到「金融 + AI/数字化」。不改法务画像的配置。"""

    def setUp(self):
        self._orig = match_engine.CFG
        match_engine.CFG = settings.load_config()

    def tearDown(self):
        match_engine.CFG = self._orig

    def test_keywords_drop_bare_terms_and_keep_finance_ai(self):
        cfg = settings.load_config()
        words = cfg["keywords"]["core"] + cfg["keywords"]["extra"]
        bare = {"售前", "数字化转型", "行业解决方案"}
        self.assertFalse(bare & set(words))
        for required in (
            "金融解决方案",
            "金融行业解决方案",
            "金融解决方案架构师",
            "金融业务架构师",
            "金融科技产品",
            "智能投顾",
            "银行数字化",
            "金融大模型",
            "金融AI解决方案",
            "财富科技",
            "财富管理数字化",
            "私行数字化",
            "理财经理数字化",
            "金融前沿部署",
            "金融解决方案总监",
        ):
            self.assertIn(required, words)
        # 财富科技经常 0 命中，近义说法要同时在
        self.assertGreaterEqual(sum(1 for w in words if "财富" in w or "私行" in w), 3)
        self.assertEqual(cfg["search"]["min_salary_k"], 50)
        self.assertEqual(cfg["scoring"]["domain_floor"], 10)
        self.assertEqual(len(cfg["keywords"]["core"]), 12)

    def test_false_positives_stay_below_the_finance_solution(self):
        generic_pm = {
            "title": "高级产品经理", "company": "品阔", "salary_text": "50-80K",
            "city": "上海", "scale_text": "500-999人",
            "tags": ["AI", "SaaS", "大模型", "云计算"], "desc": "企业数字化产品",
        }
        infra = {
            "title": "智算基础设施解决方案架构师", "company": "九章云极",
            "salary_text": "50-80K", "city": "北京", "scale_text": "1000-9999人",
            "tags": ["AI", "云计算", "大模型"], "desc": "智算中心建设",
            "industry": "计算机软件",
        }
        keep = {
            "title": "金融解决方案", "company": "某大型互联网公司",
            "salary_text": "50-80K", "city": "杭州", "scale_text": "10000人以上",
            "tags": ["AI"], "desc": "",
        }
        vendor = {
            "title": "金融大模型解决方案架构师", "company": "华为云",
            "salary_text": "60-90K·16薪", "city": "深圳", "scale_text": "10000人以上",
            "tags": ["银行", "大模型", "财富管理"], "desc": "金融行业大模型",
        }
        wealth = {
            "title": "智能投顾产品专家", "company": "招银云创",
            "salary_text": "50-80K", "city": "深圳", "scale_text": "1000-9999人",
            "tags": ["财富管理", "私人银行", "AI"], "desc": "理财经理数字化展业",
        }
        scored = {name: match_engine.score_job(dict(job)) for name, job in {
            "pm": generic_pm, "infra": infra, "keep": keep, "vendor": vendor, "wealth": wealth,
        }.items()}
        self.assertLess(scored["pm"]["score"], 50, scored["pm"])
        self.assertLess(scored["infra"]["score"], 50, scored["infra"])
        self.assertGreaterEqual(scored["keep"]["score"], 60, scored["keep"])
        self.assertGreater(scored["keep"]["score"], scored["pm"]["score"])
        self.assertGreater(scored["keep"]["score"], scored["infra"]["score"])
        self.assertGreaterEqual(scored["vendor"]["score"], 80, scored["vendor"])
        self.assertGreaterEqual(scored["wealth"]["score"], 55, scored["wealth"])
        pushed = {j["title"] for j in match_engine.rank([
            dict(generic_pm), dict(infra), dict(keep), dict(vendor), dict(wealth),
        ])}
        self.assertNotIn("高级产品经理", pushed)
        self.assertNotIn("智算基础设施解决方案架构师", pushed)
        self.assertIn("金融解决方案", pushed)
        self.assertIn("金融大模型解决方案架构师", pushed)
        self.assertIn("智能投顾产品专家", pushed)

    def test_ic_titles_are_blocked_and_solution_architect_is_not(self):
        for title in ("Java开发工程师", "java开发", "后端开发工程师", "算法工程师", "机器学习工程师"):
            job = {"title": title, "company": "阿里云", "salary_text": "50-80K", "city": "杭州"}
            self.assertIsNotNone(match_engine.hard_exclude(job), title)
        for title in ("金融解决方案架构师", "业务架构师", "金融售前解决方案专家"):
            job = {"title": title, "company": "华为云", "salary_text": "60-90K", "city": "深圳"}
            self.assertIsNone(match_engine.hard_exclude(job), title)


if __name__ == "__main__":
    unittest.main()
