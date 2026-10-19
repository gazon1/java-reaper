"""Состояние рейпера: атомарный файл, метрики качества и эффекта."""

from __future__ import annotations

import json
import os
from typing import Any

from .model import Decision, observed_activity


def state_path(cfg: dict[str, Any]) -> str:
    from .config import expand
    return os.path.join(expand(str(cfg["STATE_DIR"])), "state.json")


def load_state(cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        with open(state_path(cfg), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("daemons", {})
    data.setdefault("would_kill", [])
    data.setdefault("metric", {"events": 0, "reused": 0})
    data.setdefault("freed_mb", 0)          # фактически освобождено памяти
    data.setdefault("would_free_mb", 0)     # могло бы быть освобождено (dry-run)
    data.setdefault("last_sweep_at", 0.0)
    data.setdefault("last_sweep_ok", False)
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
