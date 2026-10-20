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


sys.path.insert(0, HERE)

import _api as reaper  # noqa: E402

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


def make_peer(pid: int | None, name: str | None = None, age: float | None = None):
    """Established-соединение демона с локальным клиентом."""
    return reaper.PeerConn(inode="999", local_port=1234,
                           remote_port=5678, owner_pid=pid, owner_name=name,
                           age=age)

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


class TestLoopbackDetection(unittest.TestCase):
    """Внешние соединения демона (HTTPS к репозиторию) не считаются наблюдателем."""

    def test_loopback_v4(self):
        self.assertTrue(reaper._is_loopback("0100007F"))
        self.assertTrue(reaper._is_loopback("0100007f"))

    def test_external_v4(self):
        self.assertFalse(reaper._is_loopback("6B01A8C0"))  # 192.0.2.107
        self.assertFalse(reaper._is_loopback("EBE92422"))  # 34.36.233.235

    def test_loopback_v6(self):
        self.assertTrue(reaper._is_loopback("00000000000000000000000001000000"))
        self.assertFalse(reaper._is_loopback("00000000000000000000000000000001"))

    def test_garbage(self):
        self.assertFalse(reaper._is_loopback("zz"))


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
    # Память: «всегда убирать» — иначе решение зависело бы от
    # того, сколько памяти свободно на машине, где идут тесты.
    cfg["MEMORY_PRESSURE_MB"] = 0

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
        d = make_daemon(last_build_end=NOW - 10 * HOUR)
        st = state_for(d, NOW - 10 * HOUR, cpu_ticks=1000)
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
        d = make_daemon(last_build_end=NOW - 10 * HOUR, children=[], ignored_children=[4242])
        st = state_for(d, NOW - 10 * HOUR, cpu_ticks=1000)
        dec = self.decide(d, st)
        self.assertTrue(dec.kill)

    def test_unknown_peer_protects(self):
        d = make_daemon(last_build_end=NOW - 5 * HOUR, peer_conns=[make_peer(None)])
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
        d = make_daemon(last_build_start=NOW - 10 * HOUR, last_build_end=None)
        st = state_for(d, NOW - 10 * HOUR, cpu_ticks=1000)
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
    # Память: «всегда убирать» — иначе решение зависело бы от
    # того, сколько памяти свободно на машине, где идут тесты.
    cfg["MEMORY_PRESSURE_MB"] = 0

    def test_busy_peer_protects_idle_daemon(self):
        client = make_daemon(pid=2000, last_build_end=NOW - 5 * HOUR, cpu_ticks=1000)
        daemon = make_daemon(pid=1000, last_build_end=NOW - 5 * HOUR,
                           peer_conns=[make_peer(2000, "client")])
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
        peer = make_daemon(pid=2000, last_build_end=NOW - 10 * HOUR, cpu_ticks=1000)
        daemon = make_daemon(pid=1000, last_build_end=NOW - 10 * HOUR,
                           peer_conns=[make_peer(2000, "client")])
        state = {
            "daemons": {
                "1000": {"first_seen": NOW - 10 * HOUR, "cpu_ticks": 1000},
                "2000": {"first_seen": NOW - 10 * HOUR, "cpu_ticks": 1000},
            }
        }
        decisions = {d.daemon.pid: d for d in reaper.classify([daemon, peer], state, NOW, self.cfg)}
        self.assertTrue(decisions[1000].kill)
        self.assertTrue(decisions[2000].kill)

    def test_peer_outside_daemon_list_protects(self):
        """Неизвестный peer (клиент сборки/IDE) — защита."""
        daemon = make_daemon(pid=1000, last_build_end=NOW - 5 * HOUR,
                           peer_conns=[make_peer(999999, "ide")])
        state = {"daemons": {"1000": {"first_seen": NOW - 5 * HOUR, "cpu_ticks": 1000}}}
        decisions = reaper.classify([daemon], state, NOW, self.cfg)
        self.assertFalse(decisions[0].kill)
        self.assertIn("клиент сборки", decisions[0].reason)


# --------------------------------------------------------------------------- #
# Метрика dry-run
# --------------------------------------------------------------------------- #

