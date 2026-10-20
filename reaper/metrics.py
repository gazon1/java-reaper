"""Метрики рейпера: качество детекции (пилот), покрытие и деградация.

Здесь живёт всё, что ИЗМЕРЯЕТ работу рейпера: доля ложных срабатываний
по пилоту, доля демонов, которых вообще удалось измерить, счётчики
срабатывания правил. Формат состояния при этом не меняется — метрики
пишутся в те же ключи `state.json`, что и раньше.
"""

from __future__ import annotations

from typing import Any

from .config import RULES_VERSION
from .model import Decision, observed_activity


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
    state["kind_signatures"] = {}
    return True


def sync_kind_signatures(state: dict[str, Any], targets: Any,
                         cfg: dict[str, Any]) -> list[str]:
    """Точечный сброс метрики: по видам, у которых изменились правила.

    Зачем: смена правил одной цели (например, подняли порог простоя
    только для неё) обнуляла бы весь накопленный пилот по всем видам —
    а метрика по остальным видам от этого изменения не менялась и
    остаётся честной.

    Возвращает список видов, чьи события выброшены.
    """
    from .config import kind_signature

    signatures = state.setdefault("kind_signatures", {})
    events = state.setdefault("would_kill", [])
    changed: list[str] = []
    for name, spec in targets.specs_by_name().items():
        signature = kind_signature(spec, cfg, targets)
        previous = signatures.get(name)
        signatures[name] = signature
        if previous is None or previous == signature:
            continue
        # Правила вида изменились: его события измеряли другое.
        stale = [e for e in events if str(e.get("kind") or "unknown") == name]
        if not stale:
            continue
        events[:] = [e for e in events
                     if str(e.get("kind") or "unknown") != name]
        changed.append(name)
    return changed


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
    kind_of = {dec.daemon.pid: dec.daemon.kind for dec in decisions}
    cost_of = {dec.daemon.pid: float(dec.recreate_cost_seconds)
               for dec in decisions}

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
                       "killed": pid in killed_pids,
                       # Вид демона и цена его воссоздания: без них метрика
                       # была бы одной общей цифрой на все виды, а цена
                       # ошибки у разных видов разная (Gradle — 40 с,
                       # дешёвый демон — единицы секунд).
                       "kind": kind_of.get(pid, "unknown"),
                       "cost": cost_of.get(pid, 0.0)})

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
        "by_kind": _metric_by_kind(events),
    }
    # «Могло бы быть освобождено»: накопленное (все события) и то, что
    # висит под ударом прямо сейчас (незакрытые события).
    state["would_free_mb"] = sum(int(e.get("mb", 0)) for e in events)
    state["would_free_pending_mb"] = sum(int(e.get("mb", 0)) for e in pending)
    _trim_events(events)
    return state["metric"]


def _metric_by_kind(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Метрика по видам демонов + взвешивание по цене воссоздания.

    Почему по видам: у разных программ разная цена ошибки. Ложное
    срабатывание на Gradle-демоне стоит 40 с пересборки, на дешёвом
    демоне — единицы секунд. Одна общая доля «ложных» это разные вещи
    смешивает, и вердикт по ней получается бессмысленным, как только
    целей больше одной.

    Взвешивание: rate_weighted — доля ложных, посчитанная по сумме цен
    воссоздания, а не по числу событий. Ложное срабатывание на дорогом
    демоне весит больше, чем на дешёвом, — и это должно отражаться в
    вердикте.
    """
    out: dict[str, Any] = {}
    for event in events:
        kind = str(event.get("kind") or "unknown")
        bucket = out.setdefault(kind, {
            "events": 0, "counted": 0, "reused": 0, "not_reused": 0,
            "unknown": 0, "killed": 0, "pending": 0,
            "cost_reused_seconds": 0.0, "cost_counted_seconds": 0.0,
        })
        outcome = event.get("outcome")
        if not event.get("final"):
            bucket["pending"] += 1
            continue
        bucket["events"] += 1
        cost = float(event.get("cost") or 0.0)
        if outcome == "reused":
            bucket["reused"] += 1
            bucket["counted"] += 1
            bucket["cost_reused_seconds"] += cost
            bucket["cost_counted_seconds"] += cost
        elif outcome == "not_reused":
            bucket["not_reused"] += 1
            bucket["counted"] += 1
            bucket["cost_counted_seconds"] += cost
        elif outcome == "unknown":
            bucket["unknown"] += 1
        elif outcome == "killed":
            bucket["killed"] += 1
    for bucket in out.values():
        counted = bucket["counted"]
        bucket["rate"] = (bucket["reused"] / counted) if counted else 0.0
        cost_total = bucket["cost_counted_seconds"]
        bucket["rate_weighted"] = ((bucket["cost_reused_seconds"] / cost_total)
                                   if cost_total else 0.0)
    return out


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
