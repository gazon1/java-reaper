#!/usr/bin/env python3
"""Тесты сторожа (п.10) и месячного отчёта (п.11).

Сторож и отчёт — вспомогательные инструменты, но именно они замечают
«рейпер молча сломался» и «рейпер работает, но вредит». Их поведение
проверяется на изолированном state-dir, без касания живой системы.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import _api as reaper  # noqa: E402
from reaper import history, logfiles  # noqa: E402


def _load_tool(name: str, filename: str):
    """Загружает инструмент из tools/ как модуль."""
    path = os.path.join(ROOT, "tools", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watchdog = _load_tool("reaper_watchdog", "watchdog.py")
monthly = _load_tool("reaper_monthly", "monthly-report.py")
analyze = _load_tool("reaper_analyze_idle", "analyze-idle.py")
idlestats = reaper.idlestats
logfiles = reaper.logfiles


class TestWatchdog(unittest.TestCase):
    """Сторож: заметить «молчание» и починить."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_state(self, **fields):
        data = {"version": reaper.STATE_VERSION, "daemons": {},
                "last_sweep_at": time.time()}
        data.update(fields)
        with open(os.path.join(self.state_dir, "state.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(data, fh)

    def _cfg(self):
        cfg = dict(reaper.DEFAULTS)
        cfg["STATE_DIR"] = self.state_dir
        cfg["LOCK_PATH"] = os.path.join(self.tmp.name, "reaper.lock")
        cfg["DISABLED_FLAG"] = os.path.join(self.tmp.name, "disabled")
        return cfg

    def test_last_sweep_age_none_when_never_swept(self):
        self.assertIsNone(watchdog.last_sweep_age({}))

    def test_last_sweep_age_computed(self):
        age = watchdog.last_sweep_age({"last_sweep_at": time.time() - 120})
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 120, delta=5)

    def test_disabled_flag_returns_code_2(self):
        cfg = self._cfg()
        os.makedirs(os.path.dirname(cfg["DISABLED_FLAG"]), exist_ok=True)
        open(cfg["DISABLED_FLAG"], "w", encoding="utf-8").close()
        self.assertEqual(watchdog.main_with_config(cfg), 2)

    def test_fresh_sweep_is_ok(self):
        self._write_state()
        cfg = self._cfg()
        self.assertEqual(watchdog.main_with_config(cfg), 0)

    def test_stale_sweep_triggers_rescue(self):
        self._write_state(last_sweep_at=time.time() - 3600)
        cfg = self._cfg()
        # Запасной свип выполняется настоящим рейпером: он пройдёт,
        # т.к. state-dir изолирован.
        self.assertEqual(watchdog.main_with_config(cfg), 0)
        with open(os.path.join(self.state_dir, "state.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertGreater(data["last_sweep_at"], time.time() - 120,
                           "запасной свип не обновил метку")

    def test_never_swept_triggers_rescue(self):
        self._write_state(last_sweep_at=0.0)
        cfg = self._cfg()
        self.assertEqual(watchdog.main_with_config(cfg), 0)

    def test_disabled_flag_blocks_rescue(self):
        """С аварийным тормозом сторож не чинит, а просто молчит."""
        self._write_state(last_sweep_at=time.time() - 3600)
        cfg = self._cfg()
        os.makedirs(os.path.dirname(cfg["DISABLED_FLAG"]), exist_ok=True)
        open(cfg["DISABLED_FLAG"], "w", encoding="utf-8").close()
        before = os.path.getmtime(os.path.join(self.state_dir, "state.json"))
        time.sleep(0.01)
        self.assertEqual(watchdog.main_with_config(cfg), 2)
        after = os.path.getmtime(os.path.join(self.state_dir, "state.json"))
        self.assertEqual(before, after, "сторож тронул состояние при флаге")


class TestMonthlyReport(unittest.TestCase):
    """Месячный отчёт: цифры сходятся с журналом убийств."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)
        self.cfg = dict(reaper.DEFAULTS)
        self.cfg["STATE_DIR"] = self.state_dir

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, kills=(), metric=None, **extra):
        state = {"version": reaper.STATE_VERSION, "daemons": {},
                 "last_sweep_at": time.time(),
                 "pilot_started_at": time.time() - 10 * 86400}
        if metric is not None:
            state["metric"] = metric
        state.update(extra)
        with open(os.path.join(self.state_dir, "state.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(state, fh)
        for k in kills:
            reaper.append_kill(self.cfg, k)

    def test_empty_report(self):
        self._seed()
        data = monthly.collect(self.cfg, 30)
        self.assertEqual(data["kills_in_period"], 0)
        self.assertEqual(data["kills_total"], 0)
        text = monthly.format_report(data)
        self.assertIn("за период убийств не было", text)

    def test_kills_are_counted(self):
        now = time.time()
        self._seed(kills=[
            {"at": now - 100, "pid": 1, "action": "SIGTERM",
             "reason": "простой 4 ч, footprint 500 МБ", "footprint_mb": 500},
            {"at": now - 200, "pid": 2, "action": "SIGKILL",
             "reason": "не ушёл после SIGTERM", "footprint_mb": 300},
        ])
        data = monthly.collect(self.cfg, 30)
        self.assertEqual(data["kills_in_period"], 2)
        self.assertEqual(data["killed_mb"], 800)
        self.assertEqual(data["kills_by_action"]["SIGTERM"], 1)
        self.assertEqual(data["kills_by_action"]["SIGKILL"], 1)

    def test_old_kills_excluded(self):
        now = time.time()
        self._seed(kills=[
            {"at": now - 40 * 86400, "pid": 1, "action": "SIGTERM",
             "footprint_mb": 500}])
        data = monthly.collect(self.cfg, 30)
        self.assertEqual(data["kills_in_period"], 0)
        self.assertEqual(data["kills_total"], 1)

    def test_metric_carried_into_report(self):
        self._seed(metric={"events": 20, "reused": 8, "rate": 0.4,
                           "pending": 3})
        data = monthly.collect(self.cfg, 30)
        self.assertEqual(data["metric"]["events"], 20)
        self.assertAlmostEqual(data["false_positive_rate"], 0.4)
        text = monthly.format_report(data)
        self.assertIn("40 %", text)

    def test_small_sample_noted_in_text(self):
        self._seed(metric={"events": 3, "reused": 1, "rate": 0.33, "pending": 0})
        text = monthly.format_report(monthly.collect(self.cfg, 30))
        self.assertIn("данных пока мало", text)

    def test_stale_peer_count_shown(self):
        self._seed(stale_peer_events=7, stale_peer_daemons={"1": 3, "2": 4})
        data = monthly.collect(self.cfg, 30)
        self.assertEqual(data["stale_peer_events"], 7)
        text = monthly.format_report(data)
        self.assertIn("7 раз", text)

    def test_json_output_is_valid(self):
        self._seed()
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "monthly-report.py"),
             "--json"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": self.tmp.name})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("metric", payload)
        self.assertIn("period_days", payload)

    def test_sweeps_journal_gives_pressure_numbers(self):
        """Отчёт читает журнал свипов: сколько памяти висело под ударом."""
        now = time.time()
        for i, (would_free, footprint) in enumerate([(1000, 5000),
                                                     (3000, 8000)]):
            reaper.append_sweep(self.cfg, {
                "at": now - 60 + i, "daemons": 10, "footprint_mb": footprint,
                "would_kill": 2, "would_free_mb": would_free,
                "by_kind": {"gradle": {"count": 5, "footprint_mb": footprint,
                                       "would_free_mb": would_free},
                            "kotlin": {"count": 5, "footprint_mb": 0,
                                       "would_free_mb": 0}},
                "rules": {"kill": 2, "not-idle": 8},
            })
        data = monthly.collect(self.cfg, 30)
        self.assertEqual(data["sweeps"], 2)
        self.assertEqual(data["max_under_threat_mb"], 3000)
        self.assertEqual(data["avg_under_threat_mb"], 2000)
        self.assertEqual(data["rule_hits"], {"kill": 4, "not-idle": 16})
        self.assertEqual(data["current_footprint_mb"]["gradle"], 8000)
        text = monthly.format_report(data)
        self.assertIn("под ударом висело", text)
        self.assertIn("not-idle", text)

    def test_sweeps_outside_period_are_ignored(self):
        old = time.time() - 40 * 86400
        reaper.append_sweep(self.cfg, {"at": old, "would_free_mb": 5000,
                                       "rules": {}})
        data = monthly.collect(self.cfg, 30)
        self.assertEqual(data["sweeps"], 0)
        self.assertEqual(data["max_under_threat_mb"], 0)


# --------------------------------------------------------------------------- #
# Архивирование умерших демонов самим рейпером
# --------------------------------------------------------------------------- #

class TestArchiveVanished(unittest.TestCase):
    """Логи Kotlin живут в /tmp и исчезают при перезагрузке.

    Рейпер обязан сохранить сводку, пока файл жив: иначе демон исчезнет
    из истории навсегда.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)
        self.cfg = dict(reaper.DEFAULTS)
        self.cfg["STATE_DIR"] = self.state_dir

    def tearDown(self):
        self.tmp.cleanup()

    def _kotlin_log(self, instance="kotlin-daemon.2026-10-20.08-29-00-827.00"):
        path = os.path.join(self.tmp.name, instance + ".log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("2026-10-20 08:29:00.100 [daemon] Kotlin daemon started\n")
            fh.write("2026-10-20 08:31:00.100 [daemon] compiling\n")
        return path

    def test_vanished_daemon_is_archived(self):
        path = self._kotlin_log()
        state = {"daemons": {"4242": {"kind": "kotlin", "log_path": path}}}
        archived = reaper.archive_vanished(self.cfg, state, [], time.time())
        self.assertEqual(archived, 1)
        history = os.path.join(self.state_dir, "logs-history.jsonl")
        records = reaper.read_history(history)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "kotlin")

    def test_archive_records_last_seen_as_lower_bound(self):
        """Нижняя граница смерти: демон точно жил в конце прошлого свипа.

        Считать, что демон жил до момента обнаружения пропажи, — значит
        завышать его простой. Поэтому запоминаем и «когда видели в
        последний раз».
        """
        path = self._kotlin_log()
        last_sweep = time.time() - 300
        state = {"daemons": {"4242": {"kind": "kotlin", "log_path": path}},
                 "last_sweep_at": last_sweep}
        reaper.archive_vanished(self.cfg, state, [], time.time())
        history = os.path.join(self.state_dir, "logs-history.jsonl")
        record = reaper.read_history(history)[0]
        self.assertAlmostEqual(record["last_seen_at"], last_sweep, places=3)
        self.assertGreater(record["death_observed_at"], record["last_seen_at"])

    def test_archive_records_observed_death(self):
        """Момент пропажи демона — это его смерть: лог-то молчит давно."""
        path = self._kotlin_log()
        state = {"daemons": {"4242": {"kind": "kotlin", "log_path": path}}}
        now = time.time()
        reaper.archive_vanished(self.cfg, state, [], now)
        history = os.path.join(self.state_dir, "logs-history.jsonl")
        record = reaper.read_history(history)[0]
        self.assertIn("death_observed_at", record)
        self.assertAlmostEqual(record["death_observed_at"], now, delta=5)

    def test_live_daemon_is_not_archived(self):
        path = self._kotlin_log()
        state = {"daemons": {"4242": {"kind": "kotlin", "log_path": path}}}

        class FakeDaemon:
            pid = 4242

        archived = reaper.archive_vanished(self.cfg, state, [FakeDaemon()])
        self.assertEqual(archived, 0)
        self.assertFalse(os.path.exists(
            os.path.join(self.state_dir, "logs-history.jsonl")))

    def test_missing_log_is_skipped(self):
        state = {"daemons": {"4242": {"kind": "kotlin",
                                      "log_path": "/tmp/нет-такого.log"}}}
        archived = reaper.archive_vanished(self.cfg, state, [])
        self.assertEqual(archived, 0)

    def test_dead_daemon_gives_measurable_idle(self):
        """С отметкой смерти простой виден: без неё демон «умирает» вместе
        с последней записью в логе, и простоя не видно вовсе."""
        path = self._kotlin_log()
        state = {"daemons": {"4242": {"kind": "kotlin", "log_path": path}}}
        reaper.archive_vanished(self.cfg, state, [], time.time() + 7200)
        history = os.path.join(self.state_dir, "logs-history.jsonl")
        record = reaper.read_history(history)[0]
        self.assertGreater(record["death_observed_at"],
                           record["last_activity"] + 3600)


class TestSummarySelfDescription(unittest.TestCase):
    """Сводка должна описывать себя сама: модель активности и пороги цели.

    Иначе история не самодостаточна: «умер ли демон сам» и «простой ли он
    дольше порога» приходилось бы угадывать по нынешним настройкам цели, а
    модель активности — по наличию ключа в словаре (смена формы сводки
    тихо ломала бы разбор).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "daemon-4242.out.log")
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write(
                "2026-10-19T19:36:17.864+0300 [INFO] [org.gradle.launcher] "
                "Received command: Build{id=1}\n"
                "2026-10-19T19:37:00.000+0300 [INFO] [org.gradle.launcher] "
                "Marking the daemon as idle, address: []\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _spec(self, **kw):
        base = dict(name="gradle", label="Gradle",
                    match_cmdline="org.gradle.launcher.daemon",
                    log_name_regex=r"^daemon-\d+\.out\.log$",
                    activity=reaper.ACTIVITY_MARKERS, ts_format="iso",
                    build_start="Received command: Build",
                    build_end="Marking the daemon as idle",
                    idle_seconds=21600.0, self_timeout_seconds=10800.0,
                    rebuildable=True)
        base.update(kw)
        return reaper.TargetSpec(**base)

    def test_summary_remembers_thresholds_and_model(self):
        rec = reaper.summarize_builds(self.log, self._spec(), pid=4242)
        self.assertEqual(rec["activity_kind"], "builds")
        self.assertEqual(rec["idle_seconds"], 21600.0)
        self.assertEqual(rec["self_timeout_seconds"], 10800.0)

    def test_intervals_follow_explicit_model(self):
        """Явное поле выбора модели главнее содержимого словаря."""
        rec = {"activity_kind": "activity", "bucket_seconds": 300,
               "activity": [[1000.0, 1.0]], "builds": [[1.0, 2.0]]}
        spans = reaper.intervals(rec)
        self.assertEqual(spans, [(1000.0, 1300.0)])

    def test_legacy_record_without_field_still_readable(self):
        """Старые записи (без поля) разбираются как раньше — по содержанию."""
        legacy = {"builds": [[1.0, 2.0]]}
        self.assertEqual(reaper.intervals(legacy), [(1.0, 2.0)])
        legacy_activity = {"activity": [[1000.0, 1.0]], "bucket_seconds": 300}
        self.assertEqual(reaper.intervals(legacy_activity), [(1000.0, 1300.0)])


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# Уборка логов мёртвых демонов (п.3)
# --------------------------------------------------------------------------- #

prune = _load_tool("reaper_prune", "prune-logs.py")


class TestPruneLogs(unittest.TestCase):
    """Уборка трогает только логи мёртвых демонов и только по --apply."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gradle = os.path.join(self.tmp.name, "gradle")
        self.kotlin = os.path.join(self.tmp.name, "kotlin")
        os.makedirs(os.path.join(self.gradle, "9.7.1"))
        os.makedirs(self.kotlin)
        self._live = prune.is_live_daemon
        self._open_fn = prune.open_paths_under
        prune.is_live_daemon = lambda pid, spec, targets: pid == 4242
        self._open: set[str] = set()
        prune.open_paths_under = lambda dirs: set(self._open)
        # Цели для теста: те же, что у рейпера, но с путями в tmp.
        self.targets = reaper.TargetSet({
            "gradle": reaper.TargetSpec(
                name="gradle", rebuildable=True,
                log_globs=(os.path.join(self.gradle, "*", "daemon-*.out.log"),),
                log_pid_regex=r"^daemon-(?P<pid>\d+)\.out\.log$"),
            "kotlin": reaper.TargetSpec(
                name="kotlin", rebuildable=True,
                log_globs=(os.path.join(self.kotlin, "kotlin-daemon.*.log*"),),
                log_group_regex=(r"^(?P<group>kotlin-daemon\.\d{4}-\d{2}-\d{2}"
                                 r"\.\d{2}-\d{2}-\d{2}-\d+)\.\d+\.log"
                                 r"(?:\.lck)?$")),
        })

    def tearDown(self):
        prune.is_live_daemon = self._live
        prune.open_paths_under = self._open_fn
        self.tmp.cleanup()

    def _write(self, path, size=1024, age_hours=100.0):
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        t = time.time() - age_hours * 3600
        os.utime(path, (t, t))
        return path

    def _gradle_log(self, pid, **kw):
        return self._write(
            os.path.join(self.gradle, "9.7.1", f"daemon-{pid}.out.log"), **kw)

    def _kotlin_log(self, instance=333, idx=0, **kw):
        name = f"kotlin-daemon.2026-10-20.08-29-00-{instance}.{idx:02d}.log"
        return self._write(os.path.join(self.kotlin, name), **kw)

    def _plan(self, min_age_hours=72.0):
        return prune.collect(self.targets, min_age_hours * 3600, time.time())

    def test_dead_daemon_log_is_planned(self):
        path = self._gradle_log(111)
        plan = self._plan()
        self.assertEqual([p["path"] for p in plan], [path])

    def test_live_daemon_log_is_not_planned(self):
        """PID в имени лога Gradle настоящий: живой демон — не трогаем."""
        self._gradle_log(4242)
        self.assertEqual(self._plan(), [])

    def test_open_file_is_never_removed(self):
        """Файл, который держит открытым процесс, — не мёртвый лог."""
        path = self._gradle_log(111)
        self._open.add(path)
        self.assertEqual(self._plan(), [])

    def test_fresh_log_is_not_planned(self):
        self._gradle_log(111, age_hours=1.0)
        self.assertEqual(self._plan(), [])

    def test_kotlin_log_is_planned(self):
        path = self._kotlin_log(333)
        plan = self._plan()
        self.assertIn(path, [p["path"] for p in plan])

    def test_kotlin_group_protected_by_open_file(self):
        """Живой Kotlin-демон держит открытым только последний файл группы.

        Остальные файлы того же демона удалять нельзя — поэтому открытый
        файл защищает всю группу.
        """
        newest = self._kotlin_log(333, idx=0)
        older = self._kotlin_log(333, idx=1)
        self._open.add(newest)
        plan = self._plan()
        self.assertNotIn(older, [p["path"] for p in plan])

    def test_kotlin_markers_are_never_touched(self):
        """Маркеры Kotlin живут своей жизнью — их не трогаем никогда."""
        markers = []
        for name in ("kotlin-compiler-client-123-is-running",
                     "kotlin-compiler-in-singularitytodo-456.alive",
                     "kotlin-compilation-session-789-is-running"):
            path = self._write(os.path.join(self.kotlin, name))
            markers.append(path)
        self.assertEqual(self._plan(), [])
        for path in markers:
            self.assertTrue(os.path.exists(path))

    def test_unrelated_files_are_ignored(self):
        self._write(os.path.join(self.gradle, "9.7.1", "notes.txt"))
        self._write(os.path.join(self.kotlin, "readme.md"))
        self.assertEqual(self._plan(), [])

    def test_dry_run_deletes_nothing(self):
        path = self._gradle_log(111)
        code = prune.main(["--gradle-dir", self.gradle,
                           "--kotlin-dir", self.kotlin])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(path))

    def test_apply_deletes_and_reports_bytes(self):
        path = self._gradle_log(111, size=4096)
        code = prune.main(["--apply", "--gradle-dir", self.gradle,
                           "--kotlin-dir", self.kotlin])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(path))

    def test_apply_removes_whole_kotlin_group(self):
        log = self._kotlin_log(333, idx=0)
        lock = self._write(log + ".lck", size=16)
        prune.main(["--apply", "--gradle-dir", self.gradle,
                    "--kotlin-dir", self.kotlin])
        self.assertFalse(os.path.exists(log))
        self.assertFalse(os.path.exists(lock))

    def test_history_saved_before_deletion(self):
        """Сводка остаётся, даже если лог удалён: лог — мусор, сводка — данные."""
        path = self._gradle_log(111)
        history = os.path.join(self.tmp.name, "history.jsonl")
        code = prune.main(["--apply", "--gradle-dir", self.gradle,
                           "--kotlin-dir", self.kotlin, "--history", history])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(path))
        loaded = reaper.read_history(history)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["pid"], 111)

    def test_archive_only_keeps_logs(self):
        """--archive-only: только сводки, логи на месте."""
        path = self._gradle_log(111)
        history = os.path.join(self.tmp.name, "history.jsonl")
        prune.main(["--archive-only", "--gradle-dir", self.gradle,
                    "--kotlin-dir", self.kotlin, "--history", history])
        self.assertTrue(os.path.exists(path))
        self.assertEqual(len(reaper.read_history(history)), 1)

    def test_no_history_skips_summaries(self):
        path = self._gradle_log(111)
        history = os.path.join(self.tmp.name, "history.jsonl")
        prune.main(["--apply", "--no-history", "--gradle-dir", self.gradle,
                    "--kotlin-dir", self.kotlin, "--history", history])
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(history))

    def test_history_not_duplicated_on_repeat(self):
        """Повторный прогон архивирования не добавляет копий.

        Симулятор считает каждую запись: копии одного демона исказили бы
        и число убийств, и долю ложных срабатываний.
        """
        self._gradle_log(111)
        history = os.path.join(self.tmp.name, "history.jsonl")
        for _ in range(3):
            prune.main(["--archive-only", "--gradle-dir", self.gradle,
                        "--kotlin-dir", self.kotlin, "--history", history])
        loaded = reaper.read_history(history)
        self.assertEqual(len(loaded), 1)

    def test_json_output_lists_plan(self):
        self._gradle_log(111)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            prune.main(["--json", "--gradle-dir", self.gradle,
                        "--kotlin-dir", self.kotlin])
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["files"], 1)
        self.assertEqual(payload["removed"], 0)

    def test_name_parsers(self):
        """PID и группа берутся по регуляркам из описания цели."""
        gradle = self.targets.get("gradle")
        self.assertEqual(prune.pid_of(gradle, "/x/daemon-123.out.log"), 123)
        self.assertIsNone(prune.pid_of(gradle, "/x/daemon-abc.out.log"))
        kotlin = self.targets.get("kotlin")
        self.assertEqual(
            prune.group_of(kotlin,
                           "/x/kotlin-daemon.2026-10-20.08-29-00-827.00.log"),
            "kotlin-daemon.2026-10-20.08-29-00-827")
        self.assertEqual(prune.group_of(kotlin, "/x/kotlin-daemon.log"),
                         "/x/kotlin-daemon.log")