class TestMetric(unittest.TestCase):
    cfg = dict(reaper.DEFAULTS)
    # Память: «всегда убирать» — иначе решение зависело бы от
    # того, сколько памяти свободно на машине, где идут тесты.
    cfg["MEMORY_PRESSURE_MB"] = 0

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

    def test_metric_split_by_kind(self):
        """Метрика разбита по видам: у разных видов разная цена ошибки."""
        gradle = make_daemon(pid=1, kind="gradle", last_build_end=NOW - 5 * HOUR)
        kotlin = make_daemon(pid=2, kind="kotlin", last_build_end=NOW - 5 * HOUR)
        state = {}
        decisions = [
            reaper.Decision(gradle, True, "x", recreate_cost_seconds=40),
            reaper.Decision(kotlin, True, "x", recreate_cost_seconds=5),
        ]
        reaper.update_metric(state, decisions, NOW, self.cfg)
        # Закрываем окно, чтобы события получили исход.
        later = NOW + 10 * HOUR
        reaper.update_metric(state, [], later, self.cfg)
        metric = state["metric"]
        self.assertIn("gradle", metric["by_kind"])
        self.assertIn("kotlin", metric["by_kind"])
        self.assertEqual(metric["by_kind"]["gradle"]["counted"], 1)
        self.assertEqual(metric["by_kind"]["kotlin"]["counted"], 1)

    def test_event_remembers_kind_and_cost(self):
        d = make_daemon(kind="gradle", last_build_end=NOW - 5 * HOUR)
        state = {}
        reaper.update_metric(state, [reaper.Decision(d, True, "x",
                                                     recreate_cost_seconds=40)],
                             NOW, self.cfg)
        event = state["would_kill"][0]
        self.assertEqual(event["kind"], "gradle")
        self.assertEqual(event["cost"], 40)

    def test_weighted_rate_counts_expensive_false_positive_more(self):
        """Взвешивание по цене: дорогое ложное срабатывание весит больше."""
        state = {
            "would_kill": [
                {"pid": 1, "at": NOW - 10 * HOUR, "reused": True, "final": True,
                 "outcome": "reused", "kind": "gradle", "cost": 40.0},
                {"pid": 2, "at": NOW - 10 * HOUR, "reused": False, "final": True,
                 "outcome": "not_reused", "kind": "gradle", "cost": 40.0},
                {"pid": 3, "at": NOW - 10 * HOUR, "reused": False, "final": True,
                 "outcome": "not_reused", "kind": "esbuild", "cost": 5.0},
            ]
        }
        metric = reaper.update_metric(state, [], NOW, self.cfg)
        gradle = metric["by_kind"]["gradle"]
        self.assertAlmostEqual(gradle["rate"], 0.5)
        self.assertAlmostEqual(gradle["rate_weighted"], 0.5)
        self.assertEqual(gradle["cost_reused_seconds"], 40.0)
        # Дешёвый вид: одно ложное из одного события.
        esbuild = metric["by_kind"]["esbuild"]
        self.assertEqual(esbuild["rate"], 0.0)
        self.assertEqual(esbuild["cost_reused_seconds"], 0.0)

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

    # ---- целостность метрики (исходы событий) ----

    def test_observation_gap_makes_event_unknown(self):
        """Разрыв наблюдения (машина выключена) → событие «unknown».

        Иначе демон, которого мы не видели, засчитывался бы как «не
        понадобился снова» — и доля ложных срабатываний была бы занижена,
        а вердикт по пилоту — ложнооптимистичен.
        """
        state = {
            "last_sweep_at": NOW - 20 * HOUR,   # нас не было 20 ч
            "would_kill": [{"pid": 1, "at": NOW - 7 * HOUR, "reused": False,
                            "final": False, "starttime": 1, "mb": 100}],
        }
        metric = reaper.update_metric(state, [], NOW, self.cfg)
        self.assertEqual(metric["unknown"], 1)
        self.assertEqual(metric["counted"], 0)
        self.assertEqual(metric["rate"], 0.0)
        self.assertEqual(state["would_kill"][0]["outcome"], "unknown")

    def test_continuous_observation_keeps_event_countable(self):
        state = {
            "last_sweep_at": NOW - 5 * 60,       # свипы шли как обычно
            "would_kill": [{"pid": 1, "at": NOW - 7 * HOUR, "reused": False,
                            "final": False, "starttime": 1, "mb": 100}],
        }
        metric = reaper.update_metric(state, [], NOW, self.cfg)
        self.assertEqual(metric["not_reused"], 1)
        self.assertEqual(metric["counted"], 1)
        self.assertEqual(metric["unknown"], 0)

    def test_killed_daemon_is_not_counted_as_not_reused(self):
        """В kill-режиме демон убит — «понадобился снова» мы не увидим.

        Событие рождается в том же свипе, где демон убит, и закрывается
        как «killed» — вне знаменателя.
        """
        d = make_daemon(last_build_end=NOW - 10 * HOUR)
        decisions = [reaper.Decision(d, True, "простой")]
        state = {}
        report = [{"pid": d.pid, "action": "SIGTERM", "reason": "простой"}]
        metric = reaper.update_metric(state, decisions, NOW, self.cfg, report)
        self.assertTrue(state["would_kill"][0]["killed"])
        # Через окно событие закрывается, но в знаменатель не идёт.
        later = NOW + 7 * HOUR
        metric = reaper.update_metric(state, [], later, self.cfg)
        self.assertEqual(metric["killed"], 1)
        self.assertEqual(metric["counted"], 0)
        self.assertEqual(metric["not_reused"], 0)
        self.assertEqual(state["would_kill"][0]["outcome"], "killed")

    def test_events_come_from_execution_report(self):
        """Считаются те, кого исполнитель действительно тронет."""
        d = make_daemon(last_build_end=NOW - 10 * HOUR)
        decisions = [reaper.Decision(d, True, "простой")]
        state = {}
        report = [{"pid": d.pid, "action": "skipped",
                   "reason": "превышен лимит убийств за свип"}]
        metric = reaper.update_metric(state, decisions, NOW, self.cfg, report)
        self.assertEqual(metric["events"], 0)
        self.assertEqual(metric["pending"], 0)
        self.assertEqual(state["would_kill"], [])

    def test_dry_run_action_creates_event(self):
        d = make_daemon(last_build_end=NOW - 10 * HOUR)
        decisions = [reaper.Decision(d, True, "простой")]
        state = {}
        report = [{"pid": d.pid, "action": "dry-run", "reason": "простой"}]
        metric = reaper.update_metric(state, decisions, NOW, self.cfg, report)
        self.assertEqual(metric["pending"], 1)

    def test_pending_footprint_reported_separately(self):
        d = make_daemon(last_build_end=NOW - 10 * HOUR, rss_mb=512)
        decisions = [reaper.Decision(d, True, "простой")]
        state = {}
        metric = reaper.update_metric(state, decisions, NOW, self.cfg)
        self.assertEqual(state["would_free_pending_mb"], 512)


