#!/usr/bin/env python3
"""Сторож рейпера: работает вне user-менеджера (через crond).

Зачем он нужен: свипы запускает systemd --user, а user-менеджер на этой
машине уже зависал (busy-loop, 96 % ядра). Пока менеджер жив — работает
таймер; если он умрёт — таймер тоже умрёт, и демоны будут копиться
незаметно, потому что «молчание» рейпера ничем не заметно.

Сторож делает две вещи:

  1. Проверяет, что свипы идут: последний свип должен быть не старше
     двух интервалов. Иначе — СТАТУС: ПОЛОМАНО.
  2. Если свипов нет — выполняет свип сам (прямой вызов рейпера). Это
     запасной путь, не зависящий ни от systemd, ни от linger.

Обе функции безопасны: рейпер защищён flock от наложения, аварийный
тормоз (~/.config/java-reaper/disabled) действует и здесь.

Запуск (cron, раз в 5 минут):

    */5 * * * * /usr/bin/python3 %h/dev/java-reaper/tools/watchdog.py >> %h/.local/state/java-reaper/watchdog.log 2>&1

Код возврата: 0 — всё в порядке (или сторож сам всё починил),
1 — свипов нет и починить не удалось, 2 — рейпер отключён флагом.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from reaper.config import DEFAULTS, disabled_flag, load_config  # noqa: E402
from reaper.state import load_state  # noqa: E402

# Сколько свипов подряд не видно — повод бить тревогу.
STALE_SWEEPS = 2

# Ключи конфигу, которые сторож передаёт рейперу в запасном свипе.
FORWARD_KEYS = ("STATE_DIR", "LOCK_PATH", "DISABLED_FLAG", "IDLE_SECONDS",
                "MIN_FOOTPRINT_MB", "BUSY_CPU_SECONDS", "SWEEP_SECONDS",
                "GRACE_SECONDS", "GRADLE_SIGKILL_AFTER_TRIES",
                "REUSE_WINDOW_SECONDS", "PID_ALLOWLIST", "LOG_SCAN_MAX_BYTES",
                "MAX_KILLS_PER_SWEEP", "PEER_MAX_AGE_SECONDS",
                "TRIM_IDLE_LOGS", "TRIM_AFTER_SECONDS")


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def last_sweep_age(state: dict) -> float | None:
    last = float(state.get("last_sweep_at", 0.0) or 0.0)
    if not last:
        return None
    return time.time() - last


def write_config(cfg: dict) -> str:
    """Записывает конфиг во временный файл — чтобы запасной свип работал
    ровно с теми же настройками, что и основной."""
    fd, path = tempfile.mkstemp(prefix="java-reaper-watchdog-", suffix=".conf")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for key in FORWARD_KEYS:
            if key in cfg:
                fh.write(f"{key} = {cfg[key]}\n")
    return path


def run_sweep(cfg: dict) -> tuple[bool, str]:
    """Запасной свип: запускаем рейпер напрямую."""
    script = os.path.join(ROOT, "java-reaper")
    config_path = write_config(cfg)
    try:
        proc = subprocess.run(
            [sys.executable, script, "--mode=dry-run", "--config", config_path],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "HOME": os.path.expanduser("~")})
    except subprocess.TimeoutExpired:
        return False, "запасной свип не завершился за 120 с"
    except OSError as exc:
        return False, f"не удалось запустить рейпер: {exc}"
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else f"код возврата {proc.returncode}"
        return False, f"запасной свип упал: {detail}"
    return True, "запасной свип выполнен"


def main_with_config(cfg: dict) -> int:
    """Основная логика сторожа с явным конфигом (тестируется изолированно)."""
    flag = disabled_flag(cfg)
    if os.path.exists(flag):
        log(f"СТАТУС: ОТКЛЮЧЁН флагом {flag} — сторож молчит")
        return 2

    state = load_state(cfg)
    sweep = float(cfg["SWEEP_SECONDS"])
    age = last_sweep_age(state)
    limit = sweep * STALE_SWEEPS

    if age is not None and age <= limit:
        log(f"СТАТУС: ок, последний свип {age / 60:.1f} мин назад")
        return 0

    why = ("свипов не было ни разу" if age is None else
           f"последний свип {age / 60:.1f} мин назад (порог {limit / 60:.0f} мин)")
    log(f"СТАТУС: ПОЛОМАНО — {why}")

    ok, detail = run_sweep(cfg)
    if ok:
        log(f"починил: {detail}")
        return 0
    log(f"починить не удалось: {detail}")
    return 1


def main() -> int:
    cfg = load_config(None)
    return main_with_config(cfg)


if __name__ == "__main__":
    sys.exit(main())
