"""Отчёты: человекочитаемая таблица, машинный JSON, canary, health."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .model import Decision


def format_status(decisions: list[Decision], state: dict[str, Any]) -> str:
    lines = [f"{'PID':>8} {'класс':>7} {'footprint':>10} {'простой':>9}  решение / причина"]
    lines.append("-" * 96)
    for dec in sorted(decisions, key=lambda d: -d.daemon.footprint_mb):
        d = dec.daemon
        idle = f"{dec.idle_seconds / 60:.0f} мин" if dec.idle_seconds is not None else "?"
        verdict = "УБИТЬ" if dec.kill else "оставить"
        lines.append(f"{d.pid:>8} {d.kind:>7} {d.footprint_mb:>7} МБ {idle:>9}  "
                     f"{verdict}: {dec.reason}")
    total = sum(d.daemon.footprint_mb for d in decisions)
    idle_total = sum(d.daemon.footprint_mb for d in decisions if d.kill)
    lines.append("-" * 96)
    lines.append(f"всего демонов: {len(decisions)}, footprint: {total} МБ, "
                 f"под убийство: {idle_total} МБ")
    metric = state.get("metric", {})
    if metric:
        lines.append(f"метрика dry-run: событий {metric.get('events', 0)}, "
                     f"повторно нужны {metric.get('reused', 0)} "
                     f"({100 * metric.get('rate', 0):.0f} %), в ожидании {metric.get('pending', 0)}")
    freed = int(state.get("freed_mb", 0))
    would = int(state.get("would_free_mb", 0))
    if freed or would:
        lines.append(f"память: освобождено {freed} МБ, могло бы быть освобождено {would} МБ")
    return "\n".join(lines)


def decision_to_dict(dec: Decision) -> dict[str, Any]:
    d = dec.daemon
    return {
        "pid": d.pid,
        "kind": d.kind,
        "footprint_mb": d.footprint_mb,
        "rss_mb": d.rss_mb,
        "swap_mb": d.swap_mb,
        "idle_seconds": dec.idle_seconds,
        "action": dec.action.value,
        "reason": dec.reason,
        "busy": dec.busy,
        "protected": dec.protected,
        "protectors": [
            {"kind": p.kind, "detail": p.detail, "pid": p.pid, "name": p.name}
            for p in dec.protectors
        ],
        "children": d.children,
        "ignored_children": d.ignored_children,
        "peers": [
            {"pid": c.owner_pid, "name": c.owner_name,
             "local_port": c.local_port, "remote_port": c.remote_port,
             "age_seconds": c.age}
            for c in d.peer_conns
        ],
        "log_path": d.log_path,
    }


def format_json(decisions: list[Decision], state: dict[str, Any],
                cfg: dict[str, Any], report: list[dict[str, Any]] | None = None,
                now: float | None = None) -> str:
    now = time.time() if now is None else now
    payload = {
        "now": now,
        "config": {
            "IDLE_SECONDS": cfg["IDLE_SECONDS"],
            "MIN_FOOTPRINT_MB": cfg["MIN_FOOTPRINT_MB"],
            "BUSY_CPU_SECONDS": cfg["BUSY_CPU_SECONDS"],
            "SWEEP_SECONDS": cfg["SWEEP_SECONDS"],
            "MAX_KILLS_PER_SWEEP": cfg.get("MAX_KILLS_PER_SWEEP"),
            "PEER_MAX_AGE_SECONDS": cfg.get("PEER_MAX_AGE_SECONDS"),
            "PID_ALLOWLIST": cfg.get("PID_ALLOWLIST", ""),
        },
        "daemons": [decision_to_dict(d) for d in decisions],
        "metric": state.get("metric", {}),
        "freed_mb": state.get("freed_mb", 0),
        "would_free_mb": state.get("would_free_mb", 0),
        "last_sweep_at": state.get("last_sweep_at", 0.0),
        "actions": report or [],
    }
    return json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True)


def canary(daemons: list[Any], decisions: list[Decision]) -> str | None:
    """Предупреждение: детектор молча перестал видеть демонов."""
    if daemons:
        return None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        from .proc import proc_cmdline
        cmdline = proc_cmdline(int(entry))
        if "gradle" in cmdline.lower() or "kotlin" in cmdline.lower():
            if "daemon" in cmdline.lower() or "KotlinCompileDaemon" in cmdline:
                return ("ВНИМАНИЕ: кандидатов 0, но есть java-процессы со словом "
                        "gradle/kotlin — вероятно, изменился main-класс или формат логов")
    return None


def health(cfg: dict[str, Any], state: dict[str, Any]) -> tuple[str, int]:
    """Проверка «таймер молча не работает».

    Свип оставляет метку времени; если её нет или она старше двух интервалов —
    рейпер не запускался (таймер не сработал, машина спала, рейпер выключен).
    """
    sweep = float(cfg["SWEEP_SECONDS"])
    last = float(state.get("last_sweep_at", 0.0))
    now = time.time()
    age = now - last if last else None
    lines = [f"java-reaper: последний свип: "
             + ("никогда" if age is None else f"{age / 60:.1f} мин назад")]
    lines.append(f"java-reaper: интервал свипа: {sweep / 60:.0f} мин")
    lines.append(f"java-reaper: освобождено памяти: {int(state.get('freed_mb', 0))} МБ, "
                 f"могло бы быть: {int(state.get('would_free_mb', 0))} МБ")
    metric = state.get("metric", {})
    if metric:
        lines.append(f"java-reaper: метрика: событий {metric.get('events', 0)}, "
                     f"повторно нужны {metric.get('reused', 0)} "
                     f"({100 * metric.get('rate', 0):.0f} %)")
    if age is None:
        lines.append("java-reaper: СТАТУС: свипов не было — таймер не запущен "
                     "или рейпер ещё ни разу не отработал")
        return "\n".join(lines), 1
    if age > 2 * sweep:
        lines.append(f"java-reaper: СТАТУС: свип старше двух интервалов "
                     f"({age / 60:.1f} мин) — таймер молчит")
        return "\n".join(lines), 1
    lines.append("java-reaper: СТАТУС: ок")
    return "\n".join(lines), 0
