"""Состояние рейпера: атомарный файл, версия формата, загрузка и сохранение.

Формат состояния версионируется: при смене версии запись мигрируется
(неизвестные поля вычищаются), чтобы старый мусор не жил вечно.

Этот модуль — ТОЧКА ВХОДА: сам он держит только формат состояния
(путь, версия, миграция, чтение/запись), а всё остальное разнесено по
смыслу:
  * `reaper.metrics` — метрика пилота, покрытие, деградация, счётчики
    правил;
  * `reaper.journal` — журналы убийств и свипов (история).

Имена из `reaper.metrics` и `reaper.journal` переэкспортируются здесь
без изменений: `from reaper.state import update_metric` продолжает
работать — это публичный контракт модуля, на него завязаны остальные
части пакета и инструменты в `tools/`.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .journal import (  # noqa: F401  (публичный API, переэкспорт)
    append_kill,
    append_sweep,
    kills_path,
    read_kills,
    sweep_record,
    sweeps_path,
)
from .metrics import (  # noqa: F401  (публичный API, переэкспорт)
    UNMEASURED_RULES,
    degradation_warnings,
    measure_coverage,
    note_rule_hits,
    note_stale_peers,
    record_freed,
    sync_kind_signatures,
    sync_rules_version,
    update_metric,
)

STATE_VERSION = 2

# Поля записи одного демона. Всё, чего здесь нет, при миграции удаляется.
DAEMON_FIELDS = frozenset({
    "first_seen", "kind", "cpu_ticks", "starttime", "last_cpu_at",
    "log_offset", "last_build_start", "last_build_end", "peer_first_seen",
    "last_activity", "sigterm_attempts", "canceled_at", "log_path",
})

GLOBAL_FIELDS = frozenset({
    "version", "daemons", "would_kill", "metric", "freed_mb", "would_free_mb",
    "last_sweep_at", "last_sweep_ok", "pilot_started_at", "stale_peer_events",
    "stale_peer_daemons", "rules_version", "would_free_pending_mb",
    "rule_hits", "rule_hits_last", "measured_count", "measured_prev",
    "degradation", "mem_available_mb", "kind_signatures",
})


def state_path(cfg: dict[str, Any]) -> str:
    from .config import expand
    return os.path.join(expand(str(cfg["STATE_DIR"])), "state.json")


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Приводит состояние к текущей версии: вычищает неизвестные поля."""
    out: dict[str, Any] = {k: v for k, v in data.items() if k in GLOBAL_FIELDS}
    daemons = data.get("daemons")
    if isinstance(daemons, dict):
        out["daemons"] = {
            pid: {k: v for k, v in rec.items() if k in DAEMON_FIELDS}
            for pid, rec in daemons.items()
            if isinstance(rec, dict)
        }
    out["version"] = STATE_VERSION
    return out


def load_state(cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        with open(state_path(cfg), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if int(data.get("version", 0)) != STATE_VERSION:
        data = _migrate(data)
    data.setdefault("version", STATE_VERSION)
    data.setdefault("daemons", {})
    data.setdefault("would_kill", [])
    data.setdefault("metric", {"events": 0, "reused": 0})
    data.setdefault("freed_mb", 0)          # фактически освобождено памяти
    data.setdefault("would_free_mb", 0)     # могло бы быть освобождено (dry-run)
    data.setdefault("last_sweep_at", 0.0)
    data.setdefault("last_sweep_ok", False)
    data.setdefault("pilot_started_at", 0.0)
    data.setdefault("stale_peer_events", 0)    # сколько раз сработало «зависший клиент»
    data.setdefault("stale_peer_daemons", {})  # pid → число срабатываний
    data.setdefault("rules_version", 0)        # версия правил, по которым набрана метрика
    data.setdefault("would_free_pending_mb", 0)   # что висит под ударом прямо сейчас
    data.setdefault("rule_hits", {})          # сколько раз сработало каждое правило
    data.setdefault("rule_hits_last", {})     # то же, но за последний свип
    data.setdefault("measured_count", 0)      # сколько демонов мы смогли измерить
    data.setdefault("measured_prev", 0)       # то же на прошлом свипе
    data.setdefault("degradation", [])        # предупреждения о деградации
    data.setdefault("mem_available_mb", None) # сколько памяти доступно системе
    data.setdefault("kind_signatures", {})   # подпись правил по видам демонов
    return data


def save_state(cfg: dict[str, Any], state: dict[str, Any]) -> None:
    """Запись атомарная: временный файл + rename, чтобы прерванный свип
    не оставил обрезанный state.json."""
    path = state_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


__all__ = [
    # формат состояния
    "DAEMON_FIELDS",
    "GLOBAL_FIELDS",
    "STATE_VERSION",
    "UNMEASURED_RULES",
    # пути и чтение/запись
    "kills_path",
    "load_state",
    "save_state",
    "state_path",
    "sweeps_path",
    # журналы
    "append_kill",
    "append_sweep",
    "read_kills",
    "sweep_record",
    # метрики
    "degradation_warnings",
    "measure_coverage",
    "note_rule_hits",
    "note_stale_peers",
    "record_freed",
    "sync_kind_signatures",
    "sync_rules_version",
    "update_metric",
]
