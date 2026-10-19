#!/usr/bin/env python3
"""Юнит-тесты java-reaper.

Запуск:  python3 -m unittest discover -s tests -v
       (или: ./tests/test_reaper.py)

Тесты не трогают реальные процессы: execute() вызывается только в dry-run
или на текущем процессе (self) как на безопасной мишени.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "java-reaper")
FIXTURES = os.path.join(HERE, "fixtures")


def _load_module():
    loader = importlib.machinery.SourceFileLoader("java_reaper", SCRIPT)
    spec = importlib.util.spec_from_file_location("java_reaper", SCRIPT, loader=loader)
    module = importlib.util.module_from_spec(spec)
    # dataclass на Python 3.14 ищет модуль в sys.modules — регистрируем заранее.
    sys.modules["java_reaper"] = module
    loader.exec_module(module)
    return module


reaper = _load_module()

NOW = 1_800_000_000.0
HOUR = 3600.0


def make_daemon(pid=1000, kind="gradle", **kw) -> "reaper.Daemon":
    d = reaper.Daemon(
        pid=pid,
        kind=kind,
        starttime=kw.pop("starttime", 12345),
        cpu_ticks=kw.pop("cpu_ticks", 1000),
        rss_mb=kw.pop("rss_mb", 512),
        swap_mb=kw.pop("swap_mb", 0),
    )
    for key, value in kw.items():
        setattr(d, key, value)
    return d


def state_for(daemon, first_seen, cpu_ticks=None, last_activity=0.0):
    rec = {"first_seen": first_seen, "last_activity": last_activity}
    if cpu_ticks is not None:
        rec["cpu_ticks"] = cpu_ticks
    return {"daemons": {str(daemon.pid): rec}}


# --------------------------------------------------------------------------- #
# Разбор логов и таймстемпов
# --------------------------------------------------------------------------- #

class TestParseTs(unittest.TestCase):
    def test_parses_comma_fraction(self):
        ts = reaper.parse_ts("2026-10-04T12:00:00,101+0300 rest")
        self.assertIsNotNone(ts)
        self.assertEqual(ts, reaper.parse_ts("2026-10-04T12:00:00.101+0300 rest"))

    def test_offset_does_not_shift_comparison(self):
        a = reaper.parse_ts("2026-10-04T12:00:00.000+0300")
        b = reaper.parse_ts("2026-10-04T12:00:00.000+0000")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        # 12:00+0300 = 09:00 UTC, то есть на 3 часа раньше 12:00 UTC.
        self.assertAlmostEqual(a - b, -3 * HOUR, places=3)

    def test_rejects_garbage(self):
        self.assertIsNone(reaper.parse_ts("not a timestamp"))
        self.assertIsNone(reaper.parse_ts("2026-10-04 12:00:00"))


class TestParseGradleLog(unittest.TestCase):
    def test_build_done(self):
        start, end = reaper.parse_gradle_log(
            os.path.join(FIXTURES, "gradle-build-done.log"), 1 << 20)
        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        self.assertGreater(end, start)

    def test_build_running_has_start_without_end(self):
        start, end = reaper.parse_gradle_log(
            os.path.join(FIXTURES, "gradle-build-running.log"), 1 << 20)
        self.assertIsNotNone(start)
        self.assertIsNone(end)

    def test_log_without_build_markers(self):
        start, end = reaper.parse_gradle_log(
            os.path.join(FIXTURES, "gradle-no-build.log"), 1 << 20)
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_missing_file(self):
        self.assertEqual(
            reaper.parse_gradle_log(os.path.join(FIXTURES, "nope.log"), 1 << 20),
            (None, None))


# --------------------------------------------------------------------------- #
# Классификация процесса
# --------------------------------------------------------------------------- #

class TestScanGradleLog(unittest.TestCase):
    """Инкрементальный разбор: лог демона засоряется спамом, читать целиком
    каждый свип нельзя."""

    def _write(self, path, text):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)

    def test_first_scan_reads_whole_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("2026-10-04T12:00:01.500+0300 [INFO] Received command: Build{id=1}\n")
            fh.write("2026-10-04T12:03:44.900+0300 [INFO] Marking the daemon as idle, address: [x]\n")
            path = fh.name
        try:
            res = reaper.scan_gradle_log(path, 0, 1 << 20)
            self.assertIsNotNone(res["start"])
            self.assertIsNotNone(res["end"])
            self.assertTrue(res["complete"])
            self.assertEqual(res["offset"], os.path.getsize(path))
        finally:
            os.unlink(path)

    def test_incremental_scan_sees_only_new_bytes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("2026-10-04T12:00:01.500+0300 [INFO] Received command: Build{id=1}\n")
            path = fh.name
        try:
            first = reaper.scan_gradle_log(path, 0, 1 << 20)
            self._write(path, "2026-10-04T12:09:00.000+0300 [INFO] Marking the daemon as idle\n")
            second = reaper.scan_gradle_log(path, first["offset"], 1 << 20)
            self.assertIsNone(second["start"])      # старый маркер не перечитывается
            self.assertIsNotNone(second["end"])      # новый — виден
            self.assertEqual(second["offset"], os.path.getsize(path))
        finally:
            os.unlink(path)

    def test_oversized_log_without_markers_is_unknown(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("2026-10-04T12:00:01.500+0300 [INFO] Received command: Build{id=1}\n")
            fh.write("x" * 5000)
            path = fh.name
        try:
            res = reaper.scan_gradle_log(path, 0, 1000)
            self.assertFalse(res["complete"])
        finally:
            os.unlink(path)


class TestDaemonKind(unittest.TestCase):
    def test_gradle(self):
        self.assertEqual(
            reaper.daemon_kind("java -cp x org.gradle.launcher.daemon.bootstrap.GradleDaemon"), "gradle")

    def test_kotlin(self):
        self.assertEqual(
            reaper.daemon_kind("java -cp x org.jetbrains.kotlin.daemon.KotlinCompileDaemon"), "kotlin")

    def test_other_java(self):
        self.assertIsNone(reaper.daemon_kind("java -jar something.jar"))


class TestIgnoredChildren(unittest.TestCase):
    def test_known_noise_is_ignored(self):
        for cmd in ("/usr/bin/adb fork-server", "node /x/y.js", "python3 a.py"):
            self.assertTrue(reaper.is_ignored_child(cmd), cmd)

    def test_unknown_child_protects(self):
        self.assertFalse(reaper.is_ignored_child("/usr/bin/java -jar worker.jar"))
        self.assertFalse(reaper.is_ignored_child(""))

    def test_classpath_noise_is_not_ignored(self):
        """Реальный fork с 'playwright' в classpath — не шум.

        Поиск подстроки по всему cmdline помечал работающее приложение
        как игнорируемый шум, и демон оставался без защиты.
        """
        cmd = ("/usr/bin/java -XX:TieredStopAtLevel=1 "
               "-cp /cache/io.playwright/playwright/1.49.0/playwright-1.49.0.jar "
               "ru.wbparser.app.WbParserMainKt")
        self.assertFalse(reaper.is_ignored_child(cmd))

    def test_noise_named_only_in_path_is_not_ignored(self):
        self.assertFalse(reaper.is_ignored_child("/opt/chromium-tools/run.sh"))


class TestBuildInProgress(unittest.TestCase):
    def test_start_without_end(self):
        d = make_daemon(last_build_start=NOW - 100, last_build_end=None)
        self.assertTrue(d.build_in_progress)

    def test_end_after_start(self):
        d = make_daemon(last_build_start=NOW - 100, last_build_end=NOW - 50)
        self.assertFalse(d.build_in_progress)

    def test_start_after_end(self):
        d = make_daemon(last_build_start=NOW - 10, last_build_end=NOW - 100)
        self.assertTrue(d.build_in_progress)

    def test_never_built(self):
        d = make_daemon(last_build_start=None, last_build_end=None)
        self.assertFalse(d.build_in_progress)


# --------------------------------------------------------------------------- #
# Классификатор решений
# --------------------------------------------------------------------------- #

class TestClassify(unittest.TestCase):
    cfg = dict(reaper.DEFAULTS)

    def decide(self, daemon, state, now=NOW):
        decisions = reaper.classify([daemon], state, now, self.cfg)
        self.assertEqual(len(decisions), 1)
        return decisions[0]

    def test_missing_log_protects(self):
        d = make_daemon(log_ok=False)
        st = state_for(d, NOW - 10 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertFalse(dec.kill)
        self.assertTrue(dec.protected)
        self.assertIn("лог", dec.reason)

    def test_no_baseline_protects(self):
        d = make_daemon(last_build_end=NOW - 10 * HOUR)
        st = state_for(d, NOW - 10 * HOUR, cpu_ticks=None)
        dec = self.decide(d, st)
        self.assertFalse(dec.kill)
        self.assertTrue(dec.protected)
        self.assertIn("baseline", dec.reason)

    def test_idle_below_limit_kept(self):
        d = make_daemon(last_build_end=NOW - 60 * 60)
        st = state_for(d, NOW - 60 * 60, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertFalse(dec.kill)
        self.assertIn("простой", dec.reason)

    def test_idle_above_limit_killed(self):
        d = make_daemon(last_build_end=NOW - 5 * HOUR)
        st = state_for(d, NOW - 5 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertTrue(dec.kill)
        self.assertIn("простой", dec.reason)

    def test_small_footprint_kept(self):
        d = make_daemon(last_build_end=NOW - 5 * HOUR, rss_mb=10, swap_mb=0)
        st = state_for(d, NOW - 5 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertFalse(dec.kill)
        self.assertIn("footprint", dec.reason)

    def test_busy_cpu_kept(self):
        d = make_daemon(last_build_end=NOW - 5 * HOUR, cpu_ticks=1000 + 100 * os.sysconf("SC_CLK_TCK"))
        st = state_for(d, NOW - 5 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertFalse(dec.kill)
        self.assertTrue(dec.busy)
        self.assertIn("CPU", dec.reason)

    def test_live_child_protects(self):
        d = make_daemon(last_build_end=NOW - 5 * HOUR, children=[4242])
        st = state_for(d, NOW - 5 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertFalse(dec.kill)
        self.assertTrue(dec.busy)
        self.assertIn("дети", dec.reason)

    def test_ignored_child_does_not_protect(self):
        d = make_daemon(last_build_end=NOW - 5 * HOUR, children=[], ignored_children=[4242])
        st = state_for(d, NOW - 5 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertTrue(dec.kill)

    def test_unknown_peer_protects(self):
        d = make_daemon(last_build_end=NOW - 5 * HOUR, peer_unknown=1)
        st = state_for(d, NOW - 5 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertFalse(dec.kill)
        self.assertTrue(dec.protected)
        self.assertIn("неопределённым владельцем", dec.reason)

    def test_recent_log_activity_protects(self):
        d = make_daemon(kind="kotlin", log_mtime=NOW - 10, log_ok=True)
        st = state_for(d, NOW - 10 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertFalse(dec.kill)
        self.assertTrue(dec.busy)
        self.assertIn("лог", dec.reason)

    def test_kotlin_idle_by_log_mtime_killed(self):
        d = make_daemon(kind="kotlin", log_mtime=NOW - 5 * HOUR, log_ok=True)
        st = state_for(d, NOW - 5 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertTrue(dec.kill)

    def test_build_in_progress_counts_from_build_start(self):
        """Сборка идёт 40 минут, последняя завершённая сборка была 5 часов назад.

        Простой должен считаться от старта текущей сборки, а не от прошлого
        конца сборки — иначе активный демон выглядел бы простаивающим.
        """
        d = make_daemon(last_build_start=NOW - 40 * 60, last_build_end=NOW - 5 * HOUR)
        st = state_for(d, NOW - 5 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertFalse(dec.kill)
        self.assertTrue(dec.busy)
        self.assertIn("сборка идёт", dec.reason)

    def test_stuck_build_without_cpu_children_is_candidate(self):
        """Эскалация: старт без конца давно, CPU 0, детей нет ⇒ кандидат."""
        d = make_daemon(last_build_start=NOW - 5 * HOUR, last_build_end=None)
        st = state_for(d, NOW - 5 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertTrue(dec.kill)
        self.assertIn("зависшая сборка", dec.reason)

    def test_idle_measured_from_log_not_from_first_seen(self):
        """Простой измеряется по логу, а не по моменту первого наблюдения.

        Иначе демон, простаивающий 10 суток, нельзя было бы тронуть в первые
        сутки наблюдения — first_seen всегда свежий.
        """
        d = make_daemon(last_build_end=NOW - 10 * HOUR)
        st = state_for(d, NOW - 60, cpu_ticks=1000)  # увидели только что
        dec = self.decide(d, st)
        self.assertTrue(dec.kill)
        self.assertGreater(dec.idle_seconds, 9 * HOUR)

    def test_pid_reuse_resets_baseline(self):
        """PID переиспользован: прежний baseline не про этот процесс."""
        d = make_daemon(last_build_end=NOW - 10 * HOUR, starttime=999)
        st = {"daemons": {"1000": {"first_seen": NOW - 10 * HOUR, "cpu_ticks": 1000,
                                   "starttime": 111}}}
        dec = self.decide(d, st)
        self.assertFalse(dec.kill)
        self.assertTrue(dec.protected)
        self.assertIn("baseline", dec.reason)


class TestClassifyPeerGraph(unittest.TestCase):
    """Least fixed point по наблюдателям: peer защищает, только если он сам занят."""

    cfg = dict(reaper.DEFAULTS)

    def test_busy_peer_protects_idle_daemon(self):
        client = make_daemon(pid=2000, last_build_end=NOW - 5 * HOUR, cpu_ticks=1000)
        daemon = make_daemon(pid=1000, last_build_end=NOW - 5 * HOUR, peer_pids=[2000])
        state = {
            "daemons": {
                "1000": {"first_seen": NOW - 5 * HOUR, "cpu_ticks": 1000},
                "2000": {"first_seen": NOW - 5 * HOUR, "cpu_ticks": 1000},
            }
        }
        # Клиент «занят»: у него есть живой ребёнок.
        client.children = [7777]
        decisions = {d.daemon.pid: d for d in reaper.classify([daemon, client], state, NOW, self.cfg)}
        self.assertFalse(decisions[1000].kill)
        self.assertIn("peer", decisions[1000].reason)

    def test_idle_peer_does_not_protect(self):
        """Peer-кандидат (тоже простаивает) не защищает — иначе ничего не убить."""
        peer = make_daemon(pid=2000, last_build_end=NOW - 5 * HOUR, cpu_ticks=1000)
        daemon = make_daemon(pid=1000, last_build_end=NOW - 5 * HOUR, peer_pids=[2000])
        state = {
            "daemons": {
                "1000": {"first_seen": NOW - 5 * HOUR, "cpu_ticks": 1000},
                "2000": {"first_seen": NOW - 5 * HOUR, "cpu_ticks": 1000},
            }
        }
        decisions = {d.daemon.pid: d for d in reaper.classify([daemon, peer], state, NOW, self.cfg)}
        self.assertTrue(decisions[1000].kill)
        self.assertTrue(decisions[2000].kill)

    def test_peer_outside_daemon_list_protects(self):
        """Неизвестный peer (клиент сборки/IDE) — защита."""
        daemon = make_daemon(pid=1000, last_build_end=NOW - 5 * HOUR, peer_pids=[999999])
        state = {"daemons": {"1000": {"first_seen": NOW - 5 * HOUR, "cpu_ticks": 1000}}}
        decisions = reaper.classify([daemon], state, NOW, self.cfg)
        self.assertFalse(decisions[0].kill)
        self.assertIn("клиент сборки", decisions[0].reason)


# --------------------------------------------------------------------------- #
# Метрика dry-run
# --------------------------------------------------------------------------- #

class TestMetric(unittest.TestCase):
    cfg = dict(reaper.DEFAULTS)

    def test_reuse_detected_after_event(self):
        d = make_daemon(last_build_end=NOW + 10)  # активность после события
        state = {"would_kill": [{"pid": 1000, "at": NOW - 100, "reused": False, "final": False}]}
        metric = reaper.update_metric(state, [reaper.Decision(d, True, "x")], NOW, self.cfg)
        self.assertEqual(state["would_kill"][0]["reused"], True)

    def test_no_reuse_when_quiet(self):
        d = make_daemon(last_build_end=NOW - 1000)
        state = {"would_kill": [{"pid": 1000, "at": NOW - 100, "reused": False, "final": False}]}
        reaper.update_metric(state, [reaper.Decision(d, True, "x")], NOW, self.cfg)
        self.assertEqual(state["would_kill"][0]["reused"], False)

    def test_event_finalized_after_window(self):
        state = {"would_kill": [{"pid": 1000, "at": NOW - 10 * HOUR, "reused": False, "final": False}]}
        metric = reaper.update_metric(state, [], NOW, self.cfg)
        self.assertEqual(metric["events"], 1)
        self.assertEqual(metric["pending"], 0)

    def test_new_event_registered_once(self):
        d = make_daemon(last_build_end=NOW - 5 * HOUR)
        state = {}
        for _ in range(3):
            reaper.update_metric(state, [reaper.Decision(d, True, "x")], NOW, self.cfg)
        self.assertEqual(len(state["would_kill"]), 1)

    def test_metric_rate(self):
        state = {
            "would_kill": [
                {"pid": 1, "at": NOW - 10 * HOUR, "reused": True, "final": True},
                {"pid": 2, "at": NOW - 10 * HOUR, "reused": False, "final": True},
            ]
        }
        metric = reaper.update_metric(state, [], NOW, self.cfg)
        self.assertEqual(metric["events"], 2)
        self.assertEqual(metric["reused"], 1)
        self.assertAlmostEqual(metric["rate"], 0.5)


# --------------------------------------------------------------------------- #
# Конфиг и состояние
# --------------------------------------------------------------------------- #

class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = reaper.load_config("/nonexistent/config")
        self.assertEqual(cfg["IDLE_SECONDS"], 10800)
        self.assertEqual(cfg["MIN_FOOTPRINT_MB"], 128)

    def test_override_and_comments(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as fh:
            fh.write("# комментарий\nIDLE_SECONDS = 600  # хвостовой комментарий\n")
            fh.write("UNKNOWN_KEY = 5\n\nMINDONTCRASH\n")
            path = fh.name
        try:
            cfg = reaper.load_config(path)
            self.assertEqual(cfg["IDLE_SECONDS"], 600)
            self.assertNotIn("UNKNOWN_KEY", cfg)
        finally:
            os.unlink(path)

    def test_bad_value_keeps_default(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as fh:
            fh.write("IDLE_SECONDS = не число\n")
            path = fh.name
        try:
            cfg = reaper.load_config(path)
            self.assertEqual(cfg["IDLE_SECONDS"], 10800)
        finally:
            os.unlink(path)


class TestState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(reaper.DEFAULTS)
            cfg["STATE_DIR"] = tmp
            reaper.save_state(cfg, {"daemons": {"1": {"cpu_ticks": 5}}, "version": 1})
            loaded = reaper.load_state(cfg)
            self.assertEqual(loaded["daemons"]["1"]["cpu_ticks"], 5)

    def test_missing_state_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(reaper.DEFAULTS)
            cfg["STATE_DIR"] = tmp
            state = reaper.load_state(cfg)
            self.assertEqual(state["daemons"], {})
            self.assertEqual(state["metric"]["events"], 0)

    def test_corrupt_state_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(reaper.DEFAULTS)
            cfg["STATE_DIR"] = tmp
            os.makedirs(tmp, exist_ok=True)
            with open(reaper.state_path(cfg), "w", encoding="utf-8") as fh:
                fh.write("{не json")
            state = reaper.load_state(cfg)
            self.assertEqual(state["daemons"], {})

    def test_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(reaper.DEFAULTS)
            cfg["STATE_DIR"] = tmp
            reaper.save_state(cfg, {"version": 1, "daemons": {}})
            leftovers = [n for n in os.listdir(tmp) if ".tmp." in n]
            self.assertEqual(leftovers, [])


# --------------------------------------------------------------------------- #
# Поиск лога через /proc
# --------------------------------------------------------------------------- #

class TestFindLogPath(unittest.TestCase):
    def test_finds_gradle_log_via_fd(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "daemon-4242.out.log")
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys,time; f=open(sys.argv[1],'w'); time.sleep(30)", log])
            try:
                deadline = __import__("time").time() + 5
                found = None
                while __import__("time").time() < deadline:
                    found = reaper.find_log_path(proc.pid)
                    if found:
                        break
                    __import__("time").sleep(0.1)
                self.assertEqual(found, log)
            finally:
                proc.kill()
                proc.wait(timeout=5)

    def test_ignores_non_log_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = os.path.join(tmp, "work.txt")
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys,time; f=open(sys.argv[1],'w'); time.sleep(30)", other])
            try:
                __import__("time").sleep(0.5)
                self.assertIsNone(reaper.find_log_path(proc.pid))
            finally:
                proc.kill()
                proc.wait(timeout=5)

    def test_dead_pid_has_no_log(self):
        self.assertIsNone(reaper.find_log_path(999999))


# --------------------------------------------------------------------------- #
# Исполнение (безопасно: dry-run и self)
# --------------------------------------------------------------------------- #

class TestExecuteDryRun(unittest.TestCase):
    cfg = dict(reaper.DEFAULTS)

    def test_dry_run_sends_no_signals(self):
        pid = os.getpid()
        st = reaper.read_proc_stat(pid)
        d = make_daemon(pid=pid, last_build_end=NOW - 5 * HOUR, starttime=st["starttime"])
        report = reaper.execute([reaper.Decision(d, True, "test")], {}, NOW, self.cfg, dry_run=True)
        self.assertEqual([r["action"] for r in report], ["dry-run"])

    def test_kotlin_skipped_while_gradle_builds(self):
        gradle_pid = os.getpid()
        kotlin_pid = os.getppid()
        st_g = reaper.read_proc_stat(gradle_pid)
        st_k = reaper.read_proc_stat(kotlin_pid)
        g = make_daemon(pid=gradle_pid, kind="gradle", starttime=st_g["starttime"],
                        last_build_start=NOW - 60, last_build_end=None)
        k = make_daemon(pid=kotlin_pid, kind="kotlin", starttime=st_k["starttime"],
                        log_mtime=NOW - 5 * HOUR)
        decisions = [reaper.Decision(g, False, "busy"), reaper.Decision(k, True, "idle")]
        report = reaper.execute(decisions, {}, NOW, self.cfg, dry_run=True)
        skipped = [r for r in report if r["action"] == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["pid"], kotlin_pid)

    def test_no_candidates_no_report(self):
        report = reaper.execute([], {}, NOW, self.cfg, dry_run=True)
        self.assertEqual(report, [])


# --------------------------------------------------------------------------- #
# Отчёт и smoke-тесты CLI
# --------------------------------------------------------------------------- #

class TestStatus(unittest.TestCase):
    def test_status_mentions_totals(self):
        d = make_daemon(last_build_end=NOW - 5 * HOUR)
        text = reaper.format_status([reaper.Decision(d, True, "простой")], {"metric": {}})
        self.assertIn("УБИТЬ", text)
        self.assertIn("footprint", text)

    def test_status_prints_metric(self):
        d = make_daemon(last_build_end=NOW - 5 * HOUR)
        state = {"metric": {"events": 3, "reused": 1, "rate": 0.33, "pending": 2}}
        text = reaper.format_status([reaper.Decision(d, True, "x")], state)
        self.assertIn("метрика", text)
        self.assertIn("33 %", text)


class TestCli(unittest.TestCase):
    def _run(self, *args, cfg=None):
        env = dict(os.environ)
        return subprocess.run(
            [sys.executable, SCRIPT, *args],
            capture_output=True, text=True, timeout=120, env=env)

    def test_status_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(f"STATE_DIR = {tmp}/state\nLOCK_PATH = {tmp}/reaper.lock\n")
            proc = self._run("--status", "--config", cfg)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("PID", proc.stdout)

    def test_status_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(f"STATE_DIR = {tmp}/state\nLOCK_PATH = {tmp}/reaper.lock\n")
            self._run("--status", "--config", cfg)
            self.assertFalse(os.path.exists(os.path.join(tmp, "state", "state.json")))

    def test_dry_run_writes_state_without_killing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(f"STATE_DIR = {tmp}/state\nLOCK_PATH = {tmp}/reaper.lock\n")
            proc = self._run("--dry-run", "--config", cfg)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(tmp, "state", "state.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("daemons", data)
            self.assertIn("version", data)
            self.assertNotIn("SIGTERM", proc.stdout)

    def test_script_compiles(self):
        proc = subprocess.run([sys.executable, "-m", "py_compile", SCRIPT],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
