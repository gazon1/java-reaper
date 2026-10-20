"""Конфигурация рейпера: значения по умолчанию и разбор файла."""

from __future__ import annotations

import os
from typing import Any

from .targets import TargetSet, TargetSpec

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
    # Общее окно «свежей сборки» — для видов, которых нет в плоских ключах
    # (то есть для новых целей, описанных в targets.yaml).
    "BUILD_ACTIVE_SECONDS": 1800,
    # Давление по памяти: сколько свободной памяти (МБ) считается «мало».
    # Ниже этого порога машина нуждается в памяти, и простаивающие демоны
    # подлежат уборке. Выше — памяти хватает, и рейпер не трогает демонов
    # поменьше KILL_LARGE_DAEMON_MB (зачем отдавать память, которой хватает).
    # 0 = «всегда убирать» (режим безусловной чистки).
    "MEMORY_PRESSURE_MB": 4096,
    # Демон крупнее этого (МБ) убирается в любом случае: столько памяти
    # жалко держать простаивающим, даже когда система не напряжена.
    "KILL_LARGE_DAEMON_MB": 4096,
    # Цена ошибки: сколько секунд стоит воссоздать демона (холодный старт).
    # Измерено: Gradle 41.6 с против 2.4 с тёплого. Kotlin дешёв: это просто
    # старт JVM, десятки секунд на компиляцию. Значение влияет на порядок
    # уборки: сначала те, чьё пересоздание дешевле.
    "GRADLE_RECREATE_COST_SECONDS": 40,
    "KOTLIN_RECREATE_COST_SECONDS": 5,
    # Общая цена воссоздания — для видов демонов, которых нет в плоских
    # ключах (то есть для новых целей, описанных в targets.yaml).
    "RECREATE_COST_SECONDS": 40,
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
#
# 4 — 2026-10-20: появилось правило «memory-not-needed» (уборка зависит от
#     того, нужна ли машине память), а защита «идёт сборка Gradle» стала
#     действовать только по свежей сборке (GRADLE_BUILD_ACTIVE_SECONDS).
#     Обе правки меняют состав кандидатов, поэтому метрика пилота,
#     набранная по правилам версии 3, обнуляется.
RULES_VERSION = 4


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def load_config(path: str | None,
                warnings: list[str] | None = None,
                explicit: set[str] | None = None) -> dict[str, Any]:
    """Читает KEY=VALUE-конфиг. Неизвестные ключи игнорируются, битые значения
    не ломают работу: остаётся значение по умолчанию.

    `warnings` — список, в который добавляются сообщения о проблемах
    (битое значение, неизвестный ключ). Раньше оба случая проходили молча:
    оператор думал, что настроил порог, а действовал дефолт — и это
    выглядело как «рейпер сломался», а не как «опечатка в конфиге».

    `explicit` — множество, в которое попадают ключи, реально написанные в
    файле. Оно нужно, чтобы отличить «оператор задал значение» от «это
    просто значение по умолчанию»: плоский ключ, заданный явно, должен
    перекрывать значение из targets.yaml (и мы предупреждаем об этом),
    а ненаписанный — не должен.
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
                else:
                    if explicit is not None:
                        explicit.add(key)
    return cfg


# Плоские ключи конфига, через которые задавались пороги ДО появления
# targets.yaml.
#
# Ключи делятся на два класса, и от этого зависит, кто главнее:
#
#   * ПЛОСКИЕ ВИДОВЫЕ (KOTLIN_IDLE_SECONDS, GRADLE_RECREATE_COST_SECONDS,
#     GRADLE_BUILD_ACTIVE_SECONDS) — относятся к конкретному виду и
#     достались из прежней настройки. Если такой ключ задан в файле конфига
#     ЯВНО, он главнее значения из targets.yaml: оператор правил его,
#     значит это и есть его намерение.
#
#   * ПЛОСКИЕ ОБЩИЕ (IDLE_SECONDS, STUCK_AFTER_SECONDS, MIN_FOOTPRINT_MB,
#     KILL_LARGE_DAEMON_MB и новые) — относятся ко всем видам сразу. Значение
#     для конкретного вида в targets.yaml СПЕЦИФИЧНЕЕ, поэтому оно главнее;
#     общий ключ работает там, где в YAML значения нет. Если общий ключ
#     задан явно и расходится с YAML — рейпер предупреждает: молчать об
#     этом нельзя, иначе «опечатка в конфиге» выглядит как «рейпер сломался».
LEGACY_IDLE_KEYS = {"kotlin": "KOTLIN_IDLE_SECONDS"}
LEGACY_COST_KEYS = {
    "gradle": "GRADLE_RECREATE_COST_SECONDS",
    "kotlin": "KOTLIN_RECREATE_COST_SECONDS",
}
LEGACY_BUILD_ACTIVE_KEYS = {"gradle": "GRADLE_BUILD_ACTIVE_SECONDS"}

# (поле спецификации, общий плоский ключ, плоские ключи по видам)
THRESHOLD_KEYS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("idle_seconds", "IDLE_SECONDS", LEGACY_IDLE_KEYS),
    ("recreate_cost_seconds", "RECREATE_COST_SECONDS", LEGACY_COST_KEYS),
    ("build_active_seconds", "BUILD_ACTIVE_SECONDS", LEGACY_BUILD_ACTIVE_KEYS),
    ("stuck_after_seconds", "STUCK_AFTER_SECONDS", {}),
    ("min_footprint_mb", "MIN_FOOTPRINT_MB", {}),
    ("large_daemon_mb", "KILL_LARGE_DAEMON_MB", {}),
)


def legacy_idle_key(kind: str) -> str:
    """Плоский ключ конфига с порогом простоя для данного вида."""
    return LEGACY_IDLE_KEYS.get(kind, "IDLE_SECONDS")


def legacy_cost_key(kind: str) -> str:
    """Плоский ключ конфига с ценой воссоздания для данного вида."""
    return LEGACY_COST_KEYS.get(kind, "RECREATE_COST_SECONDS")


def resolve_targets(targets: "TargetSet", cfg: dict[str, Any],
                    explicit: set[str] | None = None,
                    warnings: list[str] | None = None) -> "TargetSet":
    """Подставляет в цели пороги (простоя, цены, свежести сборки и прочие).

    Приоритет для каждого порога:
      1. явно заданный плоский ВИДОВОЙ ключ конфига (например
         KOTLIN_IDLE_SECONDS) — он и есть намерение оператора;
      2. значение из targets.yaml — оно относится к конкретному виду, то
         есть специфичнее общего плоского ключа;
      3. общий плоский ключ конфига (или значение по умолчанию).

    Расхождение значений — не молчание, а предупреждение при запуске.
    """
    from dataclasses import replace as _replace

    explicit = explicit or set()
    resolved: dict[str, Any] = {}
    for name, spec in targets.specs_by_name().items():
        values: dict[str, Any] = {}
        for field, general_key, per_kind in THRESHOLD_KEYS:
            yaml_value = getattr(spec, field)
            kind_key = per_kind.get(name)
            # 1. Явно заданный плоский ВИДОВОЙ ключ — намерение оператора.
            if kind_key is not None and kind_key in explicit:
                flat = float(cfg.get(kind_key, DEFAULTS[kind_key]))
                if yaml_value is not None and float(yaml_value) != flat:
                    if warnings is not None:
                        warnings.append(
                            f"конфиг: ключ {kind_key}={cfg.get(kind_key)} "
                            f"расходится с порогом для «{name}» в "
                            f"targets.yaml ({float(yaml_value):.0f}) — работает "
                            f"значение из конфига; перенесите порог в "
                            f"targets.yaml, чтобы править в одном месте")
                values[field] = flat
                continue
            # 2. Значение из targets.yaml — оно относится к конкретному
            #    виду, то есть специфичнее общего плоского ключа.
            if yaml_value is not None:
                if general_key in explicit:
                    flat = float(cfg.get(general_key, DEFAULTS[general_key]))
                    if flat != float(yaml_value):
                        if warnings is not None:
                            warnings.append(
                                f"конфиг: ключ {general_key}={cfg.get(general_key)} "
                                f"расходится с порогом для «{name}» в "
                                f"targets.yaml ({float(yaml_value):.0f}) — "
                                f"работает значение из targets.yaml; уберите "
                                f"общий ключ из конфига, чтобы править "
                                f"в одном месте")
                values[field] = float(yaml_value)
                continue
            # 3. Запасной путь: видовой ключ (с его значением по умолчанию),
            #    а для новых видов — общий ключ.
            fallback = kind_key or general_key
            values[field] = float(cfg.get(fallback, DEFAULTS[fallback]))
        resolved[name] = _replace(spec, **values)
    return TargetSet(resolved)


def idle_limit(cfg: dict[str, Any], kind: str) -> float:
    """Порог простоя для класса демона (секунды).

    У Gradle и Kotlin разные правила жизни, поэтому разные и пороги:
    у Gradle есть собственный idle-таймаут (180 мин) — наш порог обязан быть
    выше него; у Kotlin такого таймаута нет вовсе, его не убирает никто, и
    2 ч — заведомо выше «живого» простоя (p99.9 = 100 мин).

    Это плоская (legacy) версия: она читает только плоский конфиг и не знает
    про targets.yaml. Основной путь — `idle_limit_for_kind`.
    """
    if kind == "kotlin":
        return float(cfg.get("KOTLIN_IDLE_SECONDS", cfg["IDLE_SECONDS"]))
    return float(cfg["IDLE_SECONDS"])


def idle_limit_for_kind(cfg: dict[str, Any], kind: str,
                        targets: "TargetSet | None" = None) -> float:
    """Порог простоя для вида демона — единственное место, где он считается.

    Описание цели (targets.yaml) важнее плоского ключа конфига, а плоский
    ключ — запасной путь. Раньше это считалось в пяти местах по-разному, и
    одно из них (аудит, который решает «критерий приёмки выполнен») про
    описание цели вообще забывало: при пороге, заданном в YAML, аудит
    сравнивал бы демонов с другим числом.

    После `resolve_targets` обе дороги дают одно число (там уже применён
    приоритет ключей), но считать его надо в одном месте.
    """
    if targets is not None:
        spec = targets.get(kind)
        if spec is not None and spec.idle_seconds is not None:
            return float(spec.idle_seconds)
    return float(idle_limit(cfg, kind))


# Ключи конфига, влияющие на решения по ЛЮБОМУ виду демонов. Меняются —
# меняются решения всех видов, и метрика по всем видам становится непригодной.
GLOBAL_SIGNATURE_KEYS = (
    "IDLE_SECONDS",
    "MIN_FOOTPRINT_MB",
    "BUSY_CPU_SECONDS",
    "SWEEP_SECONDS",
    "MEMORY_PRESSURE_MB",
    "KILL_LARGE_DAEMON_MB",
    "GRADLE_BUILD_ACTIVE_SECONDS",
    "BUILD_ACTIVE_SECONDS",
    "STUCK_AFTER_SECONDS",
    "PEER_MAX_AGE_SECONDS",
    "RECREATE_COST_SECONDS",
    "PID_ALLOWLIST",
)


def _spec_fingerprint(spec: TargetSpec) -> dict[str, Any]:
    """Поля описания цели, влияющие на решения по демонам этого вида."""
    return {
        "idle_seconds": spec.idle_seconds,
        "recreate_cost_seconds": spec.recreate_cost_seconds,
        "build_active_seconds": spec.build_active_seconds,
        "stuck_after_seconds": spec.stuck_after_seconds,
        "min_footprint_mb": spec.min_footprint_mb,
        "large_daemon_mb": spec.large_daemon_mb,
        "self_timeout_seconds": spec.self_timeout_seconds,
        "rebuildable": spec.rebuildable,
        "activity": spec.activity,
    }


def kind_signature(spec: TargetSpec, cfg: dict[str, Any],
                   targets: "TargetSet | None" = None) -> str:
    """Подпись правил, действующих для одного вида демонов.

    Что меняет решения по демонам вида:
      * поля описания цели (пороги, пересоздаваемость, способ измерения
        активности);
      * общие ключи конфига (давление по памяти, порог простоя, белый
        список PID и прочие) — они действуют на все виды сразу;
      * описания видов, которые этот вид защищает (protects): изменилось
        их правило — изменилось и решение по защищаемому.

    Подпись нужна для того, чтобы сбросить метрику ТОЛЬКО по тому виду,
    чьи правила изменились. Общий сброс по всем видам — слишком грубо:
    добавление новой цели обнуляло бы весь накопленный пилот.
    """
    import hashlib
    import json as _json

    payload: dict[str, Any] = {
        "rules_version": RULES_VERSION,
        "spec": _spec_fingerprint(spec),
        "global": {key: cfg.get(key) for key in GLOBAL_SIGNATURE_KEYS},
        "protectors": {},
    }
    if targets is not None:
        for name in spec.protects:
            other = targets.get(name)
            if other is not None:
                payload["protectors"][name] = _spec_fingerprint(other)
    blob = _json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
