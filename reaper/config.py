"""Конфигурация рейпера: значения по умолчанию и разбор файла."""

from __future__ import annotations

import os
from typing import Any

DEFAULTS: dict[str, Any] = {
    "IDLE_SECONDS": 10800,        # 3 ч — порог простоя
    "MIN_FOOTPRINT_MB": 128,      # RSS+swap ниже — не жалко
    "BUSY_CPU_SECONDS": 3.0,      # 3 с CPU за свип = «занят»
    "SWEEP_SECONDS": 300,         # интервал свипа
    "GRACE_SECONDS": 15,          # SIGTERM → пауза → SIGKILL
    "GRADLE_SIGKILL_AFTER_TRIES": 3,
    "REUSE_WINDOW_SECONDS": 7200,  # окно «понадобился снова» для метрики
    "PID_ALLOWLIST": "",          # пусто = все; список PID через запятую
    "LOG_SCAN_MAX_BYTES": 512 * 1024 * 1024,  # предел полного разбора лога
    "MAX_KILLS_PER_SWEEP": 3,     # страховка от массовой ошибки правил
    "PEER_MAX_AGE_SECONDS": 21600,  # 6 ч — после этого «клиент» считается зависшим
    "TRIM_IDLE_LOGS": 0,      # 1 = усекать логи простаивающих демонов (запись в чужие файлы)
    "TRIM_AFTER_SECONDS": 3600,  # усекать лог демона, простаивающего дольше часа
    "STATE_DIR": "~/.local/state/java-reaper",
    "CONFIG_PATH": "~/.config/java-reaper/config",
    "LOCK_PATH": "~/.local/state/java-reaper/reaper.lock",
    "DISABLED_FLAG": "~/.config/java-reaper/disabled",
}


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def load_config(path: str | None) -> dict[str, Any]:
    """Читает KEY=VALUE-конфиг. Неизвестные ключи игнорируются, битые значения
    не ломают работу: остаётся значение по умолчанию."""
    cfg = dict(DEFAULTS)
    cfg_path = expand(path or DEFAULTS["CONFIG_PATH"])
    if os.path.isfile(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.split("#", 1)[0].strip()
                if key not in DEFAULTS:
                    continue
                default = DEFAULTS[key]
                try:
                    if isinstance(default, bool):
                        cfg[key] = value.lower() in ("1", "true", "yes")
                    elif isinstance(default, int):
                        cfg[key] = int(value)
                    elif isinstance(default, float):
                        cfg[key] = float(value)
                    else:
                        cfg[key] = value
                except (TypeError, ValueError):
                    pass
    return cfg


def allowlist(cfg: dict[str, Any]) -> set[int]:
    """PID'ы, которым разрешено убивать. Пусто — разрешены все."""
    raw = str(cfg.get("PID_ALLOWLIST") or "")
    out: set[int] = set()
    for token in raw.replace(",", " ").split():
        try:
            out.add(int(token))
        except ValueError:
            continue
    return out


def disabled_flag(cfg: dict[str, Any]) -> str:
    return expand(str(cfg.get("DISABLED_FLAG") or DEFAULTS["DISABLED_FLAG"]))
