#!/usr/bin/env python3
"""Сквозные тесты kill-пути на поддельных демонах.

Реальные Gradle/Kotlin-демоны машины не затрагиваются: цели создаются
здесь, как процессы с поддельным cmdline и поддельным логом, а рейпер
запускается с PID_ALLOWLIST, ограниченным этими целями.

Запуск: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import importlib.machinery
import json
import importlib.util
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "java-reaper")


sys.path.insert(0, HERE)

import _api as reaper  # noqa: E402

DEFAULTS = reaper.DEFAULTS
Daemon = reaper.Daemon
Decision = reaper.Decision
execute = reaper.execute
read_proc_stat = reaper.read_proc_stat

GRADLE_MAIN = "org.gradle.launcher.daemon.bootstrap.GradleDaemon"
KOTLIN_MAIN = "org.jetbrains.kotlin.daemon.KotlinCompileDaemon"

FAKE_SRC = '''
import os, signal, sys, time
log_path = sys.argv[1]
mode = sys.argv[2]
kind = sys.argv[3]
stamp = time.time() - 4 * 3600          # «давно простаивает»
def line(ts, text):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) + ".000+0300 " + text + "\\n"
fh = open(log_path, "w")
if kind == "gradle":
    fh.write(line(stamp, "[INFO] Received command: Build{id=1}"))
    fh.write(line(stamp + 1, "[INFO] Marking the daemon as idle, address: [x]"))
else:
    fh.write("kotlin daemon log\\n")
fh.flush()
os.utime(log_path, (stamp, stamp))
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.2)
'''


class FakeDaemon:
    """Процесс, который рейпер принимает за Gradle/Kotlin-демон."""

    def __init__(self, kind: str, mode: str = "normal", tmpdir: str | None = None):
        self.tmpdir = tmpdir or tempfile.mkdtemp(prefix="reaper-fake-")
        os.makedirs(self.tmpdir, exist_ok=True)
        src = os.path.join(self.tmpdir, "fake.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(FAKE_SRC)
        if kind == "gradle":
            argv0 = "java -cp x " + GRADLE_MAIN
        else:
            argv0 = "java -cp x " + KOTLIN_MAIN
        self.log_dir = os.path.join(self.tmpdir, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        # Имя лога должно совпасть с тем, что ищет рейпер.
        placeholder = os.path.join(self.log_dir, "daemon-0.out.log")
        if kind == "kotlin":
            placeholder = os.path.join(self.log_dir, "kotlin-daemon.fake.log")
        self.proc = subprocess.Popen(
            [argv0, src, placeholder, mode, kind],
            executable=sys.executable,   # argv[0] — поддельная «java», тело — python
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Переименуем лог под реальный PID после старта.
        time.sleep(0.6)
        if kind == "gradle":
            final = os.path.join(self.log_dir, f"daemon-{self.proc.pid}.out.log")
        else:
            final = os.path.join(self.log_dir, "kotlin-daemon.fake.log")
        try:
            os.replace(placeholder, final)
        except OSError:
            pass
        # Переоткрываем с правильным именем: процесс держит уже открытый fd,
        # поэтому достаточно переименовать файл — fd следует за файлом.
        self.log_path = final

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.alive():
            self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def run_reaper(cfg_path: str, *args: str) -> subprocess.CompletedProcess:
    # --targets указывает на несуществующий файл: тест не должен зависеть от
    # того, что лежит в ~/.config/java-reaper/targets.yaml на машине.
    return subprocess.run([sys.executable, SCRIPT, "--config", cfg_path,
                           "--targets", cfg_path + ".targets", *args],
                          capture_output=True, text=True, timeout=120)


class TestKillPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reaper-kill-")
        self.state = os.path.join(self.tmp, "state")
        self.cfg = os.path.join(self.tmp, "config")
        self.fakes: list[FakeDaemon] = []

    def tearDown(self):
        for fake in self.fakes:
            fake.stop()

    def write_cfg(self, allow: str, **extra: str) -> str:
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
            f"PID_ALLOWLIST={allow}",
        ]
        for key, value in extra.items():
            lines.append(f"{key}={value}")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return self.cfg

    def fake(self, kind: str, mode: str = "normal") -> FakeDaemon:
        fake = FakeDaemon(kind, mode, tmpdir=os.path.join(self.tmp, kind + mode))
        os.makedirs(fake.tmpdir, exist_ok=True)
        self.fakes.append(fake)
        return fake

    # ------------------------------------------------------------------ #

    def test_gradle_daemon_gets_sigterm_and_exits(self):
        fake = self.fake("gradle")
        self.write_cfg(str(fake.proc.pid))
        run_reaper(self.cfg, "--mode=dry-run")   # первый свип: baseline
        proc = run_reaper(self.cfg, "--mode=kill")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SIGTERM", proc.stdout)
        deadline = time.time() + 10
        while time.time() < deadline and fake.alive():
            time.sleep(0.2)
        self.assertFalse(fake.alive(), "фейковый Gradle-демон должен был выйти")

    def test_stubborn_gradle_daemon_is_escalated_to_sigkill(self):
        fake = self.fake("gradle", mode="stubborn")
        self.write_cfg(str(fake.proc.pid), GRADLE_SIGKILL_AFTER_TRIES="1")
        run_reaper(self.cfg, "--mode=dry-run")
        proc = run_reaper(self.cfg, "--mode=kill")
        self.assertIn("SIGTERM", proc.stdout)
        self.assertIn("SIGKILL", proc.stdout)
        deadline = time.time() + 10
        while time.time() < deadline and fake.alive():
            time.sleep(0.2)
        self.assertFalse(fake.alive(), "фейковый Gradle-демон должен был быть убит")

    def test_stubborn_kotlin_daemon_is_sigkilled_after_grace(self):
        """Kotlin-демон, игнорирующий SIGTERM, получает SIGKILL.

        Через CLI результат зависит от машины: если где-то идёт свежая
        сборка Gradle, рейпер по правилу «сборка Gradle ⇒ Kotlin не трогаю»
        защищает цель — и это правильный исход (компилятор нужен сборке).
        Поэтому проверяем оба допустимых исхода, а сам механизм эскалации —
        детерминированно, прямым вызовом execute() (см. следующий тест).
        """
        fake = self.fake("kotlin", mode="stubborn")
        # Защита «идёт сборка Gradle» выключена: иначе исход зависел бы от
        # того, собирает ли что-то на машине в этот момент.
        self.write_cfg(str(fake.proc.pid), GRADLE_BUILD_ACTIVE_SECONDS=0)
        run_reaper(self.cfg, "--mode=dry-run")
        proc = run_reaper(self.cfg, "--mode=kill")
        if "идёт сборка Gradle" in proc.stdout:
            # Цель защищена правилом: демон остался жив — это допустимый
            # исход (компилятор нужен идущей сборке).
            self.assertTrue(fake.alive())
            return
        self.assertIn("SIGTERM", proc.stdout)
        self.assertIn("SIGKILL", proc.stdout)
        deadline = time.time() + 10
        while time.time() < deadline and fake.alive():
            time.sleep(0.2)
        self.assertFalse(fake.alive(), "фейковый Kotlin-демон должен был быть убит")

    def test_execute_escalates_kotlin_to_sigkill(self):
        """Прямая проверка эскалации Kotlin: SIGTERM → пауза → SIGKILL."""
        fake = self.fake("kotlin", mode="stubborn")
        cfg = dict(DEFAULTS)
        cfg["GRACE_SECONDS"] = 1
        stat = read_proc_stat(fake.proc.pid)
        daemon = Daemon(pid=fake.proc.pid, kind="kotlin", starttime=stat["starttime"],
                        cpu_ticks=0, rss_mb=256, swap_mb=0)
        report = execute([Decision(daemon, True, "тест")], {}, time.time(), cfg,
                         dry_run=False)
        actions = [r["action"] for r in report]
        self.assertIn("SIGTERM", actions)
        self.assertIn("SIGKILL", actions)
        deadline = time.time() + 10
        while time.time() < deadline and fake.alive():
            time.sleep(0.2)
        self.assertFalse(fake.alive())

    def test_dry_run_never_signals(self):
        fake = self.fake("gradle", mode="stubborn")
        self.write_cfg(str(fake.proc.pid))
        run_reaper(self.cfg, "--mode=dry-run")
        proc = run_reaper(self.cfg, "--mode=dry-run")
        self.assertNotIn("SIGTERM", proc.stdout)
        self.assertNotIn("SIGKILL", proc.stdout)
        self.assertTrue(fake.alive())

    def test_pid_outside_allowlist_is_untouched(self):
        fake = self.fake("gradle", mode="stubborn")
        self.write_cfg("999999")
        run_reaper(self.cfg, "--mode=dry-run")
        proc = run_reaper(self.cfg, "--mode=kill")
        self.assertNotIn("SIGTERM", proc.stdout)
        self.assertTrue(fake.alive())

    def test_kill_is_logged_to_jsonl(self):
        """Фактическое убийство попадает в журнал kills.jsonl.

        Журнал — это аудит: «кого рейпер убил и почему». Без него
        нельзя ни подтвердить эффект, ни разобрать инцидент.
        """
        fake = self.fake("gradle")
        self.write_cfg(str(fake.proc.pid))
        run_reaper(self.cfg, "--mode=dry-run")
        proc = run_reaper(self.cfg, "--mode=kill")
        self.assertIn("SIGTERM", proc.stdout)

        kills_path = os.path.join(self.state, "kills.jsonl")
        self.assertTrue(os.path.exists(kills_path),
                        "журнал убийств не создан")
        with open(kills_path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(rows, "журнал убийств пуст")
        row = rows[-1]
        self.assertEqual(row["pid"], fake.proc.pid)
        self.assertIn(row["action"], ("SIGTERM", "SIGKILL"))
        self.assertTrue(row.get("reason"), "в записи нет причины")
        self.assertIn("at", row)
        self.assertGreater(row["at"], 0)
        self.assertEqual(row["kind"], "gradle")

    def test_dry_run_writes_no_kills(self):
        """В dry-run журнал убийств остаётся пустым."""
        fake = self.fake("gradle")
        self.write_cfg(str(fake.proc.pid))
        run_reaper(self.cfg, "--mode=dry-run")
        run_reaper(self.cfg, "--mode=dry-run")
        kills_path = os.path.join(self.state, "kills.jsonl")
        if os.path.exists(kills_path):
            with open(kills_path, encoding="utf-8") as fh:
                self.assertEqual(fh.read().strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
