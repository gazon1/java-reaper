"""Классификатор: превращает наблюдения в вердикт.

Порядок работы:
1. локальные измерения (простой, CPU, дети, свежесть лога);
2. least fixed point по peer'ам: peer защищает, только если он не кандидат
   (клиент сборки, IDE) или сам занят; соединение старше PEER_MAX_AGE_SECONDS
   считается зависшим клиентом и не защищает;
3. таблица правил (reaper.policy) выносит вердикт.
"""

from __future__ import annotations

import os
from typing import Any

from .config import allowlist
from .model import Action, Daemon, Decision, Protector, observed_activity
from .policy import Rule, Signals, default_policy


def _measure(d: Daemon, rec: dict[str, Any], now: float,
             cfg: dict[str, Any]) -> Signals:
    """Все измерения по одному демону."""
    idle_limit = float(cfg["IDLE_SECONDS"])
    sweep = float(cfg["SWEEP_SECONDS"])
    hz = os.sysconf("SC_CLK_TCK")

    first_seen = rec.get("first_seen", now)
    last_state_activity = rec.get("last_activity") or None

    cpu_delta = None
    if "cpu_ticks" in rec:
        # Дельту приводим к «одному свипу»: если свип задержался (таймер
        # пропустил запуск), накопленный CPU иначе выглядел бы как работа.
        elapsed = (now - float(rec["last_cpu_at"])) if rec.get("last_cpu_at") else sweep
        scale = sweep / max(elapsed, 1.0)
        cpu_delta = (d.cpu_ticks - int(rec["cpu_ticks"])) / float(hz) * scale

    log_recent = False
    if d.kind == "kotlin" and d.log_mtime is not None:
        log_recent = (now - d.log_mtime) <= sweep
    elif d.kind == "gradle" and d.last_build_end is not None:
        log_recent = (now - d.last_build_end) <= sweep

    observed = observed_activity(d)
    # Если сборка идёт (старт без конца), отсчёт простоя идёт от её старта:
    # иначе демон, чья предыдущая сборка закончилась 5 часов назад, а текущая
    # идёт уже 45 минут, выглядел бы простаивающим 5 часов.
    if d.build_in_progress and d.last_build_start is not None:
        observed = max(x for x in (observed, d.last_build_start) if x is not None)
    # Опоры простоя: только измеренная активность (лог) и наша собственная
    # история. first_seen — лишь fallback, когда активность измерить нечем:
    # иначе демон, простаивающий 10 суток, нельзя было бы убить в первые
    # сутки наблюдения.
    anchors = [x for x in (observed, last_state_activity) if x is not None]
    base = max(anchors) if anchors else first_seen

    allowed_pids = allowlist(cfg)
    return Signals(
        daemon=d,
        now=now,
        age=now - base,
        idle_limit=idle_limit,
        min_footprint=int(cfg["MIN_FOOTPRINT_MB"]),
        cpu_delta=cpu_delta,
        cpu_busy=cpu_delta is not None and cpu_delta > float(cfg["BUSY_CPU_SECONDS"]),
        has_children=bool(d.children),
        log_recent=log_recent,
        build_in_progress=d.build_in_progress,
        baseline_known="cpu_ticks" in rec,
        log_ok=d.log_ok,
        idle_known=d.idle_known,
        allowed=(not allowed_pids) or d.pid in allowed_pids,
    )


def _peer_protection(signals: dict[int, Signals], peer_max_age: float
                     ) -> tuple[dict[int, list[Protector]], dict[int, list[Protector]]]:
    """Least fixed point снизу вверх: кто кого защищает.

    Возвращает (защиты, зависшие соединения). Соединение, которое висит дольше
    peer_max_age, защиту не даёт: это «зависший клиент», а не наблюдатель.
    """
    known = set(signals)
    protection: dict[int, list[Protector]] = {pid: [] for pid in signals}
    stale: dict[int, list[Protector]] = {pid: [] for pid in signals}

    changed = True
    while changed:
        changed = False
        for pid, s in signals.items():
            if protection[pid]:
                continue
            for conn in s.daemon.peer_conns:
                if conn.age is not None and conn.age > peer_max_age:
                    stale[pid].append(Protector(
                        "peer-stale",
                        f"peer: соединение висит {conn.age / 3600:.1f} ч "
                        f"(порт {conn.remote_port}) — клиент не считается наблюдателем",
                        pid=conn.owner_pid, name=conn.owner_name))
                    continue
                owner = conn.owner_pid
                if owner is None:
                    prot = Protector("peer-unknown",
                                     "peer: соединение с неопределённым владельцем "
                                     f"(порт {conn.remote_port})")
                elif owner not in known:
                    name = conn.owner_name or "?"
                    prot = Protector("peer-client",
                                     f"peer: клиент сборки (pid {owner}, {name})",
                                     pid=owner, name=name)
                else:
                    owner_s = signals[owner]
                    if not (owner_s.cpu_busy or owner_s.has_children
                            or protection[owner]):
                        # Peer-кандидат: два простаивающих демона не защищают
                        # друг друга, иначе ничего не удастся убрать.
                        continue
                    name = conn.owner_name or "?"
                    prot = Protector("peer-busy",
                                     f"peer: занят (pid {owner}, {name})",
                                     pid=owner, name=name)
                protection[pid].append(prot)
                changed = True
                break
    return protection, stale


def evaluate(signals: Signals, rules: list[Rule]) -> Decision:
    """Первое совпавшее правило решает."""
    for rule in rules:
        if not rule.check(signals):
            continue
        return Decision(
            daemon=signals.daemon,          # type: ignore[arg-type]
            action=rule.action,
            reason=rule.reason(signals),
            idle_seconds=signals.age,
            last_activity=signals.now - signals.age,
            busy=rule.busy,
            protected=rule.protected,
            protectors=list(signals.peer_protection),
            stale_peers=list(signals.peer_stale),
        )
    # Правил не должно быть без catch-all; на всякий случай — оставить.
    return Decision(daemon=signals.daemon,  # type: ignore[arg-type]
                    action=Action.KEEP, reason="правило не сработало — оставляю",
                    idle_seconds=signals.age, protected=True)


def classify(daemons: list[Daemon], state: dict[str, Any], now: float,
             cfg: dict[str, Any]) -> list[Decision]:
    """Решает, кого убивать. Чистая функция: не трогает систему."""
    rules = default_policy(cfg)
    peer_max_age = float(cfg["PEER_MAX_AGE_SECONDS"])
    prev = state.get("daemons", {})

    signals: dict[int, Signals] = {}
    for d in daemons:
        rec = prev.get(str(d.pid)) or {}
        if rec.get("starttime") is not None and rec["starttime"] != d.starttime:
            rec = {}  # PID переиспользован
        signals[d.pid] = _measure(d, rec, now, cfg)

    protection, stale = _peer_protection(signals, peer_max_age)
    for pid, s in signals.items():
        s.peer_protection = protection.get(pid, [])
        s.peer_stale = stale.get(pid, [])

    return [evaluate(signals[d.pid], rules) for d in daemons]
