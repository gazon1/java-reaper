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
from dataclasses import replace
from typing import Any

from .config import (allowlist, idle_limit_for_kind as _idle_limit_for_kind,
                    legacy_cost_key)
from .model import (ACTIVITY_MARKERS, ACTIVITY_MTIME, Action, Context, Daemon,
                    Decision, Protector, observed_activity)
from .proc import mem_available_mb
from .policy import Rule, Signals, default_policy
from .targets import TargetSet, builtin_targets


def recreate_cost(cfg: dict[str, Any], kind: str,
                  targets: TargetSet | None = None) -> float:
    """Сколько секунд стоит воссоздать демона этого класса (холодный старт).

    Измерено: Gradle — 41.6 с против 2.4 с тёплого. Kotlin дешёв: это
    старт JVM. Значение нужно, чтобы при нехватке памяти первым убирать
    того, чьё пересоздание дешевле.

    Если вид описан в наборе целей — берём цену оттуда (targets.yaml),
    иначе — из плоского конфига.
    """
    if targets is not None:
        spec = targets.get(kind)
        if spec is not None and spec.recreate_cost_seconds is not None:
            return float(spec.recreate_cost_seconds)
    key = legacy_cost_key(kind)
    return float(cfg.get(key, 0) or 0)


def idle_limit_for_kind(cfg: dict[str, Any], kind: str,
                        targets: TargetSet | None = None) -> float:
    """Порог простоя для вида демона (секунды).

    Обёртка над `config.idle_limit_for_kind` — там это считается в одном
    месте, чтобы отчёт, аудит и правила не разъезжались.
    """
    return _idle_limit_for_kind(cfg, kind, targets)


def _activity_of(d: Daemon, targets: TargetSet | None) -> str:
    """Как этот демон измеряет активность: по описанию цели или по полю демона.

    Описание цели — источник истины; поле `activity` у демона — запасной
    вариант (демон мог быть собран вне сборщика).
    """
    if targets is not None:
        spec = targets.get(d.kind)
        if spec is not None:
            return spec.activity
    return getattr(d, "activity", "") or ""


def _measure(d: Daemon, rec: dict[str, Any], now: float,
             cfg: dict[str, Any],
             targets: TargetSet | None = None) -> Signals:
    """Все измерения по одному демону."""
    # Порог простоя разный для разных видов демонов: у Kotlin нет своего
    # idle-таймаута (его не убирает никто), у Gradle есть — и тот равен
    # 180 мин, поэтому наш порог должен быть выше.
    idle_limit = idle_limit_for_kind(cfg, d.kind, targets)
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

    # «Лог обновлялся недавно» — по тому, как цель измеряет активность:
    # у целей с mtime это время записи в лог, у целей с маркерами — конец
    # последней сборки. Раньше вид был зашит в код, и новая программа
    # молча считалась бы «неактивной», что для kill-режима опасно.
    log_recent = False
    activity = _activity_of(d, targets)
    if activity == ACTIVITY_MTIME:
        log_recent = d.log_mtime is not None and (now - d.log_mtime) <= sweep
    elif activity == ACTIVITY_MARKERS:
        log_recent = (d.last_build_end is not None
                      and (now - d.last_build_end) <= sweep)

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

    # «Зависший» демон: клиент отвалился во время сборки. Работать некому,
    # и работать уже некому. Gradle такой демон не убирает — его idle-таймаут
    # не действует, пока демон считается «в сборке».
    #
    # Признак «завис» держится, только если сборка действительно не
    # завершилась: если после отмены была новая сборка, демон просто
    # простаивает, и его уберёт обычное правило простоя, а не это.
    stuck_build = bool(d.stuck_build and d.build_in_progress
                       and d.canceled_at is not None)
    stuck_age = (now - d.canceled_at) if stuck_build else 0.0

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
        stuck_build=stuck_build,
        stuck_age=stuck_age,
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
            rule=rule.name,
            recreate_cost_seconds=signals.recreate_cost_seconds,
            context=signals.context,
        )
    # Правил не должно быть без catch-all; на всякий случай — оставить.
    return Decision(daemon=signals.daemon,  # type: ignore[arg-type]
                    action=Action.KEEP, reason="правило не сработало — оставляю",
                    idle_seconds=signals.age, protected=True, rule="catch-all")


