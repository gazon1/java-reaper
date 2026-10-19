"""Состояние рейпера: атомарный файл, метрики качества и эффекта.

Формат состояния версионируется: при смене версии запись мигрируется
(неизвестные поля вычищаются), чтобы старый мусор не жил вечно.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .model import Decision, observed_activity

STATE_VERSION = 2

# Поля записи одного демона. Всё, чего здесь нет, при миграции удаляется.
DAEMON_FIELDS = frozenset({
    "first_seen", "kind", "cpu_ticks", "starttime", "last_cpu_at",
    "log_offset", "last_build_start", "last_build_end", "peer_first_seen",
    "last_activity", "sigterm_attempts",
})

GLOBAL_FIELDS = frozenset({
    "version", "daemons", "would_kill", "metric", "freed_mb", "would_free_mb",
    "last_sweep_at", "last_sweep_ok", "pilot_started_at", "stale_peer_events",
    "stale_peer_daemons",
})


def state_path(cfg: dict[str, Any]) -> str:
    from .config import expand
    return os.path.join(expand(str(cfg["STATE_DIR"])), "state.json")


def kills_path(cfg: dict[str, Any]) -> str:
    """Структурированный лог убийств (JSONL) — рядом с состоянием."""
    from .config import expand
    return os.path.join(expand(str(cfg["STATE_DIR"])), "kills.jsonl")


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


def update_metric(state: dict[str, Any], decisions: list[Decision], now: float,
                  cfg: dict[str, Any]) -> dict[str, Any]:
    """Следит за судьбой «would kill»: понадобился ли демон снова в течение окна.

    Событие — это уникальный PID (плюс starttime, чтобы переиспользованный PID
    не считался тем же демоном).
    """
    window = float(cfg["REUSE_WINDOW_SECONDS"])
    events = state.setdefault("would_kill", [])
    by_pid = {dec.daemon.pid: dec.daemon for dec in decisions}

    for ev in events:
        if ev.get("final"):
            continue
        age = now - float(ev["at"])
        pid = int(ev["pid"])
        if age > window:
            ev["final"] = True
            continue
        d = by_pid.get(pid)
        if d is None:
            continue
        if ev.get("starttime") is not None and d.starttime != ev["starttime"]:
            # PID переиспользован: это уже другой процесс, а не «тот же демон».
            continue
        act = observed_activity(d)
        if act is not None and act > float(ev["at"]):
            ev["reused"] = True

    # новые события
    known = {int(e["pid"]) for e in events}
    for dec in decisions:
        if dec.kill and dec.daemon.pid not in known:
            events.append({"pid": dec.daemon.pid, "at": now, "reused": False,
                           "final": False, "starttime": dec.daemon.starttime,
                           "mb": dec.daemon.footprint_mb})

    finals = [e for e in events if e.get("final")]
    reused = sum(1 for e in finals if e.get("reused"))
    state["metric"] = {
        "events": len(finals),
        "reused": reused,
        "rate": (reused / len(finals)) if finals else 0.0,
        "pending": len([e for e in events if not e.get("final")]),
    }
    state["would_free_mb"] = sum(int(e.get("mb", 0)) for e in events)
    return state["metric"]


def record_freed(state: dict[str, Any], report: list[dict[str, Any]]) -> int:
    """Считает фактически освобождённую память по отчёту исполнения."""
    freed = 0
    for item in report:
        if item.get("action") in ("SIGTERM", "SIGKILL"):
            freed += int(item.get("footprint_mb", 0))
    state["freed_mb"] = int(state.get("freed_mb", 0)) + freed
    return freed


def note_stale_peers(state: dict[str, Any], decisions: list[Decision]) -> int:
    """Считает, сколько раз сработало правило «зависший клиент».

    Правило снимает защиту с демона, у которого соединение висит дольше
    PEER_MAX_AGE_SECONDS. Оно влияет на решения, но до сих пор было
    непроверяемым — теперь это видно в метриках пилота.
    """
    touched = 0
    per_daemon = state.setdefault("stale_peer_daemons", {})
    for dec in decisions:
        if not dec.stale_peers:
            continue
        touched += len(dec.stale_peers)
        per_daemon[str(dec.daemon.pid)] = int(per_daemon.get(str(dec.daemon.pid), 0)) + 1
    if touched:
        state["stale_peer_events"] = int(state.get("stale_peer_events", 0)) + touched
    return touched


def append_kill(cfg: dict[str, Any], record: dict[str, Any]) -> None:
    """Дописывает запись об убийстве в JSONL-лог (аудит, кто и что убил)."""
    path = kills_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = dict(record)
    record.setdefault("at", time.time())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_kills(cfg: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    """Читает лог убийств (последние limit записей)."""
    path = kills_path(cfg)
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out[-limit:] if limit else out
