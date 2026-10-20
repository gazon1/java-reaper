"""Отчёты: человекочитаемая таблица, машинный JSON, вердикты, health."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .config import idle_limit as idle_limit_for
from .model import Decision
from .policy import default_policy
from .state import read_kills

# Версия схемы машинного вывода: потребитель (агент, скрипт) обязан понимать,
# что формат изменился, а не молча получить «поле пропало».
JSON_SCHEMA_VERSION = 2

# Критерий перехода из dry-run в kill-режим.
# Значения по умолчанию; переопределяются конфигом (PILOT_MIN_EVENTS,
# PILOT_MAX_REUSE_RATE) — чтобы менять планку без правки кода.
PILOT_MAX_REUSE_RATE = 0.60
PILOT_MIN_EVENTS = 8

# Классы демонов и их подписи в отчётах.
KINDS = ("gradle", "kotlin")
KIND_LABELS = {"gradle": "Gradle", "kotlin": "Kotlin"}


def kind_summary(decisions: list[Decision], cfg: dict[str, Any]) -> dict[str, Any]:
    """Сводка по классам демонов: сколько, сколько памяти, кого под удар.

    Классы живут по разным правилам (у Gradle есть свой idle-таймаут,
    у Kotlin — нет), поэтому в отчёте они видны отдельными строками.
    """
    out: dict[str, Any] = {}
    for kind in KINDS:
        items = [d for d in decisions if d.daemon.kind == kind]
        kill = [d for d in items if d.kill]
        out[kind] = {
            "label": KIND_LABELS[kind],
            "count": len(items),
            "footprint_mb": sum(d.daemon.footprint_mb for d in items),
            "kill_count": len(kill),
            "kill_footprint_mb": sum(d.daemon.footprint_mb for d in kill),
            "idle_limit_seconds": idle_limit_for(cfg, kind),
        }
    return out


def _stale_note(dec: Decision) -> str:
    if not dec.stale_peers:
        return ""
    detail = "; ".join(p.detail for p in dec.stale_peers)
    return f" [зависший клиент: {detail}]"


def format_status(decisions: list[Decision], state: dict[str, Any],
                  cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg if cfg is not None else {}
    lines = [f"{'PID':>8} {'класс':>7} {'footprint':>10} {'простой':>9}  решение / причина"]
    lines.append("-" * 96)
    for dec in sorted(decisions, key=lambda d: -d.daemon.footprint_mb):
        d = dec.daemon
        idle = f"{dec.idle_seconds / 60:.0f} мин" if dec.idle_seconds is not None else "?"
        verdict = "УБИТЬ" if dec.kill else "оставить"
        lines.append(f"{d.pid:>8} {d.kind:>7} {d.footprint_mb:>7} МБ {idle:>9}  "
                     f"{verdict}: {dec.reason}{_stale_note(dec)}")
    total = sum(d.daemon.footprint_mb for d in decisions)
    idle_total = sum(d.daemon.footprint_mb for d in decisions if d.kill)
    lines.append("-" * 96)
    lines.append(f"всего демонов: {len(decisions)}, footprint: {total} МБ, "
                 f"под убийство: {idle_total} МБ")
    # Сводка по классам: у них разные пороги простоя и разная судьба.
    for kind in KINDS:
        items = [d for d in decisions if d.daemon.kind == kind]
        if not items:
            continue
        kill = [d for d in items if d.kill]
        limit = idle_limit_for(cfg, kind) if cfg else 0.0
        limit_txt = f", порог простоя {limit / 3600:.0f} ч" if limit else ""
        lines.append(f"  {KIND_LABELS[kind]}: {len(items)} шт, "
                     f"{sum(d.daemon.footprint_mb for d in items)} МБ, "
                     f"под убийство {sum(d.daemon.footprint_mb for d in kill)} МБ"
                     f" ({len(kill)} шт){limit_txt}")
    metric = state.get("metric", {})
    if metric:
        lines.append(f"метрика dry-run: событий {metric.get('events', 0)}, "
                     f"повторно нужны {metric.get('reused', 0)} "
                     f"({100 * metric.get('rate', 0):.0f} %), в ожидании {metric.get('pending', 0)}")
    freed = int(state.get("freed_mb", 0))
    would = int(state.get("would_free_mb", 0))
    if freed or would:
        lines.append(f"память: освобождено {freed} МБ, могло бы быть освобождено {would} МБ")
    stale = int(state.get("stale_peer_events", 0))
    if stale:
        lines.append(f"зависших клиентов снято защитой: {stale}")
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
        "stale_peers": [
            {"kind": p.kind, "detail": p.detail, "pid": p.pid, "name": p.name}
            for p in dec.stale_peers
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
        "stuck_build": bool(d.stuck_build),
        "canceled_at": d.canceled_at,
    }


def format_json(decisions: list[Decision], state: dict[str, Any],
                cfg: dict[str, Any], report: list[dict[str, Any]] | None = None,
                now: float | None = None, extra: dict[str, Any] | None = None,
                ) -> str:
    now = time.time() if now is None else now
    payload = {
        "schema_version": JSON_SCHEMA_VERSION,
        "now": now,
        "config": {
            "IDLE_SECONDS": cfg["IDLE_SECONDS"],
            "KOTLIN_IDLE_SECONDS": cfg.get("KOTLIN_IDLE_SECONDS"),
            "MIN_FOOTPRINT_MB": cfg["MIN_FOOTPRINT_MB"],
            "BUSY_CPU_SECONDS": cfg["BUSY_CPU_SECONDS"],
            "SWEEP_SECONDS": cfg["SWEEP_SECONDS"],
            "MAX_KILLS_PER_SWEEP": cfg.get("MAX_KILLS_PER_SWEEP"),
            "PEER_MAX_AGE_SECONDS": cfg.get("PEER_MAX_AGE_SECONDS"),
            "PID_ALLOWLIST": cfg.get("PID_ALLOWLIST", ""),
        },
        "kinds": kind_summary(decisions, cfg),
        "daemons": [decision_to_dict(d) for d in decisions],
        "metric": state.get("metric", {}),
        "freed_mb": state.get("freed_mb", 0),
        "would_free_mb": state.get("would_free_mb", 0),
        "last_sweep_at": state.get("last_sweep_at", 0.0),
        "pilot_started_at": state.get("pilot_started_at", 0.0),
        "stale_peer_events": state.get("stale_peer_events", 0),
        "stale_peer_daemons": state.get("stale_peer_daemons", {}),
        "actions": report or [],
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True)


def format_explain(cfg: dict[str, Any]) -> str:
    """Печатает таблицу правил политики — документация и отладочный инструмент."""
    rules = default_policy(cfg)
    lines = ["Правила политики (в порядке приоритета; срабатывает первое подходящее):", ""]
    for i, rule in enumerate(rules, 1):
        lines.append(f"{i:>2}. [{rule.action.value}] {rule.name}")
        lines.append(f"    что:    {rule.doc}")
        lines.append(f"    условие: {rule.condition}")
        if rule.busy:
            lines.append("    следствие: демон считается занятым")
        if rule.protected:
            lines.append("    следствие: демон считается защищённым")
    lines.append("")
    gradle_limit = idle_limit_for(cfg, "gradle")
    kotlin_limit = idle_limit_for(cfg, "kotlin")
    lines.append(f"Порог простоя: Gradle {gradle_limit / 60:.0f} мин "
                 f"(выше его собственного idle-таймаута в 180 мин), "
                 f"Kotlin {kotlin_limit / 60:.0f} мин (своего таймаута нет — "
                 f"его не убирает никто);")
    lines.append(f"минимум памяти: {cfg['MIN_FOOTPRINT_MB']} МБ; "
                 f"лимит убийств за свип: {cfg.get('MAX_KILLS_PER_SWEEP')}; "
                 f"возраст «зависшего клиента»: "
                 f"{int(cfg.get('PEER_MAX_AGE_SECONDS', 0)) / 3600:.0f} ч")
    return "\n".join(lines)


def pilot_report(cfg: dict[str, Any], state: dict[str, Any],
                 now: float | None = None) -> tuple[str, int]:
    """Вердикт по пилоту: можно ли включать kill-режим.

    Критерий: доля «понадобился снова в течение REUSE_WINDOW_SECONDS» не выше
    PILOT_MAX_REUSE_RATE, при этом набрано не меньше PILOT_MIN_EVENTS событий
    (иначе выборка слишком мала и вердикт не считается).
    """
    now = time.time() if now is None else now
    metric = state.get("metric", {}) or {}
    events = int(metric.get("events", 0))
    reused = int(metric.get("reused", 0))
    pending = int(metric.get("pending", 0))
    rate = float(metric.get("rate", 0.0))
    started = float(state.get("pilot_started_at", 0.0))
    days = (now - started) / 86400.0 if started else 0.0
    min_events = int(cfg.get("PILOT_MIN_EVENTS", PILOT_MIN_EVENTS))
    max_rate = float(cfg.get("PILOT_MAX_REUSE_RATE", PILOT_MAX_REUSE_RATE))

    if events < min_events:
        verdict = (f"ДАННЫХ МАЛО: событий {events} из нужных {min_events} — "
                   f"вердикт не выносим")
        code = 2
    elif rate > max_rate:
        verdict = (f"НЕ ВКЛЮЧАТЬ: доля ложных срабатываний "
                   f"{100 * rate:.0f} % > {100 * max_rate:.0f} %")
        code = 1
    else:
        verdict = (f"МОЖНО ВКЛЮЧАТЬ: доля ложных срабатываний {100 * rate:.0f} % "
                   f"≤ {100 * max_rate:.0f} %, событий {events}")
        code = 0

    kills = read_kills(cfg, limit=0)
    killed_mb = sum(int(k.get("footprint_mb", 0)) for k in kills)
    lines = [
        "Пилот java-reaper",
        "=" * 60,
        f"идёт: {days:.1f} сут (с {time.strftime('%d.%m %H:%M', time.localtime(started))})"
        if started else "идёт: неизвестно (нет отметки старта)",
        f"событий «would kill»: {events} (в ожидании ещё {pending})",
        f"из них понадобились снова: {reused} ({100 * rate:.0f} %)"
        f" — базис по ретроспективе 47 %",
        f"критерий: доля ≤ {100 * max_rate:.0f} % при ≥ {min_events} событиях",
        "",
        "Эффект:",
        f"  освобождено памяти (kill-режим): {int(state.get('freed_mb', 0))} МБ",
        f"  могло бы быть освобождено (dry-run): {int(state.get('would_free_mb', 0))} МБ",
        f"  записей об убийствах: {len(kills)} ({killed_mb} МБ)",
        "",
        "Наблюдаемость правил:",
        f"  «зависший клиент» снял защиту: {int(state.get('stale_peer_events', 0))} раз",
        f"  демонов с зависшим клиентом: {len(state.get('stale_peer_daemons', {}))}",
        "",
        f"ВЕРДИКТ: {verdict}",
    ]
    return "\n".join(lines), code


def audit_report(cfg: dict[str, Any], decisions: list[Decision],
                 state: dict[str, Any]) -> tuple[str, int]:
    """Критерий приёмки из плана: демонов с простоями длиннее порога
    не должно остаться. Ноль — цель, неноль — недоработанный рейпер.

    Порог свой у каждого класса (Gradle / Kotlin), поэтому сравнение идёт
    с порогом того демона, а не с одним общим числом.
    """
    stale = [dec for dec in decisions
             if dec.idle_seconds is not None
             and dec.idle_seconds > idle_limit_for(cfg, dec.daemon.kind)]
    stale.sort(key=lambda d: -d.daemon.footprint_mb)
    total = sum(dec.daemon.footprint_mb for dec in stale)
    limits = ", ".join(f"{KIND_LABELS[k]} {idle_limit_for(cfg, k) / 3600:.0f} ч"
                       for k in KINDS)
    lines = [f"Аудит: демоны с простоями длиннее порога ({limits})", "=" * 60]
    if not stale:
        lines.append("  таких демонов нет — критерий приёмки выполнен (footprint = 0)")
        return "\n".join(lines), 0
    for dec in stale:
        d = dec.daemon
        lines.append(f"  pid={d.pid:>8} {d.kind:>7} {d.footprint_mb:>6} МБ  "
                     f"простой {dec.idle_seconds / 3600:>6.1f} ч  {dec.reason}")
    lines.append(f"  итого: {len(stale)} демонов, {total} МБ — критерий приёмки НЕ выполнен")
    return "\n".join(lines), 1


def canary(daemons: list[Any], decisions: list[Decision]) -> str | None:
    """Предупреждение: детектор молча перестал видеть демонов."""
    if daemons:
        return None
    from .proc import proc_cmdline
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
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
