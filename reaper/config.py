"""Конфигурация рейпера: значения по умолчанию и разбор файла."""

from __future__ import annotations

import os
from typing import Any

DEFAULTS: dict[str, Any] = {
    # Порог простоя, разный для Gradle и Kotlin (см. README «Пороги»).
    #
    # Gradle: 6 ч, а НЕ 3 ч. У Gradle есть собственный idle-таймаут
    # (idleTimeout=10800000 мс = 180 мин): он убивает демона сам, и ровно
    # через 180 мин простоя. Наш порог обязан быть ВЫШЕ этого таймаута,
    # иначе мы конкурируем с Gradle за одни и те же демоны — и проигрываем:
    # к моменту нашего свипа демон уже мёртв.
    #
    # Почему не ниже (проверено ретроспективно по 8291 простою между сборками):
    #   60 мин → 88.5 % ложных срабатываний (демон понадобился снова)
    #   90 мин → 92.9 %
    #  120 мин → 100 %
    #   180 мин → 0 (но там уже работает таймаут Gradle)
    # При 6 ч остаются только «зависшие» демоны (клиент отвалился во время
    # сборки — Gradle не может их убрать): 3 из 893 за всю историю, и все
    # три — именно такие.
    "IDLE_SECONDS": 21600,        # 6 ч — порог простоя (Gradle)
    # Kotlin: своего idle-таймаута у Kotlin-демона НЕТ (он умирает только
    # по «No more clients left»). Значит, в отличие от Gradle, его никто
    # не уберёт — это и есть настоящая цель рейпера. Порог 2 ч: между
    # сессиями компиляции у Kotlin p99.9 = 100 мин, максимум 1.7 ч, то есть
    # 2 ч — заведомо выше «живого» простоя.
    "KOTLIN_IDLE_SECONDS": 7200,   # 2 ч — порог простоя (Kotlin)
    # Через сколько после отмены сборки считать демон «зависшим».
    # Отдельный порог, потому что это ДРУГАЯ категория: демон, у которого
    # клиент отвалился во время сборки, работу продолжать некому и
    # некому — а Gradle его не убирает (его idle-таймаут не действует,
    # пока демон «в сборке»). Таких демонов не жалко держать часами.
    "STUCK_AFTER_SECONDS": 3600,  # 1 ч
    # Защита Kotlin-демонов действует, только пока сборка Gradle идёт
    # «по-настоящему» (началась недавно). Сборка, начавшаяся несколько часов
    # назад и не завершившаяся, — это зависший демон, а не идущая работа;
    # иначе один зависший Gradle-демон блокировал бы уборку Kotlin-навсегда.
    "GRADLE_BUILD_ACTIVE_SECONDS": 1800,  # 30 мин
    "MIN_FOOTPRINT_MB": 128,      # RSS+swap ниже — не жалко
    "BUSY_CPU_SECONDS": 3.0,      # 3 с CPU за свип = «занят»
    "SWEEP_SECONDS": 300,         # интервал свипа
    "GRACE_SECONDS": 15,          # SIGTERM → пауза → SIGKILL
    "GRADLE_SIGKILL_AFTER_TRIES": 3,
    # Окно «понадобился снова». 6 ч, а не 2 ч: измерено, что демон
    # возвращается с лагом p90 = 6.4 ч. При окне 2 ч мы считали бы
    # ложными только 61 % возвратов, то есть занижали бы долю
    # ложных срабатываний почти вдвое.
    "REUSE_WINDOW_SECONDS": 21600,  # 6 ч
    # Критерий перехода из dry-run в kill-режим.
    # Порог событий снижен с 15 до 8: при новых порогах простоя события
    # Gradle-стороны редки ПО ЗАМЫСЛУ (3 из 893 демонов — «зависшие»),
    # и основной вклад даёт Kotlin. 8 событий — тот же минимум, отсекающий
    # вердикт по 1–2 случаям, но достижимый за 2–3 сут пилота.
    "PILOT_MIN_EVENTS": 8,
    "PILOT_MAX_REUSE_RATE": 0.60,
    # Детекция деградации: если измерить удаётся меньше половины демонов
    # (все ушли в «неизвестная история» / «нет baseline»), рейпер, скорее
    # всего, слеп — и молчит, а выглядит здоровым. Раньше это замечалось
    # только при нуле демонов вообще.
    "DEGRADATION_MIN_MEASURED_RATIO": 0.5,
    # Падение числа измеренных демонов относительно прошлого свипа.
    "DEGRADATION_MIN_COUNT_RATIO": 0.5,
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

# Версия правил. Меняется, когда меняется смысл порогов: чтобы при смене
# правил сбросить накопленную метрику пилота (она измеряет ДРУГОЕ, если
# правила другие, и смешивать их нельзя).
RULES_VERSION = 3


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def load_config(path: str | None,
                warnings: list[str] | None = None) -> dict[str, Any]:
    """Читает KEY=VALUE-конфиг. Неизвестные ключи игнорируются, битые значения
    не ломают работу: остаётся значение по умолчанию.

    `warnings` — список, в который добавляются сообщения о проблемах
    (битое значение, неизвестный ключ). Раньше оба случая проходили молча:
    оператор думал, что настроил порог, а действовал дефолт — и это
    выглядело как «рейпер сломался», а не как «опечатка в конфиге».
    """
    cfg = dict(DEFAULTS)
    cfg_path = expand(path or DEFAULTS["CONFIG_PATH"])
    if os.path.isfile(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            for number, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.split("#", 1)[0].strip()
                if key not in DEFAULTS:
                    if warnings is not None:
                        warnings.append(f"конфиг: неизвестный ключ {key!r} "
                                        f"(строка {number}) — проигнорирован")
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
                    if warnings is not None:
                        warnings.append(f"конфиг: значение {key!r} = {value!r} "
                                        f"не подходит — взят дефолт "
                                        f"{default!r} (строка {number})")
    return cfg


def idle_limit(cfg: dict[str, Any], kind: str) -> float:
    """Порог простоя для класса демона (секунды).

    У Gradle и Kotlin разные правила жизни, поэтому разные и пороги:
    у Gradle есть собственный idle-таймаут (180 мин) — наш порог обязан быть
    выше него; у Kotlin такого таймаута нет вовсе, его не убирает никто, и
    2 ч — заведомо выше «живого» простоя (p99.9 = 100 мин).
    """
    if kind == "kotlin":
        return float(cfg.get("KOTLIN_IDLE_SECONDS", cfg["IDLE_SECONDS"]))
    return float(cfg["IDLE_SECONDS"])


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