def build_context(daemons: list[Daemon], now: float,
                  context: Context | None = None) -> Context:
    """Собирает контекст всего набора демонов.

    Контекст — то, что невозможно знать по одному демону: сколько памяти
    доступно системе и идёт ли где-то свежая сборка (по видам демонов).

    `context` можно передать явно — тогда тесты не зависят от того, сколько
    памяти свободно на машине, где они идут. Не передан — берём из системы.
    """
    ctx = replace(context, now=now) if context is not None else Context(now=now)
    if ctx.mem_available_mb is None and context is None:
        ctx.mem_available_mb = mem_available_mb()
    ctx.daemon_count = len(daemons)
    ctx.total_footprint_mb = sum(d.footprint_mb for d in daemons)
    # Пока идёт СВЕЖАЯ сборка — компилятор нужен, и защищаемые виды трогать
    # нельзя. Зависшая сборка (началась давно и не кончилась) защиты не
    # даёт: такой демон сам подлежит уборке.
    ages: dict[str, float] = {}
    for d in daemons:
        if not d.build_in_progress or d.last_build_start is None:
            continue
        age = now - d.last_build_start
        current = ages.get(d.kind)
        if current is None or age < current:
            ages[d.kind] = age
    for kind, age in ages.items():
        known = ctx.build_ages.get(kind)
        if known is None or age < known:
            ctx.build_ages[kind] = age
    return ctx


def classify(daemons: list[Daemon], state: dict[str, Any], now: float,
             cfg: dict[str, Any],
             context: Context | None = None,
             targets: TargetSet | None = None) -> list[Decision]:
    """Решает, кого убивать. Чистая функция: не трогает систему."""
    targets = targets or builtin_targets()
    rules = default_policy(cfg)
    peer_max_age = float(cfg["PEER_MAX_AGE_SECONDS"])
    prev = state.get("daemons", {})

    ctx = build_context(daemons, now, context)

    signals: dict[int, Signals] = {}
    for d in daemons:
        rec = prev.get(str(d.pid)) or {}
        if rec.get("starttime") is not None and rec["starttime"] != d.starttime:
            rec = {}  # PID переиспользован
        sig = _measure(d, rec, now, cfg, targets)
        sig.context = ctx
        sig.recreate_cost_seconds = recreate_cost(cfg, d.kind, targets)
        # Пороги этого вида (targets.yaml) и «пересоздаваем ли демон».
        # Не знаем — не убиваем: «неизвестное ⇒ оставить».
        spec = targets.get(d.kind)
        if spec is not None:
            sig.rebuildable = bool(spec.rebuildable)
            sig.min_footprint = int(spec.min_footprint_mb or 0)
            sig.large_daemon_mb = int(spec.large_daemon_mb or 0)
            sig.stuck_after = float(spec.stuck_after_seconds or 0.0)
            sig.build_active = float(spec.build_active_seconds or 0.0)
        # Кто защищает этот вид демонов, пока сам работает (targets.yaml).
        sig.protector_kinds = targets.protectors_of(d.kind)
        labels = targets.labels()
        sig.protector_labels = [labels.get(k, k) for k in sig.protector_kinds]
        signals[d.pid] = sig

    protection, stale = _peer_protection(signals, peer_max_age)
    for pid, s in signals.items():
        s.peer_protection = protection.get(pid, [])
        s.peer_stale = stale.get(pid, [])

    return [evaluate(signals[d.pid], rules) for d in daemons]
