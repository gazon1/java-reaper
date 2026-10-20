"""Состояние рейпера: атомарный файл, метрики качества и эффекта.

Формат состояния версионируется: при смене версии запись мигрируется
(неизвестные поля вычищаются), чтобы старый мусор не жил вечно.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .config import RULES_VERSION
from .model import Decision, observed_activity

STATE_VERSION = 2

# Поля записи одного демона. Всё, чего здесь нет, при миграции удаляется.
DAEMON_FIELDS = frozenset({
    "first_seen", "kind", "cpu_ticks", "starttime", "last_cpu_at",
    "log_offset", "last_build_start", "last_build_end", "peer_first_seen",
    "last_activity", "sigterm_attempts", "canceled_at",
})

GLOBAL_FIELDS = frozenset({
    "version", "daemons", "would_kill", "metric", "freed_mb", "would_free_mb",
    "last_sweep_at", "last_sweep_ok", "pilot_started_at", "stale_peer_events",
    "stale_peer_daemons", "rules_version", "would_free_pending_mb",
    "rule_hits", "rule_hits_last", "measured_count", "measured_prev",
    "degradation",
})


def state_path(cfg: dict[str, Any]) -> str:
    from .config import expand
    return os.path.join(expand(str(cfg["STATE_DIR"])), "state.json")


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
    return data


def sync_rules_version(state: dict[str, Any]) -> bool:
    """Сбрасывает метрику пилота, если правила изменились.

    Метрика пилота измеряет долю ложных срабатываний для КОНКРЕТНЫХ правил
    и порогов. После смены правил события, набранные по старым, измеряют
    другое — смешивать их нельзя, иначе вердикт по пилоту бессмыслен.

    Возвращает True, если метрика была сброшена.
    """
    current = int(state.get("rules_version", 0) or 0)
    if current == RULES_VERSION:
        return False
    state["rules_version"] = RULES_VERSION
    state["would_kill"] = []
    state["metric"] = {"events": 0, "reused": 0, "rate": 0.0, "pending": 0}
    state["pilot_started_at"] = 0.0
    state["stale_peer_events"] = 0
    state["stale_peer_daemons"] = {}
    return True


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
                  cfg: dict[str, Any],
                  report: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Следит за судьбой «would kill»: понадобился ли демон снова в течение окна.

    Событие — уникальный демон (PID + starttime, чтобы переиспользованный PID
    не считался тем же демоном). У события есть ИСХОД, и их четыре:

      reused      — демон понадобился снова → ложное срабатывание;
      not_reused  — не понадобился → верное срабатывание;
      unknown     — мы не видели демона часть окна (наблюдение прервано:
                    машина выключена, таймер молчал) → в знаменатель не
                    идёт: иначе доля ложных срабатываний была бы занижена,
                    а вердикт по пилоту — ложнооптимистичен;
      killed      — демон реально убит (kill-режим): «понадобился снова» мы
                    уже не увидим → тоже вне знаменателя.

    В знаменатель попадают только `reused` и `not_reused` — то есть события,
    которые мы наблюдали целиком и по которым вопрос «понадобился ли»
    имеет честный ответ.

    Источник событий — отчёт исполнителя, а не список решений: исполнитель
    режет кандидатов (лимит за свип, прочие проверки), и то, что он не
    выполнил, не должно попадать в метрику.
    """
    window = float(cfg["REUSE_WINDOW_SECONDS"])
    events = state.setdefault("would_kill", [])
    by_pid = {dec.daemon.pid: dec.daemon for dec in decisions}

    # Разрыв наблюдения: сколько прошло с прошлого свипа. Если свипов не
    # было (машина выключена), часть окна мы не видели.
    last_sweep = float(state.get("last_sweep_at") or 0.0)
    gap = (now - last_sweep) if last_sweep else None

    # Кого исполнитель действительно тронет.
    candidates: dict[int, Any] = {}
    killed_pids: set[int] = set()
    if report is None:
        for dec in decisions:
            if dec.kill:
                candidates[dec.daemon.pid] = dec.daemon
    else:
        for item in report:
            action = item.get("action")
            pid = int(item["pid"])
            if action == "dry-run":
                d = by_pid.get(pid)
                if d is not None:
                    candidates[pid] = d
            elif action in ("SIGTERM", "SIGKILL"):
                killed_pids.add(pid)
                d = by_pid.get(pid)
                if d is not None:
                    candidates[pid] = d

    for ev in events:
        if ev.get("final"):
            # Событие, закрытое по старым правилам (или вручную), не имеет
            # исхода — восстанавливаем его из признаков.
            if not ev.get("outcome"):
                if ev.get("killed"):
                    ev["outcome"] = "killed"
                elif float(ev.get("max_gap", 0.0)) > window:
                    ev["outcome"] = "unknown"
                else:
                    ev["outcome"] = "reused" if ev.get("reused") else "not_reused"
            continue
        # Учитываем разрыв наблюдения: событие пережило его.
        if gap is not None:
            ev["max_gap"] = max(float(ev.get("max_gap", 0.0)), gap)
        age = now - float(ev["at"])
        pid = int(ev["pid"])
        if age > window:
            ev["final"] = True
            if ev.get("killed"):
                ev["outcome"] = "killed"
            elif float(ev.get("max_gap", 0.0)) > window:
                ev["outcome"] = "unknown"
            elif ev.get("reused"):
                ev["outcome"] = "reused"
            else:
                ev["outcome"] = "not_reused"
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
    for pid, d in candidates.items():
        if pid in known:
            # Демон уже учитывается: обновляем признак «убит».
            for ev in events:
                if int(ev["pid"]) == pid:
                    if pid in killed_pids:
                        ev["killed"] = True
            continue
        events.append({"pid": pid, "at": now, "reused": False,
                       "final": False, "starttime": d.starttime,
                       "mb": d.footprint_mb, "max_gap": 0.0,
                       "killed": pid in killed_pids})

    finals = [e for e in events if e.get("final")]
    reused = sum(1 for e in finals if e.get("outcome") == "reused")
    not_reused = sum(1 for e in finals if e.get("outcome") == "not_reused")
    unknown = sum(1 for e in finals if e.get("outcome") == "unknown")
    killed = sum(1 for e in finals if e.get("outcome") == "killed")
    countable = reused + not_reused
    pending = [e for e in events if not e.get("final")]
    state["metric"] = {
        "events": len(finals),
        "counted": countable,
        "reused": reused,
        "not_reused": not_reused,
        "unknown": unknown,
        "killed": killed,
        "rate": (reused / countable) if countable else 0.0,
        "pending": len(pending),
    }
    # «Могло бы быть освобождено»: накопленное (все события) и то, что
    # висит под ударом прямо сейчас (незакрытые события).
    state["would_free_mb"] = sum(int(e.get("mb", 0)) for e in events)
    state["would_free_pending_mb"] = sum(int(e.get("mb", 0)) for e in pending)
    _trim_events(events)
    return state["metric"]


