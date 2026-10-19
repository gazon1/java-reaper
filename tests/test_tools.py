#!/usr/bin/env python3
"""Тесты сторожа (п.10) и месячного отчёта (п.11).

Сторож и отчёт — вспомогательные инструменты, но именно они замечают
«рейпер молча сломался» и «рейпер работает, но вредит». Их поведение
проверяется на изолированном state-dir, без касания живой системы.
"""

from __future__ import annotations

import importlib.util
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


def _load_tool(name: str, filename: str):
    """Загружает инструмент из tools/ как модуль."""
    path = os.path.join(ROOT, "tools", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watchdog = _load_tool("reaper_watchdog", "watchdog.py")
monthly = _load_tool("reaper_monthly", "monthly-report.py")


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


if __name__ == "__main__":
    unittest.main()
