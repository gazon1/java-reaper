#!/usr/bin/env python3
"""Сквозной тест: новая программа, описанная только в targets.yaml.

Проверяем главное обещание обобщения: чтобы добавить программу, не нужно
править код. Описание цели в YAML → процесс опознан → лог найден через
/proc/PID/fd → маркеры разобраны → решение принято → не пересоздаваемая
цель не убита.

Процесс фейковый (как в test_kill_path.py): настоящий процесс с поддельным
cmdline, свой лог, настоящий /proc. Реальные демоны машины не затрагиваются:
рейпер запускается с PID_ALLOWLIST, ограниченным фейком.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "java-reaper")

# Имя класса, по которому цель узнаёт свой демон в cmdline.
MOCK_MAIN = "mock.daemon.Bootstrap"

# Маркеры работы фейкового демона — свои, не Gradle-овские: у новой
# программы свой формат лога, и цель описывает его сама.
MARK_START = "MOCK: build started"
MARK_END = "MOCK: build finished"

FAKE_SRC = '''
import os, sys, time
log_path = sys.argv[1]
stamp = time.time() - 4 * 3600          # «давно простаивает»
def line(ts, text):
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
            + ".000+0300 " + text + "\\n")
fh = open(log_path, "a", buffering=1)
fh.write(line(stamp, "[INFO] MOCK: build started"))
fh.write(line(stamp + 60, "[INFO] MOCK: build finished"))
fh.flush()
os.utime(log_path, (stamp, stamp))
while True:
    time.sleep(0.2)
'''


class FakeTargetDaemon:
    """Процесс, который рейпер принимает за демон новой цели."""

    def __init__(self, tmpdir: str):
        os.makedirs(tmpdir, exist_ok=True)
        src = os.path.join(tmpdir, "fake.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(FAKE_SRC)
        self.log_dir = os.path.join(tmpdir, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_path = os.path.join(self.log_dir, "mock-daemon.out.log")
        # argv[0] — поддельная «java», тело — python: так cmdline процесса
        # выглядит как настоящий демон, а работает он без JVM.
        argv0 = "java -cp x " + MOCK_MAIN
        self.proc = subprocess.Popen(
            [argv0, src, self.log_path],
            executable=sys.executable,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_for_log()

    def _wait_for_log(self, timeout: float = 10.0) -> None:
        """Ждём, пока демон откроет свой лог (иначе лог не найти через /proc)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.log_path) and self._log_is_open():
                return
            time.sleep(0.1)
        raise AssertionError("фейковый демон не открыл свой лог")

    def _log_is_open(self) -> bool:
        fddir = f"/proc/{self.proc.pid}/fd"
        try:
            fds = os.listdir(fddir)
        except OSError:
            return False
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fddir, fd))
            except OSError:
                continue
            if os.path.basename(target) == os.path.basename(self.log_path):
                return True
        return False

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.alive():
            self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