def _trim_events(events: list[dict[str, Any]], keep: int = 100) -> None:
    """Не даём списку событий расти бесконечно.

    Завершённые события нужны только для отчётов за последний период;
    незакрытые (pending) — терять нельзя, по ним считается пилот.
    """
    finals = [e for e in events if e.get("final")]
    if len(finals) <= keep:
        return
    tail = finals[-keep:]
    pending = [e for e in events if not e.get("final")]
    events[:] = pending + tail


def record_freed(state: dict[str, Any], report: list[dict[str, Any]]) -> int:
    """Считает фактически освобождённую память по отчёту исполнения."""
    freed = 0
    for item in report:
        if item.get("action") in ("SIGTERM", "SIGKILL"):
            freed += int(item.get("footprint_mb", 0))
    state["freed_mb"] = int(state.get("freed_mb", 0)) + freed
    return freed


# Правила, после которых демон считается «неизмеренным»: мы не знаем,
# сколько он простаивает, и потому не можем оценить его кандидатство.
UNMEASURED_RULES = frozenset({"no-log", "unknown-history", "no-baseline"})


def note_rule_hits(state: dict[str, Any], decisions: list[Decision]) -> dict[str, int]:
    """Считает, какие правила сработали.

    Наблюдаемость правил: без неё деградация детекции (например, формат
    лога изменился и все демоны ушли в «неизвестная история») выглядит
    как обычное «нечего было убивать».
    """
    hits: dict[str, int] = {}
    for dec in decisions:
        name = dec.rule or "unknown"
        hits[name] = hits.get(name, 0) + 1
    total = state.setdefault("rule_hits", {})
    for name, count in hits.items():
        total[name] = int(total.get(name, 0)) + count
    state["rule_hits_last"] = hits
    return hits