# --------------------------------------------------------------------------- #
# Конфиг и состояние
# --------------------------------------------------------------------------- #

class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = reaper.load_config("/nonexistent/config")
        self.assertEqual(cfg["IDLE_SECONDS"], 21600)
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

    def test_bad_value_is_reported(self):
        """Битое значение не проходит молча: оператор должен узнать."""
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as fh:
            fh.write("IDLE_SECONDS = не число\n")
            fh.write("НЕТ_ТАКОГО_КЛЮЧА = 5\n")
            path = fh.name
        try:
            warnings = []
            cfg = reaper.load_config(path, warnings)
            self.assertEqual(cfg["IDLE_SECONDS"], 21600)
            self.assertTrue(any("IDLE_SECONDS" in w for w in warnings))
            self.assertTrue(any("НЕТ_ТАКОГО_КЛЮЧА" in w for w in warnings))
        finally:
            os.unlink(path)

    def test_good_config_has_no_warnings(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as fh:
            fh.write("IDLE_SECONDS = 600\n")
            path = fh.name
        try:
            warnings = []
            reaper.load_config(path, warnings)
            self.assertEqual(warnings, [])
        finally:
            os.unlink(path)

    def test_bad_value_keeps_default(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as fh:
            fh.write("IDLE_SECONDS = не число\n")
            path = fh.name
        try:
            cfg = reaper.load_config(path)
            self.assertEqual(cfg["IDLE_SECONDS"], 21600)
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
    # Память: «всегда убирать» — иначе решение зависело бы от
    # того, сколько памяти свободно на машине, где идут тесты.
    cfg["MEMORY_PRESSURE_MB"] = 0

    def test_dry_run_sends_no_signals(self):
        pid = os.getpid()
        st = reaper.read_proc_stat(pid)
        d = make_daemon(pid=pid, last_build_end=NOW - 5 * HOUR, starttime=st["starttime"])
        report = reaper.execute([reaper.Decision(d, True, "test")], {}, NOW, self.cfg, dry_run=True)
        self.assertEqual([r["action"] for r in report], ["dry-run"])

    def test_kotlin_protected_while_gradle_builds(self):
        """Пока идёт сборка Gradle, Kotlin-демоны не трогаем.

        Защита живёт в таблице правил (правило «gradle-building»), а не в
        исполнителе: раньше исполнитель резал кандидатов в последний
        момент, а отчёт и метрика считали их убитыми.
        """
        g = make_daemon(pid=1001, kind="gradle",
                        last_build_start=NOW - 60, last_build_end=None)
        k = make_daemon(pid=1002, kind="kotlin", log_mtime=NOW - 5 * HOUR)
        state = {"daemons": {
            "1001": {"first_seen": NOW - 10 * HOUR, "cpu_ticks": 1000},
            "1002": {"first_seen": NOW - 10 * HOUR, "cpu_ticks": 1000},
        }}
        decisions = {d.daemon.pid: d
                     for d in reaper.classify([g, k], state, NOW, self.cfg)}
        self.assertFalse(decisions[1002].kill)
        self.assertEqual(decisions[1002].rule, "protected-by-build")
        self.assertIn("сборка Gradle", decisions[1002].reason)

    def test_kotlin_killed_when_no_gradle_build(self):
        """Без сборки Gradle Kotlin-демон простоя выводится под удар."""
        k = make_daemon(pid=1002, kind="kotlin", log_mtime=NOW - 5 * HOUR)
        state = {"daemons": {"1002": {"first_seen": NOW - 10 * HOUR,
                                      "cpu_ticks": 1000}}}
        decisions = reaper.classify([k], state, NOW, self.cfg)
        self.assertTrue(decisions[0].kill)

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

    def test_mode_dry_run_writes_state_and_kills_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(f"STATE_DIR = {tmp}/state\nLOCK_PATH = {tmp}/reaper.lock\n")
            proc = self._run("--mode=dry-run", "--config", cfg)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("режим dry-run", proc.stdout)
            self.assertNotIn("SIGTERM", proc.stdout)
            self.assertTrue(os.path.exists(os.path.join(tmp, "state", "state.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)


# --------------------------------------------------------------------------- #
# Калибровка порогов (п.1) и Kotlin как отдельная цель (п.2)
# --------------------------------------------------------------------------- #

class TestThresholds(unittest.TestCase):
    """Пороги простоя: Gradle и Kotlin живут по разным правилам."""

    cfg = dict(reaper.DEFAULTS)
    # Память: «всегда убирать» — иначе решение зависело бы от
    # того, сколько памяти свободно на машине, где идут тесты.
    cfg["MEMORY_PRESSURE_MB"] = 0

    def decide(self, d, st=None):
        if st is None:
            st = state_for(d, NOW - 10 * HOUR, cpu_ticks=1000)
        return reaper.classify([d], st, NOW, self.cfg)[0]

    def test_gradle_threshold_is_six_hours(self):
        """5 ч простоя Gradle — ещё мало: у Gradle свой таймаут 180 мин,
        наш порог обязан быть выше, иначе мы конкурируем с ним."""
        d = make_daemon(last_build_end=NOW - 5 * HOUR)
        self.assertFalse(self.decide(d).kill)

    def test_gradle_above_six_hours_killed(self):
        d = make_daemon(last_build_end=NOW - 7 * HOUR)
        self.assertTrue(self.decide(d).kill)

    def test_kotlin_threshold_is_two_hours(self):
        """Kotlin-демон умирает только по «No more clients left» — его
        не убирает никто, поэтому порог вдвое меньше."""
        d = make_daemon(kind="kotlin", log_mtime=NOW - 2.5 * HOUR)
        self.assertTrue(self.decide(d).kill)

    def test_kotlin_below_threshold_kept(self):
        d = make_daemon(kind="kotlin", log_mtime=NOW - 1.5 * HOUR)
        self.assertFalse(self.decide(d).kill)

    def test_idle_limit_helper(self):
        self.assertEqual(reaper.idle_limit(self.cfg, "gradle"), 21600)
        self.assertEqual(reaper.idle_limit(self.cfg, "kotlin"), 7200)


class TestStuckDaemon(unittest.TestCase):
    """Зависший демон: клиент отвалился во время сборки."""

    cfg = dict(reaper.DEFAULTS)
    # Память: «всегда убирать» — иначе решение зависело бы от
    # того, сколько памяти свободно на машине, где идут тесты.
    cfg["MEMORY_PRESSURE_MB"] = 0

    def decide(self, d, st=None):
        if st is None:
            st = state_for(d, NOW - 10 * HOUR, cpu_ticks=1000)
        return reaper.classify([d], st, NOW, self.cfg)[0]

    def test_canceled_build_killed_by_stuck_rule(self):
        d = make_daemon(last_build_start=NOW - 10 * HOUR, last_build_end=None,
                        canceled_at=NOW - 9 * HOUR, stuck_build=True)
        dec = self.decide(d)
        self.assertTrue(dec.kill)
        self.assertIn("завис", dec.reason)

    def test_fresh_cancel_not_reported_as_stuck(self):
        """Свежая отмена — это не «зависший демон».

        Демон с очень долгой сборкой без CPU кандидат на общих условиях
        (простой считается от старта сборки), но причиной называть
        «клиент отвалился» нельзя: отмена была час назад, клиент может
        вернуться.
        """
        d = make_daemon(last_build_start=NOW - 10 * HOUR, last_build_end=None,
                        canceled_at=NOW - 1 * HOUR, stuck_build=True)
        dec = self.decide(d)
        self.assertNotIn("клиент отвалился", dec.reason)

    def test_new_build_cancels_stuck_state(self):
        """Сборка после отмены → демон не «завис»."""
        d = make_daemon(last_build_start=NOW - 2 * HOUR, last_build_end=NOW - HOUR,
                        canceled_at=NOW - 9 * HOUR, stuck_build=True)
        self.assertFalse(self.decide(d).kill)

    def test_canceled_state_helper(self):
        self.assertEqual(reaper.canceled_state(100.0, 200.0), None)
        self.assertEqual(reaper.canceled_state(100.0, 50.0), 100.0)
        self.assertIsNone(reaper.canceled_state(None, 50.0))
        self.assertEqual(reaper.canceled_state(100.0, None), 100.0)


class TestRulesVersion(unittest.TestCase):
    """Смена правил обнуляет метрику пилота: она измеряет другое."""

    def test_mismatch_resets_metric(self):
        state = {
            "rules_version": reaper.RULES_VERSION - 1,
            "would_kill": [{"pid": 1, "at": 100.0, "reused": True, "final": True}],
            "metric": {"events": 5, "reused": 3, "rate": 0.6, "pending": 0},
            "pilot_started_at": 123.0,
            "stale_peer_events": 7,
            "stale_peer_daemons": {"1": 7},
        }
        self.assertTrue(reaper.sync_rules_version(state))
        self.assertEqual(state["rules_version"], reaper.RULES_VERSION)
        self.assertEqual(state["would_kill"], [])
        self.assertEqual(state["metric"]["events"], 0)
        self.assertEqual(state["pilot_started_at"], 0.0)
        self.assertEqual(state["stale_peer_events"], 0)

    def test_same_version_keeps_metric(self):
        state = {
            "rules_version": reaper.RULES_VERSION,
            "would_kill": [{"pid": 1, "at": 100.0, "reused": True, "final": True}],
            "metric": {"events": 5, "reused": 3, "rate": 0.6, "pending": 0},
        }
        self.assertFalse(reaper.sync_rules_version(state))
        self.assertEqual(state["metric"]["events"], 5)
        self.assertEqual(len(state["would_kill"]), 1)


class TestKindSummary(unittest.TestCase):
    """Сводка по классам демонов в отчёте (п.2)."""

    cfg = dict(reaper.DEFAULTS)
    # Память: «всегда убирать» — иначе решение зависело бы от
    # того, сколько памяти свободно на машине, где идут тесты.
    cfg["MEMORY_PRESSURE_MB"] = 0

    def test_summary_separates_kinds(self):
        g = make_daemon(pid=1, kind="gradle", rss_mb=400)
        k = make_daemon(pid=2, kind="kotlin", rss_mb=300)
        decisions = [reaper.Decision(g, True, "простой"),
                     reaper.Decision(k, False, "работает")]
        summary = reaper.kind_summary(decisions, self.cfg)
        self.assertEqual(summary["gradle"]["count"], 1)
        self.assertEqual(summary["gradle"]["kill_count"], 1)
        self.assertEqual(summary["gradle"]["footprint_mb"], 400)
        self.assertEqual(summary["kotlin"]["kill_count"], 0)
        self.assertEqual(summary["kotlin"]["idle_limit_seconds"], 7200)
        self.assertEqual(summary["gradle"]["idle_limit_seconds"], 21600)

    def test_status_mentions_both_kinds(self):
        g = make_daemon(pid=1, kind="gradle", rss_mb=400)
        k = make_daemon(pid=2, kind="kotlin", rss_mb=300)
        decisions = [reaper.Decision(g, True, "простой"),
                     reaper.Decision(k, False, "работает")]
        text = reaper.format_status(decisions, {"metric": {}}, self.cfg)
        self.assertIn("Gradle", text)
        self.assertIn("Kotlin", text)

    def test_json_has_kinds(self):
        import json as _json
        g = make_daemon(pid=1, kind="gradle", rss_mb=400)
        decisions = [reaper.Decision(g, True, "простой")]
        payload = _json.loads(reaper.format_json(decisions, {"metric": {}},
                                                 self.cfg, now=NOW))
        self.assertEqual(payload["schema_version"], 4)
        self.assertIn("kinds", payload)
        self.assertEqual(payload["kinds"]["gradle"]["count"], 1)
        self.assertEqual(payload["config"]["KOTLIN_IDLE_SECONDS"], 7200)

    def test_json_has_memory_and_rule(self):
        """Схема 3: в JSON видно память, правило и цену воссоздания."""
        import json as _json
        g = make_daemon(pid=1, kind="gradle", rss_mb=400)
        decisions = [reaper.Decision(g, True, "простой", rule="kill",
                                     recreate_cost_seconds=40)]
        payload = _json.loads(reaper.format_json(decisions, {"metric": {}},
                                                 self.cfg, now=NOW))
        self.assertIn("memory", payload)
        self.assertIn("available_mb", payload["memory"])
        self.assertEqual(payload["memory"]["pressure_mb"],
                         self.cfg["MEMORY_PRESSURE_MB"])
        self.assertEqual(payload["memory"]["large_daemon_mb"],
                         self.cfg["KILL_LARGE_DAEMON_MB"])
        self.assertEqual(payload["daemons"][0]["rule"], "kill")
        self.assertEqual(payload["daemons"][0]["recreate_cost_seconds"], 40)

    def test_schema_version_is_public(self):
        """Потребитель JSON обязан видеть версию схемы, а не угадывать."""
        self.assertEqual(reaper.JSON_SCHEMA_VERSION, 4)


# --------------------------------------------------------------------------- #
# Контекст: свободная память и свежая сборка Gradle (общий объект)
# --------------------------------------------------------------------------- #

class TestContext(unittest.TestCase):
    """Контекст — то, что невозможно знать по одному демону."""

    def test_mem_available_is_measurable(self):
        value = reaper.mem_available_mb()
        if value is None:
            self.skipTest("на этой машине нет /proc/meminfo")
        self.assertGreater(value, 0)

    def test_context_counts_daemons_and_footprint(self):
        g = make_daemon(pid=1, kind="gradle", rss_mb=400, swap_mb=100)
        k = make_daemon(pid=2, kind="kotlin", rss_mb=300)
        ctx = reaper.build_context([g, k], NOW)
        self.assertEqual(ctx.daemon_count, 2)
        self.assertEqual(ctx.total_footprint_mb, 800)
        self.assertEqual(ctx.now, NOW)

    def test_context_sees_fresh_gradle_build(self):
        g = make_daemon(pid=1, kind="gradle",
                        last_build_start=NOW - 60, last_build_end=None)
        ctx = reaper.build_context([g], NOW)
        self.assertIsNotNone(ctx.gradle_build_age)
        self.assertAlmostEqual(ctx.gradle_build_age, 60, delta=1)

    def test_context_without_gradle_build(self):
        g = make_daemon(pid=1, kind="gradle", last_build_end=NOW - 60)
        ctx = reaper.build_context([g], NOW)
        self.assertIsNone(ctx.gradle_build_age)

    def test_decision_carries_context(self):
        d = make_daemon(pid=1, kind="gradle", rss_mb=400)
        d.last_build_end = NOW - 10 * HOUR
        state = {"daemons": {"1": {"first_seen": NOW - 10 * HOUR,
                                   "cpu_ticks": 1000}}}
        ctx = reaper.Context(now=NOW, mem_available_mb=12345)
        dec = reaper.classify([d], state, NOW, dict(reaper.DEFAULTS),
                              context=ctx)[0]
        self.assertIsNotNone(dec.context)
        self.assertEqual(dec.context.mem_available_mb, 12345)
        self.assertEqual(dec.context.daemon_count, 1)


# --------------------------------------------------------------------------- #
# Наблюдаемость правил и детекция деградации (блок 2)
# --------------------------------------------------------------------------- #

class TestObservability(unittest.TestCase):
    """Видно, какие правила работают и не слеп ли рейпер."""

    cfg = dict(reaper.DEFAULTS)
    # Память: «всегда убирать» — иначе решение зависело бы от
    # того, сколько памяти свободно на машине, где идут тесты.
    cfg["MEMORY_PRESSURE_MB"] = 0

    def test_rule_hits_counted(self):
        d1 = make_daemon(pid=1, last_build_end=NOW - 10 * HOUR)
        d2 = make_daemon(pid=2, last_build_end=NOW - 10 * HOUR)
        state = {"daemons": {
            "1": {"first_seen": NOW - 10 * HOUR, "cpu_ticks": 1000},
            "2": {"first_seen": NOW - 10 * HOUR, "cpu_ticks": 1000},
        }}
        decisions = reaper.classify([d1, d2], state, NOW, self.cfg)
        hits = reaper.note_rule_hits(state, decisions)
        self.assertEqual(hits.get("kill"), 2)
        self.assertEqual(state["rule_hits_last"], hits)
        self.assertEqual(state["rule_hits"]["kill"], 2)

    def test_rule_hits_accumulate(self):
        d = make_daemon(pid=1, last_build_end=NOW - 10 * HOUR)
        state = {"daemons": {"1": {"first_seen": NOW - 10 * HOUR,
                                   "cpu_ticks": 1000}}}
        decisions = reaper.classify([d], state, NOW, self.cfg)
        reaper.note_rule_hits(state, decisions)
        reaper.note_rule_hits(state, decisions)
        self.assertEqual(state["rule_hits"]["kill"], 2)

    def test_degradation_when_most_daemons_unmeasured(self):
        """Если почти все демоны под «не знаем» — детекция деградировала."""
        d = make_daemon(pid=1, log_ok=False)      # лог не найден → не измерен
        state = {"daemons": {"1": {"first_seen": NOW - 10 * HOUR,
                                   "cpu_ticks": 1000}}}
        decisions = reaper.classify([d], state, NOW, self.cfg)
        measured, total = reaper.measure_coverage(state, decisions, self.cfg)
        self.assertEqual(total, 1)
        self.assertEqual(measured, 0)
        warnings = reaper.degradation_warnings(state, self.cfg, measured, total)
        self.assertTrue(warnings)
        self.assertIn("деградировала", warnings[0])

    def test_no_degradation_when_all_measured(self):
        d = make_daemon(pid=1, last_build_end=NOW - 10 * HOUR)
        state = {"daemons": {"1": {"first_seen": NOW - 10 * HOUR,
                                   "cpu_ticks": 1000}}}
        decisions = reaper.classify([d], state, NOW, self.cfg)
        measured, total = reaper.measure_coverage(state, decisions, self.cfg)
        self.assertEqual(measured, 1)
        self.assertEqual(reaper.degradation_warnings(state, self.cfg,
                                                    measured, total), [])

    def test_degradation_on_sudden_drop(self):
        """Число измеренных демонов упало больше чем вдвое."""
        state = {"measured_count": 8, "measured_prev": 8}
        warnings = reaper.degradation_warnings(state, self.cfg, measured=3,
                                                total=3)
        self.assertTrue(any("упало" in w for w in warnings))

    def test_health_reports_degradation(self):
        state = {"last_sweep_at": NOW, "metric": {},
                 "degradation": ["измерить удалось лишь 0 из 5 демонов"]}
        text, code = reaper.health(self.cfg, state)
        self.assertEqual(code, 1)
        self.assertIn("деградация", text)

    def test_status_shows_rule_hits(self):
        d = make_daemon(pid=1, last_build_end=NOW - 10 * HOUR)
        state = {"metric": {}, "rule_hits_last": {"kill": 1}}
        text = reaper.format_status([reaper.Decision(d, True, "простой")],
                                     state, self.cfg)
        self.assertIn("правила за последний свип", text)
        self.assertIn("kill", text)

    def test_status_shows_unknown_and_killed(self):
        d = make_daemon(pid=1, last_build_end=NOW - 10 * HOUR)
        state = {"metric": {"events": 3, "counted": 1, "reused": 0,
                            "unknown": 1, "killed": 1, "pending": 2,
                            "rate": 0.0}}
        text = reaper.format_status([reaper.Decision(d, True, "простой")],
                                     state, self.cfg)
        self.assertIn("вне знаменателя", text)


class TestStateModuleLayout(unittest.TestCase):
    """Структура модулей состояния: `reaper.state` — точка входа.

    Смысл проверки: `reaper.state` разбит на `reaper.metrics` (метрики
    пилота и покрытие) и `reaper.journal` (журналы), но это переименование
    внутреннего устройства — публичные имена обязаны остаться на месте,
    иначе сломаются `cli.py`, `report.py` и инструменты из `tools/`.
    """

    # Полный публичный контракт модуля: так он выглядел до разбиения.
    PUBLIC_NAMES = (
        "DAEMON_FIELDS", "GLOBAL_FIELDS", "STATE_VERSION", "UNMEASURED_RULES",
        "append_kill", "append_sweep", "degradation_warnings", "kills_path",
        "load_state", "measure_coverage", "note_rule_hits", "note_stale_peers",
        "read_kills", "record_freed", "save_state", "state_path",
        "sweep_record", "sweeps_path", "sync_kind_signatures",
        "sync_rules_version", "update_metric",
    )

    def test_public_names_still_importable_from_state(self):
        import reaper.state as state_mod
        for name in self.PUBLIC_NAMES:
            self.assertTrue(hasattr(state_mod, name), f"пропал reaper.state.{name}")

    def test_state_is_a_facade_not_a_copy(self):
        """Имена в `state` — те же объекты, а не копии.

        Копия означала бы, что модуль разошёлся с теми, где живёт
        настоящая реализация: правка в одном месте не дойдёт до другого.
        """
        import reaper.journal as journal_mod
        import reaper.metrics as metrics_mod
        import reaper.state as state_mod
        for name in ("update_metric", "measure_coverage", "note_rule_hits",
                     "note_stale_peers", "degradation_warnings", "record_freed",
                     "sync_kind_signatures", "sync_rules_version",
                     "UNMEASURED_RULES"):
            self.assertIs(getattr(state_mod, name), getattr(metrics_mod, name))
        for name in ("append_kill", "append_sweep", "read_kills", "sweep_record",
                     "kills_path", "sweeps_path"):
            self.assertIs(getattr(state_mod, name), getattr(journal_mod, name))

    def test_state_file_format_unchanged(self):
        """Файл состояния: те же ключи, тот же порядок, та же разметка."""
        import reaper.state as state_mod
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"STATE_DIR": tmp}
            state = state_mod.load_state(cfg)
            state_mod.save_state(cfg, state)
            with open(os.path.join(tmp, "state.json"), encoding="utf-8") as fh:
                raw = fh.read()
        # Ключи отсортированы и на месте — от этого зависит diff состояний.
        self.assertTrue(raw.startswith("{\n"))
        keys = list(json.loads(raw, object_pairs_hook=lambda pairs: dict(pairs)))
        self.assertEqual(keys, sorted(keys))
        self.assertIn("version", keys)
        self.assertIn("would_kill", keys)
        self.assertEqual(set(keys), set(state_mod.GLOBAL_FIELDS))