class TestNewTargetEndToEnd(unittest.TestCase):
    """Новая цель в YAML: от процесса до решения рейпера."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reaper-newtarget-")
        self.state = os.path.join(self.tmp, "state")
        self.cfg = os.path.join(self.tmp, "config")
        self.targets = os.path.join(self.tmp, "targets.yaml")
        self.fake = FakeTargetDaemon(os.path.join(self.tmp, "fake"))
        self.write_config()
        self.write_targets(rebuildable=True)

    def tearDown(self):
        self.fake.stop()

    # ------------------------------------------------------------------ #

    def write_config(self, **extra: str) -> None:
        lines = [
            "IDLE_SECONDS=60",
            "MIN_FOOTPRINT_MB=0",
            "BUSY_CPU_SECONDS=1000",
            "GRACE_SECONDS=1",
            # Память: «всегда убирать» — иначе исход теста зависел бы от
            # того, сколько памяти свободно на машине, где он идёт.
            "MEMORY_PRESSURE_MB=0",
            f"STATE_DIR={self.state}",
            f"LOCK_PATH={os.path.join(self.tmp, 'lock')}",
            f"PID_ALLOWLIST={self.fake.proc.pid}",
        ]
        for key, value in extra.items():
            lines.append(f"{key}={value}")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def write_targets(self, rebuildable: bool | None = True,
                      idle_seconds: int | None = None) -> None:
        """Описание новой цели в YAML — как это сделал бы пользователь."""
        lines = [
            "targets:",
            "  - name: mock",
            "    label: Mock",
            f"    match_cmdline: '{MOCK_MAIN}'",
            "    log_name_regex: '^mock-.*\\.out\\.log$'",
            "    activity: markers",
            "    ts_format: iso",
            f"    build_start: '{MARK_START}'",
            f"    build_end: '{MARK_END}'",
            "    log_globs:",
            f"      - '{self.fake.log_path}'",
        ]
        if rebuildable is not None:
            lines.append(f"    rebuildable: {str(rebuildable).lower()}")
        if idle_seconds is not None:
            lines.append(f"    idle_seconds: {idle_seconds}")
        with open(self.targets, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def run_reaper(self, *args: str) -> dict:
        """Прогон рейпера (dry-run) и разбор JSON-отчёта."""
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--config", self.cfg,
             "--targets", self.targets, "--mode=dry-run", "--json", *args],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def decision_for_fake(self, payload: dict) -> dict:
        """Решение рейпера по нашему фейковому демону."""
        for item in payload.get("daemons", []):
            if int(item.get("pid", -1)) == self.fake.proc.pid:
                return item
        self.fail(f"решения по pid={self.fake.proc.pid} нет: {payload}")

    # ------------------------------------------------------------------ #

    def test_new_kind_is_detected_with_its_own_log(self):
        """Новая цель опознаётся, её лог найден, маркеры разобраны."""
        self.run_reaper()          # первый свип: baseline
        payload = self.run_reaper()
        decision = self.decision_for_fake(payload)
        self.assertEqual(decision.get("kind"), "mock")
        self.assertEqual(decision.get("log_path"), self.fake.log_path)
        # Простой 4 ч при пороге 1 ч — демон кандидат на уборку.
        self.assertIn("простой", decision.get("reason", ""))

    def test_rebuildable_target_becomes_candidate(self):
        """Пересоздаваемая цель после двух свипов попадает под нож."""
        self.run_reaper()          # первый свип: baseline
        payload = self.run_reaper()  # второй свип: решение
        decision = self.decision_for_fake(payload)
        self.assertEqual(decision.get("action"), "kill",
                         f"ожидалось решение об уборке: {decision}")

    def test_not_rebuildable_target_is_never_killed(self):
        """Непересоздаваемая цель не убивается никогда — даже в kill-режиме."""
        self.write_targets(rebuildable=False)
        self.run_reaper()
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--config", self.cfg,
             "--targets", self.targets, "--mode=kill", "--json"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        decision = self.decision_for_fake(payload)
        self.assertNotEqual(decision.get("action"), "kill",
                            f"непересоздаваемую цель убивать нельзя: {decision}")
        self.assertIn("пересоздаваем", decision.get("reason", ""))
        self.assertTrue(self.fake.alive(),
                        "фейковый демон должен был остаться жив")

    def test_rebuildable_defaults_to_false(self):
        """Если пересоздаваемость не описана — цель не трогаем.

        «Не знаем ⇒ не убиваем»: цена ошибки тут не «холодный старт»,
        а потерянная работа, и по умолчанию выбирается безопасный вариант.
        """
        self.write_targets(rebuildable=None)
        self.run_reaper()
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--config", self.cfg,
             "--targets", self.targets, "--mode=kill", "--json"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        decision = self.decision_for_fake(payload)
        self.assertNotEqual(decision.get("action"), "kill",
                            f"цель без описания пересоздаваемости убивать нельзя: "
                            f"{decision}")
        self.assertTrue(self.fake.alive())

    def test_idle_threshold_of_new_target_is_respected(self):
        """Порог простоя цели работает: при большем пороге демон не трогается."""
        self.write_targets(rebuildable=True, idle_seconds=86400)
        self.run_reaper()
        payload = self.run_reaper()
        decision = self.decision_for_fake(payload)
        self.assertNotEqual(decision.get("action"), "kill",
                            f"при пороге сутки демон простаивает 4 ч — не кандидат: "
                            f"{decision}")
        self.assertIn("простой", decision.get("reason", ""))

    def test_target_without_own_log_is_protected(self):
        """Цель, у которой свой лог не найден, защищается (нечем мерить простой)."""
        with open(self.targets, "w", encoding="utf-8") as fh:
            fh.write(
                "targets:\n"
                "  - name: mock\n"
                "    label: Mock\n"
                f"    match_cmdline: '{MOCK_MAIN}'\n"
                "    log_name_regex: '^does-not-exist-.*\\.log$'\n"
                "    activity: markers\n"
                "    rebuildable: true\n"
            )
        self.run_reaper()
        payload = self.run_reaper()
        decision = self.decision_for_fake(payload)
        self.assertNotEqual(decision.get("action"), "kill",
                            f"без лога простой не измерить — демон защищён: {decision}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