def measure_coverage(state: dict[str, Any], decisions: list[Decision],
                     cfg: dict[str, Any]) -> tuple[int, int]:
    """(измеренных демонов, всего демонов) и обновление счётчиков."""
    total = len(decisions)
    measured = sum(1 for dec in decisions
                   if (dec.rule or "") not in UNMEASURED_RULES)
    state["measured_prev"] = int(state.get("measured_count", 0) or 0)
    state["measured_count"] = measured
    return measured, total


def degradation_warnings(state: dict[str, Any], cfg: dict[str, Any],
                         measured: int | None = None,
                         total: int | None = None) -> list[str]:
    """Признаки того, что рейпер слеп и молчит.

    Два сигнала:
      * измерить удалось меньше половины демонов — почти все под защитой
        «не знаем», то есть ни один не может быть оценён;
      * число измеренных демонов упало больше чем вдвое относительно
        прошлого свипа — детектор стал видеть меньше.
    """
    out: list[str] = []
    if measured is None:
        measured = int(state.get("measured_count", 0) or 0)
    if total is None:
        total = 0
    ratio = float(cfg.get("DEGRADATION_MIN_MEASURED_RATIO", 0.5))
    if total > 0 and measured / total < ratio:
        out.append(f"измерить удалось лишь {measured} из {total} демонов "
                   f"(< {100 * ratio:.0f} %): детекция, скорее всего, деградировала")
    prev = int(state.get("measured_prev", 0) or 0)
    count_ratio = float(cfg.get("DEGRADATION_MIN_COUNT_RATIO", 0.5))
    if prev > 0 and measured < prev * count_ratio:
        out.append(f"число измеренных демонов упало: {prev} → {measured} "
                   f"(прошлый свип)")
    return out


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


def append_sweep(cfg: dict[str, Any], record: dict[str, Any]) -> None:
    """Дописывает сводку свипа в JSONL-журнал (история решений)."""
    path = sweeps_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def sweep_record(decisions: list[Decision], now: float,
                 cfg: dict[str, Any]) -> dict[str, Any]:
    """Сводка одного свипа для журнала.

    Ключевое поле — `would_free_mb`: сколько памяти висело под ударом
    в этом свипе. Именно это и есть эффект рейпера, измеряемый непрерывно,
    а не «сколько демонов убито» (их всегда мало, а памяти — много).
    """
    kills = [d for d in decisions if d.kill]
    return {
        "at": now,
        "daemons": len(decisions),
        "footprint_mb": sum(d.daemon.footprint_mb for d in decisions),
        "would_kill": len(kills),
        "would_free_mb": sum(d.daemon.footprint_mb for d in kills),
        "by_kind": {
            kind: {
                "count": sum(1 for d in decisions if d.daemon.kind == kind),
                "footprint_mb": sum(d.daemon.footprint_mb for d in decisions
                                    if d.daemon.kind == kind),
                "would_free_mb": sum(d.daemon.footprint_mb for d in kills
                                     if d.daemon.kind == kind),
            }
            for kind in ("gradle", "kotlin")
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
