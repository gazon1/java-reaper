"""Журналы рейпера: история решений (JSONL) и аудит убийств.

Журналы отделены от `state.json` намеренно: `state.json` — это текущее
состояние (перезаписывается целиком, формат версионируется), а журналы —
накопительная история (append-only), по которой считается эффект рейпера
за произвольный период.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .model import Decision


def kills_path(cfg: dict[str, Any]) -> str:
    """Структурированный лог убийств (JSONL) — рядом с состоянием."""
    from .config import expand
    return os.path.join(expand(str(cfg["STATE_DIR"])), "kills.jsonl")


def sweeps_path(cfg: dict[str, Any]) -> str:
    """Журнал свипов (JSONL): что мы видели и какие решения приняли.

    В отличие от `state.json` (последнее состояние) это ИСТОРИЯ: по ней
    видно, сколько памяти висело под ударом, какие правила срабатывали
    и как менялась картина со временем.
    """
    from .config import expand
    return os.path.join(expand(str(cfg["STATE_DIR"])), "sweeps.jsonl")


def append_sweep(cfg: dict[str, Any], record: dict[str, Any]) -> None:
    """Дописывает сводку свипа в JSONL-журнал (история решений)."""
    path = sweeps_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def sweep_record(decisions: list[Decision], now: float,
                 cfg: dict[str, Any],
                 mem_available_mb: int | None = None) -> dict[str, Any]:
    """Сводка одного свипа для журнала.

    Ключевое поле — `would_free_mb`: сколько памяти висело под ударом
    в этом свипе. Именно это и есть эффект рейпера, измеряемый непрерывно,
    а не «сколько демонов убито» (их всегда мало, а памяти — много).

    `mem_available_mb` — сколько памяти было доступно системе в момент
    свипа. Без него по журналу нельзя понять, при каком давлении памяти
    рейпер начал бы убирать: правило «памяти хватает — не трогай» не
    оставляет следов, и порог давления оказывается непроверенным.
    """
    kills = [d for d in decisions if d.kill]
    return {
        "at": now,
        "daemons": len(decisions),
        "footprint_mb": sum(d.daemon.footprint_mb for d in decisions),
        "would_kill": len(kills),
        "would_free_mb": sum(d.daemon.footprint_mb for d in kills),
        "mem_available_mb": mem_available_mb,
        # Виды — те, что реально встретились в этом свипе. Список не
        # зашит: новая цель попадает в журнал сама, иначе её эффект был бы
        # не виден в месячном отчёте (тихая потеря данных).
        "by_kind": {
            kind: {
                "count": sum(1 for d in decisions if d.daemon.kind == kind),
                "footprint_mb": sum(d.daemon.footprint_mb for d in decisions
                                    if d.daemon.kind == kind),
                "would_free_mb": sum(d.daemon.footprint_mb for d in kills
                                     if d.daemon.kind == kind),
            }
            for kind in sorted({d.daemon.kind for d in decisions})
        },
        "rules": {
            name: sum(1 for d in decisions if (d.rule or "") == name)
            for name in sorted({d.rule or "unknown" for d in decisions})
        },
    }


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