# --------------------------------------------------------------------------- #
# История демонов и симулятор правил (блоки 3 и 4)
# --------------------------------------------------------------------------- #

simulate = _load_tool("reaper_simulate", "simulate.py")


class TestLogHistory(unittest.TestCase):
    """Сводка по логу: компактная запись вместо мегабайт лога."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "daemon-4242.out.log")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("2026-10-04T12:00:01.500+0300 [INFO] Received command: Build{id=1}\n")
            fh.write("2026-10-04T12:09:00.000+0300 [INFO] Marking the daemon as idle\n")
            fh.write("2026-10-04T12:10:00.000+0300 [INFO] Received command: Build{id=2}\n")
            fh.write("thread 7: client disconnection detected, canceling the build\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_has_builds_and_cancel(self):
        rec = reaper.summarize_gradle(self.path, pid=4242, version="9.7.1")
        self.assertEqual(rec["kind"], "gradle")
        self.assertEqual(rec["pid"], 4242)
        self.assertEqual(len(rec["builds"]), 2)
        # Отмена найдена даже без таймстемпа на её строке: временем отмены
        # считается начало той сборки, во время которой клиент отвалился.
        self.assertIsNotNone(rec["canceled_at"])

    def test_cancel_cleared_by_later_build(self):
        """Новая сборка после отмены → демон не «завис»."""
        rec = reaper.summarize_gradle(self.path, pid=4242)
        later = dict(rec)
        later["builds"] = rec["builds"] + [[rec["canceled_at"] + 10, None]]
        self.assertIsNone(reaper.effective_canceled(later))

    def test_history_roundtrip(self):
        path = os.path.join(self.tmp.name, "history.jsonl")
        rec = reaper.summarize_gradle(self.path, pid=4242)
        reaper.append_history(path, rec)
        loaded = reaper.read_history(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["pid"], 4242)

    def test_intervals_for_gradle(self):
        rec = reaper.summarize_gradle(self.path, pid=4242)
        spans = reaper.intervals(rec)
        self.assertTrue(spans)
        self.assertEqual(len(spans), 2)


class TestSimulator(unittest.TestCase):
    """Симуляция правил по истории: сколько убили бы и сколько ошиблись."""

    def _record(self, kind="gradle", builds=(), first=0.0, mtime=0.0,
                canceled=None):
        return {"kind": kind, "pid": 1, "builds": [list(b) for b in builds],
                "first_activity": first, "last_activity": mtime, "mtime": mtime,
                "canceled_at": canceled}

    def test_idle_daemon_killed_after_threshold(self):
        rec = self._record(builds=[[0.0, 100.0]], first=0.0, mtime=10000.0)
        kt = simulate.kill_time(rec, 3600.0)
        self.assertIsNotNone(kt)
        self.assertAlmostEqual(kt, 100.0 + 3600.0)

    def test_busy_daemon_not_killed(self):
        """Демон, который отработал и умер, не кандидат."""
        rec = self._record(builds=[[0.0, 1000.0]], first=0.0, mtime=1000.0)
        self.assertIsNone(simulate.kill_time(rec, 3600.0))

    def test_build_longer_than_threshold_is_candidate(self):
        """Сборка длиннее порога — кандидат: простой считается от её начала.

        В рейпере такой демон защищён правилами «занят: CPU» и «дети», но
        модель их не знает — поэтому это верхняя оценка убийств.
        """
        rec = self._record(builds=[[0.0, 10000.0]], first=0.0, mtime=10000.0)
        kt = simulate.kill_time(rec, 3600.0)
        self.assertIsNotNone(kt)
        self.assertAlmostEqual(kt, 3600.0)

    def test_gradle_self_timeout_wins(self):
        """Демон, простоявший дольше таймаута Gradle, мёртв до нашего свипа."""
        rec = self._record(builds=[[0.0, 100.0]], first=0.0, mtime=100.0 + 181 * 60)
        # Наш порог 6 ч — позже, чем таймаут Gradle (3 ч): демон уже мёртв.
        self.assertIsNone(simulate.kill_time(rec, 6 * 3600.0))
        # Наш порог 1 ч — раньше: успеваем.
        self.assertIsNotNone(simulate.kill_time(rec, 3600.0))

    def test_stuck_daemon_survives_gradle_timeout(self):
        """Зависший демон таймаутом Gradle не убирается — в этом его суть."""
        rec = self._record(builds=[[0.0, None]], first=0.0, mtime=100.0 + 10 * 3600,
                           canceled=100.0)
        kt = simulate.kill_time(rec, 6 * 3600.0, stuck_threshold=3600.0)
        self.assertIsNotNone(kt)
        self.assertAlmostEqual(kt, 100.0 + 3600.0)

    def test_reuse_detected(self):
        rec = self._record(builds=[[0.0, 100.0], [5000.0, 5100.0]], first=0.0,
                           mtime=5100.0)
        self.assertTrue(simulate.reused_after(rec, 100.0 + 3600.0))

    def test_simulate_counts(self):
        recs = [
            # простаивает 5 ч, порог 1 ч → убили бы
            self._record(builds=[[0.0, 100.0]], first=0.0, mtime=100.0 + 5 * 3600),
            # отработал и умер → не убили бы
            self._record(builds=[[0.0, 1000.0]], first=0.0, mtime=1000.0),
        ]
        result = simulate.simulate(recs, {"60 мин": 3600.0})
        data = result["gradle"]["60 мин"]
        self.assertEqual(data["kills"], 1)
        self.assertEqual(data["false_positives"], 0)
        self.assertEqual(data["died_own"], 1)

    def test_false_positive_counted(self):
        recs = [self._record(builds=[[0.0, 100.0], [5000.0, 5100.0]], first=0.0,
                              mtime=5100.0)]
        result = simulate.simulate(recs, {"60 мин": 3600.0})
        data = result["gradle"]["60 мин"]
        self.assertEqual(data["kills"], 1)
        self.assertEqual(data["false_positives"], 1)
        self.assertAlmostEqual(data["rate"], 1.0)


class TestStuckScan(unittest.TestCase):
    """Скан порога «зависшего»: порог должен быть измерен, а не выбран."""

    def _record(self, kind="gradle", builds=(), first=0.0, mtime=0.0,
                canceled=None):
        return {"kind": kind, "pid": 1, "builds": [list(b) for b in builds],
                "first_activity": first, "last_activity": mtime, "mtime": mtime,
                "canceled_at": canceled}

    def test_stuck_lifetime_measured(self):
        rec = self._record(builds=[[0.0, None]], first=0.0,
                           mtime=100.0 + 2 * 3600, canceled=100.0)
        self.assertAlmostEqual(simulate.stuck_lifetime(rec), 2 * 3600)

    def test_not_stuck_has_no_lifetime(self):
        rec = self._record(builds=[[0.0, 100.0]], first=0.0, mtime=100.0)
        self.assertIsNone(simulate.stuck_lifetime(rec))

    def test_scan_counts_kills_by_threshold(self):
        """Демон, проживший после отмены 3 ч, переживает порог в 1 ч,
        но не порог в 6 ч."""
        recs = [self._record(builds=[[0.0, None]], first=0.0,
                             mtime=100.0 + 3 * 3600, canceled=100.0)]
        result = simulate.simulate_stuck(recs, {"60 мин": 3600.0,
                                                "360 мин": 21600.0})
        data = result["gradle"]
        self.assertEqual(data["60 мин"]["kills"], 1)
        self.assertEqual(data["60 мин"]["stuck"], 1)
        self.assertEqual(data["360 мин"]["kills"], 0)

    def test_scan_reports_median_and_max(self):
        recs = [
            self._record(builds=[[0.0, None]], first=0.0,
                         mtime=100.0 + 600, canceled=100.0),
            self._record(builds=[[0.0, None]], first=0.0,
                         mtime=100.0 + 4 * 3600, canceled=100.0),
        ]
        result = simulate.simulate_stuck(recs, {"60 мин": 3600.0})
        data = result["gradle"]["60 мин"]
        self.assertAlmostEqual(data["max_lifetime_seconds"], 4 * 3600)
        self.assertAlmostEqual(data["median_lifetime_seconds"], (600 + 14400) / 2)

    def test_scan_counts_false_positives(self):
        """Повторное использование зависшего демона невозможно по модели:
        если клиент вернулся к тому же демону, демон не был зависшим."""
        rec = self._record(builds=[[0.0, None], [20000.0, 20100.0]],
                           first=0.0, mtime=20100.0, canceled=100.0)
        result = simulate.simulate_stuck([rec], {"60 мин": 3600.0})
        data = result["gradle"]["60 мин"]
        self.assertEqual(data["stuck"], 0, "демон с новой сборкой не зависший")
        self.assertEqual(data["kills"], 0)

    def test_cli_accepts_stuck_thresholds(self):
        """Скан запускается из командной строки."""
        with tempfile.TemporaryDirectory() as tmp:
            history = os.path.join(tmp, "history.jsonl")
            rec = self._record(builds=[[0.0, None]], first=0.0,
                               mtime=100.0 + 3 * 3600, canceled=100.0)
            reaper.append_history(history, rec)
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tools", "simulate.py"),
                 "--history", history, "--stuck-thresholds", "60,360",
                 "--json"],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertIn("stuck_scan", payload)
            self.assertEqual(payload["stuck_thresholds"]["60 мин"], 3600.0)
            self.assertEqual(payload["stuck_scan"]["gradle"]["60 мин"]["kills"], 1)


# --------------------------------------------------------------------------- #
# Ретроспективный анализ простоя — по целям (targets.yaml)
# --------------------------------------------------------------------------- #


class TestAnalyzeIdle(unittest.TestCase):
    """Анализ простоя идёт по описаниям целей, а не по двум именам.

    Проверяем три вещи: цель с маркерами разбирается; цель без маркеров
    пропускается с внятной причиной (а не молча); PID демона берётся из
    описания цели.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.logs)

    def tearDown(self):
        self.tmp.cleanup()

    def _spec(self, **kw):
        base = dict(
            name="mock", label="Mock",
            match_cmdline="mock.daemon.Bootstrap",
            log_name_regex=r"^mock-\d+\.out\.log$",
            activity=reaper.ACTIVITY_MARKERS,
            ts_format="iso",
            build_start="Received command: Build",
            build_end="Marking the daemon as idle",
            log_globs=(os.path.join(self.logs, "mock-*.out.log"),),
            log_pid_regex=r"^mock-(?P<pid>\d+)\.out\.log$",
        )
        base.update(kw)
        return reaper.TargetSpec(**base)

    def _write_log(self, pid=111, builds=((0, 60), (6600, 6660)),
                   mtime=None):
        """Лог с парами «сборка: (начало, конец)» в секундах от эпохи."""
        path = os.path.join(self.logs, f"mock-{pid}.out.log")
        lines = []
        for start, end in builds:
            lines.append(
                f"{_iso(start)} [INFO] [org.gradle.launcher] "
                f"Received command: Build{{id=1}}")
            lines.append(
                f"{_iso(end)} [INFO] [org.gradle.launcher] "
                f"Marking the daemon as idle, address: []")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        stamp = mtime if mtime is not None else builds[-1][1]
        os.utime(path, (stamp, stamp))
        return path

    def _analyze(self, spec, thresholds=(30,)):
        report, scan = idlestats.analyze_target(
            spec, list(thresholds), 7200, time.time(), 512 * 1024 * 1024)
        return report, scan

    def test_gap_between_builds_is_measured(self):
        """Простой между двумя сборками = 6600 - 60 секунд."""
        self._write_log()
        report, scan = self._analyze(self._spec())
        self.assertEqual(scan["files_read"], 1)
        self.assertEqual(report["totals"]["idle_gaps"], 1)
        self.assertAlmostEqual(report["totals"]["total_idle_hours"],
                               6540 / 3600.0, places=3)

    def test_threshold_splits_kills(self):
        """Порог 30 мин ловит простой, порог 200 мин — нет."""
        self._write_log()
        report, _ = self._analyze(self._spec(), thresholds=(30, 200))
        rows = {row["threshold_min"]: row for row in report["per_threshold"]}
        self.assertEqual(rows[30]["kill_events"], 1)
        self.assertEqual(rows[200]["kill_events"], 0)

    def test_target_without_markers_is_skipped_with_reason(self):
        """Цель с activity=mtime анализом не охватывается — и это сказано."""
        reason = idlestats.analyzable(self._spec(activity=reaper.ACTIVITY_MTIME,
                                                 build_start="", build_end=""))
        self.assertIsNotNone(reason)
        self.assertIn("mtime", reason)

    def test_target_with_markers_is_analyzable(self):
        self.assertIsNone(idlestats.analyzable(self._spec()))

    def test_pid_comes_from_target_description(self):
        """PID демона берётся из log_pid_regex цели, а не из имени файла."""
        spec = self._spec()
        self.assertEqual(logfiles.pid_of(spec, "/x/mock-4242.out.log"), 4242)
        # У цели без log_pid_regex PID неизвестен — и это не «ноль».
        spec = self._spec(log_pid_regex="")
        self.assertIsNone(logfiles.pid_of(spec, "/x/mock-4242.out.log"))

    def test_alive_is_unknown_without_pid_regex(self):
        spec = self._spec(log_pid_regex="")
        self.assertIsNone(logfiles.daemon_alive(spec, "/x/mock-4242.out.log"))

    def test_cli_reports_skipped_target(self):
        """CLI по файлу целей: разобранная цель — в отчёте, пропущенная — с причиной."""
        targets_path = os.path.join(self.tmp.name, "targets.yaml")
        with open(targets_path, "w", encoding="utf-8") as fh:
            fh.write(
                "targets:\n"
                "  - name: mock\n"
                "    label: Mock\n"
                "    match_cmdline: mock.daemon.Bootstrap\n"
                "    log_name_regex: '^mock-\\d+\\.out\\.log$'\n"
                "    activity: markers\n"
                "    ts_format: iso\n"
                "    build_start: 'Received command: Build'\n"
                "    build_end: 'Marking the daemon as idle'\n"
                f"    log_globs: ['{os.path.join(self.logs, 'mock-*.out.log')}']\n"
                "    log_pid_regex: '^mock-(?P<pid>\\d+)\\.out\\.log$'\n"
                "  - name: other\n"
                "    label: Other\n"
                "    match_cmdline: other.daemon.Bootstrap\n"
                "    activity: mtime\n"
            )
        self._write_log()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = analyze.main(["--targets", targets_path, "--thresholds", "30",
                                 "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertIn("mock", payload["targets"])
        self.assertNotIn("other", payload["targets"])
        skipped = {item["kind"]: item["reason"] for item in payload["skipped"]}
        self.assertIn("other", skipped)
        self.assertIn("mtime", skipped["other"])


def _iso(seconds: float) -> str:
    """Таймстемп в формате Gradle (iso со смещением) из секунд эпохи."""
    import datetime as _dt
    moment = _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


# --------------------------------------------------------------------------- #
# Общий модуль работы с файлами логов целей (reaper/logfiles.py)
# --------------------------------------------------------------------------- #


class TestLogFiles(unittest.TestCase):
    """«Чей это лог» — одно правило для уборщика, истории и ретроспективы.

    Раньше правило жило в трёх копиях, и копии расходились: уборщик считал
    один и тот же файл живым, а история — чужим. Теперь правило одно, а
    тесты ниже фиксируют его контракт.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.logs)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, size=16):
        path = os.path.join(self.logs, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return path

    def _spec(self, **kw):
        base = dict(
            name="mock",
            log_globs=(os.path.join(self.logs, "mock-*.log"),),
            log_pid_regex=r"^mock-(?P<pid>\d+)\.log$",
            log_group_regex=r"^(?P<group>mock-\d+)\.\d+\.log$",
            forbidden_prefixes=("mock-marker-",),
        )
        base.update(kw)
        return reaper.TargetSpec(**base)

    # --- поиск файлов ----------------------------------------------------

    def test_expand_pattern_returns_files_only(self):
        """Каталог — не лог: в план уборки он попадать не должен."""
        self._write("mock-1.log")
        os.makedirs(os.path.join(self.logs, "mock-2.log"))
        found = logfiles.expand_pattern(os.path.join(self.logs, "*"))
        self.assertEqual(found, [os.path.join(self.logs, "mock-1.log")])

    def test_expand_pattern_expands_home(self):
        """«~» в шаблоне цели раскрывается: иначе путь не найдётся."""
        home = self.tmp.name
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            os.makedirs(os.path.join(home, "daemon-logs"))
            path = os.path.join(home, "daemon-logs", "mock-7.log")
            with open(path, "wb") as fh:
                fh.write(b"x")
            self.assertEqual(logfiles.expand_pattern("~/daemon-logs/*.log"),
                             [path])
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home

    def test_discover_uses_target_globs(self):
        first = self._write("mock-1.log")
        second = self._write("mock-2.log")
        self._write("other.txt")
        self.assertEqual(logfiles.discover(self._spec()), sorted([first, second]))

    def test_discover_deduplicates(self):
        """Один файл под двумя шаблонами — один файл в плане."""
        path = self._write("mock-1.log")
        spec = self._spec(log_globs=(os.path.join(self.logs, "mock-*.log"),
                                     os.path.join(self.logs, "mock-1.log")))
        self.assertEqual(logfiles.discover(spec), [path])

    def test_discover_overrides_by_target_name(self):
        """Перекрытие оператора приходит словарём «имя цели → шаблоны»."""
        path = self._write("other-1.log")
        spec = self._spec()
        overrides = {"mock": [os.path.join(self.logs, "other-*.log")]}
        self.assertEqual(logfiles.discover(spec, overrides), [path])

    def test_discover_overrides_as_plain_list(self):
        """Ретроспектива перекрывает шаблоны одной цели — списком."""
        path = self._write("other-1.log")
        spec = self._spec()
        overrides = [os.path.join(self.logs, "other-*.log")]
        self.assertEqual(logfiles.discover(spec, overrides), [path])

    def test_discover_without_overrides_keeps_target_globs(self):
        """Пустое перекрытие — это «шаблоны цели», а не «ничего не искать»."""
        path = self._write("mock-1.log")
        self.assertEqual(logfiles.discover(self._spec(), {}), [path])

    # --- кто это демон ----------------------------------------------------

    def test_pid_of_reads_target_regex(self):
        spec = self._spec()
        self.assertEqual(logfiles.pid_of(spec, "/x/mock-4242.log"), 4242)

    def test_pid_of_unknown_without_regex(self):
        """Нет регулярки — PID неизвестен, а не «ноль» и не «нет демона»."""
        spec = self._spec(log_pid_regex="")
        self.assertIsNone(logfiles.pid_of(spec, "/x/mock-4242.log"))

    def test_pid_of_none_on_mismatch(self):
        spec = self._spec()
        self.assertIsNone(logfiles.pid_of(spec, "/x/other-4242.log"))

    def test_pid_of_none_on_broken_regex(self):
        """Негодная регулярка не должна ронять уборку."""
        spec = self._spec(log_pid_regex="([")
        self.assertIsNone(logfiles.pid_of(spec, "/x/mock-4242.log"))

    def test_pid_for_falls_back_to_file_name(self):
        """Имя файла разбирается, когда описание цели не совпало."""
        self.assertEqual(logfiles.pid_for(self._spec(), "/x/daemon-77.out.log"),
                         77)

    def test_group_of_reads_target_regex(self):
        spec = self._spec()
        self.assertEqual(logfiles.group_of(spec, "/x/mock-42.00.log"),
                         "mock-42")

    def test_group_of_unknown_without_regex(self):
        spec = self._spec(log_group_regex="")
        self.assertIsNone(logfiles.group_of(spec, "/x/mock-42.00.log"))

    def test_instance_of_prefers_group(self):
        spec = self._spec()
        self.assertEqual(logfiles.instance_of(spec, "/x/mock-42.00.log"),
                         "mock-42")

    def test_instance_of_falls_back_to_file_name(self):
        """Без регулярки группировки инстанс видно только в имени файла."""
        spec = self._spec(log_group_regex="")
        self.assertEqual(
            logfiles.instance_of(spec, "/x/kotlin-daemon.2026-10-20.08-827.00.log"),
            "827")

    def test_is_forbidden(self):
        spec = self._spec()
        self.assertTrue(logfiles.is_forbidden(spec, "mock-marker-123"))
        self.assertFalse(logfiles.is_forbidden(spec, "mock-42.00.log"))

    # --- жив ли демон -----------------------------------------------------

    def test_daemon_alive_unknown_without_pid_regex(self):
        spec = self._spec(log_pid_regex="")
        self.assertIsNone(logfiles.daemon_alive(spec, "/x/mock-4242.log"))

    def test_daemon_alive_true_for_own_pid(self):
        spec = self._spec()
        path = os.path.join(self.logs, f"mock-{os.getpid()}.log")
        self.assertTrue(logfiles.daemon_alive(spec, path))

    def test_daemon_alive_false_for_missing_pid(self):
        spec = self._spec()
        path = os.path.join(self.logs, "mock-999999.log")
        self.assertFalse(logfiles.daemon_alive(spec, path))

    def test_is_target_process_matches_cmdline(self):
        """Процесс должен быть похож на демона цели, а не просто на живой."""
        spec = self._spec(name="mock", match_cmdline="python")
        targets = reaper.TargetSet({"mock": spec})
        self.assertTrue(logfiles.is_target_process(os.getpid(), spec, targets))

    def test_is_target_process_rejects_foreign_process(self):
        spec = self._spec(name="mock", match_cmdline="definitely-not-here")
        targets = reaper.TargetSet({"mock": spec})
        self.assertFalse(logfiles.is_target_process(os.getpid(), spec, targets))

    # --- инструменты пользуются общим правилом ---------------------------

    def test_prune_uses_common_implementation(self):
        """Уборщик не должен заводить свою копию правила."""
        self.assertIs(prune.pid_of, logfiles.pid_of)
        self.assertIs(prune.is_forbidden, logfiles.is_forbidden)

    def test_prune_group_falls_back_to_path(self):
        """В уборке «файл сам себе группа»: открытый файл защищает группу."""
        kotlin = self._spec(log_group_regex="")
        self.assertEqual(prune.group_of(kotlin, "/x/mock-1.log"),
                         "/x/mock-1.log")

    def test_history_uses_common_implementation(self):
        """История и общий модуль должны понимать логи одинаково."""
        spec = self._spec()
        path = "/x/mock-42.00.log"
        self.assertEqual(history._instance_for(path, spec),
                         logfiles.instance_of(spec, path))
        self.assertEqual(history._pid_for("/x/daemon-77.out.log", spec),
                         logfiles.pid_for(spec, "/x/daemon-77.out.log"))
