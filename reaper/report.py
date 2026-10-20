"""Отчёты: человекочитаемая таблица, машинный JSON, вердикты, health."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .config import DEFAULTS, idle_limit_for_kind
from .model import ACTIVITY_MARKERS, Decision
from .policy import default_policy, kill_rule_names
from .state import read_kills
from .targets import TargetSet, builtin_targets

# Версия схемы машинного вывода: потребитель (агент, скрипт) обязан понимать,
# что формат изменился, а не молча получить «поле пропало».
#
# 4 — log_path стал путём относительно домашнего каталога (`~/…`): домашний
#     путь пользователя в машинном выводе не светится.
JSON_SCHEMA_VERSION = 4

# Критерий перехода из dry-run в kill-режим.
# Значения по умолчанию; переопределяются конфигом (PILOT_MIN_EVENTS,
# PILOT_MAX_REUSE_RATE) — чтобы менять планку без правки кода.
PILOT_MAX_REUSE_RATE = 0.60
PILOT_MIN_EVENTS = 8

# Классы демонов и их подписи в отчётах. Основной путь — набор целей
# (targets.yaml); эти константы нужны там, где набора целей нет.
# Правила, которые УБИВАЮТ, перечисляются самой таблицей политики
# (reaper.policy.kill_rule_names): их срабатывание — единственное
# доказательство, что путь «решили убить» работает.

KINDS = ("gradle", "kotlin")
KIND_LABELS = {"gradle": "Gradle", "kotlin": "Kotlin"}


def _targets(targets: TargetSet | None) -> TargetSet:
    return targets if targets is not None else builtin_targets()


def kind_label(labels: dict[str, str], kind: str) -> str:
    """Подпись вида в отчёте: своя подпись цели, иначе её имя.

    Подпись живёт у цели, а вид приходит откуда угодно, поэтому запасной
    вариант (имя) должен быть в одном месте, а не в каждом отчёте.
    """
    return labels.get(kind, kind)


def daemons_by_kind(decisions: list[Decision],
                    target_set: TargetSet) -> dict[str, list[Decision]]:
    """Демоны, разложенные по видам.

    Виды берём из набора целей, а не из самих демонов: у демона неизвестного
    вида нет ни подписи, ни порога простоя, и в сводке по видам ему не
    место. Порядок демонов внутри вида сохраняется.
    """
    grouped: dict[str, list[Decision]] = {kind: [] for kind in target_set.names()}
    for dec in decisions:
        items = grouped.get(dec.daemon.kind)
        if items is not None:
            items.append(dec)
    return grouped


def idle_limits(cfg: dict[str, Any], target_set: TargetSet,
                kinds: list[str] | None = None) -> dict[str, float]:
    """Порог простоя для каждого вида демона.

    У каждого вида своя планка (у Gradle есть свой idle-таймаут, у Kotlin его
    нет), и считать её надо в одном месте: отчёты брали её по-разному,
    и числа в них расходились.

    `kinds` ограничивает круг: отчёт, где вид не показывается, не должен
    платить за его порог.
    """
    names = target_set.names() if kinds is None else list(kinds)
    return {kind: idle_limit_for_kind(cfg, kind, target_set) for kind in names}


def kind_aggregates(decisions: list[Decision], cfg: dict[str, Any],
                    target_set: TargetSet,
                    limits: dict[str, float] | None = None) -> dict[str, Any]:
    """Сводка по видам: сколько демонов, сколько памяти, кого под удар.

    Один сборщик на все отчёты: таблица и JSON показывают одни и те же
    числа, а не две независимые выжимки одного и того же.
    """
    if limits is None:
        limits = idle_limits(cfg, target_set)
    labels = target_set.labels()
    out: dict[str, Any] = {}
    for kind, items in daemons_by_kind(decisions, target_set).items():
        kill = [d for d in items if d.kill]
        out[kind] = {
            "label": kind_label(labels, kind),
            "count": len(items),
            "footprint_mb": sum(d.daemon.footprint_mb for d in items),
            "kill_count": len(kill),
            "kill_footprint_mb": sum(d.daemon.footprint_mb for d in kill),
            "idle_limit_seconds": limits.get(kind, 0.0),
        }
    return out


def kind_summary(decisions: list[Decision], cfg: dict[str, Any],
                 targets: TargetSet | None = None) -> dict[str, Any]:
    """Сводка по классам демонов: сколько, сколько памяти, кого под удар.

    Классы живут по разным правилам (у Gradle есть свой idle-таймаут,
    у Kotlin — нет), поэтому в отчёте они видны отдельными строками.
    """
    return kind_aggregates(decisions, cfg, _targets(targets))


def _stale_note(dec: Decision) -> str:
    if not dec.stale_peers:
        return ""
    detail = "; ".join(p.detail for p in dec.stale_peers)
    return f" [зависший клиент: {detail}]"


def format_status(decisions: list[Decision], state: dict[str, Any],
                  cfg: dict[str, Any] | None = None,
                  targets: TargetSet | None = None) -> str:
    cfg = cfg if cfg is not None else {}
    target_set = _targets(targets)
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
    available = state.get("mem_available_mb")
    if available is not None:
        lines.append(f"свободной памяти: {available} МБ "
                     f"(порог давления {cfg.get('MEMORY_PRESSURE_MB')} МБ)")
    # Сводка по классам: у них разные пороги простоя и разная судьба.
    # Пустой конфиг — это «порогов нет»: строка про порог не печатается.
    by_kind = daemons_by_kind(decisions, target_set)
    limits = idle_limits(cfg, target_set,
                         [kind for kind, items in by_kind.items() if items]) if cfg else {}
    for kind, agg in kind_aggregates(decisions, cfg, target_set, limits).items():
        if not agg["count"]:
            continue
        limit = agg["idle_limit_seconds"]
        limit_txt = f", порог простоя {limit / 3600:.0f} ч" if limit else ""
        lines.append(f"  {agg['label']}: {agg['count']} шт, "
                     f"{agg['footprint_mb']} МБ, "
                     f"под убийство {agg['kill_footprint_mb']} МБ"
                     f" ({agg['kill_count']} шт){limit_txt}")
    metric = state.get("metric", {})
    if metric:
        counted = int(metric.get("counted", metric.get("events", 0)))
        lines.append(f"метрика dry-run: наблюдённых событий {counted}, "
                     f"повторно нужны {metric.get('reused', 0)} "
                     f"({100 * metric.get('rate', 0):.0f} %), в ожидании {metric.get('pending', 0)}")
        unknown = int(metric.get("unknown", 0))
        killed = int(metric.get("killed", 0))
        if unknown or killed:
            lines.append(f"  не засчитано: наблюдение прерывалось {unknown}, "
                         f"демон убит {killed} — вне знаменателя")
    hits = state.get("rule_hits_last") or {}
    if hits:
        top = ", ".join(f"{name} ×{count}" for name, count in
                        sorted(hits.items(), key=lambda kv: -kv[1]))
        lines.append(f"правила за последний свип: {top}")
    for w in state.get("degradation", []) or []:
        lines.append(f"ВНИМАНИЕ: {w}")
    freed = int(state.get("freed_mb", 0))
    would = int(state.get("would_free_mb", 0))
    if freed or would:
        lines.append(f"память: освобождено {freed} МБ, могло бы быть освобождено {would} МБ")
    stale = int(state.get("stale_peer_events", 0))
    if stale:
        lines.append(f"зависших клиентов снято защитой: {stale}")
    lines.extend(_coverage_lines(state, cfg))
    return "\n".join(lines)


def redact_home(path: str | None) -> str | None:
    """Путь без домашнего каталога: `/home/max/x` → `~/x`.

    Приватность машинного вывода: домашний путь раскрывает имя пользователя
    и раскладку его рабочих каталогов, а в отчёте он не нужен — нужен сам
    путь. Поэтому путь остаётся (видно, где лежит лог), но без домашнего
    каталога. Внутренние пути и UID в вывод не попадают.
    """
    if not path:
        return path
    home = os.path.expanduser("~")
    if home and home not in ("/", "") and path.startswith(home):
        return "~" + path[len(home):]
    return path


def rule_coverage(state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Покрытие правил: что срабатывало, а что ни разу.

    Правило, которое не сработало НИ РАЗУ, оставляет свой порог
    непроверенным: значение стоит, но никто не знает, верное ли оно.
    Особенно это касается kill-правил: пока ни одно из них не сработало,
    сам путь «решили убить» на живых демонах не проверен, а пилот не может
    набрать событий — их не из чего набрать.

    Счётчики берутся из состояния (накопительные), а не из журнала
    свипов: состояние живёт рядом с рейпером и не теряется.
    """
    hits = {name: int(count or 0)
            for name, count in (state.get("rule_hits") or {}).items()}
    # Имена правил и список убивающих берутся из таблицы политики: она и есть
    # источник истины. Пороги подставляются из значений по умолчанию, чтобы
    # список правил получался всегда, даже если конфиг не задан.
    policy_cfg = {**DEFAULTS, **cfg}
    all_rules = [rule.name for rule in default_policy(policy_cfg)]
    kill_rules = kill_rule_names(policy_cfg)
    fired = {name: hits[name] for name in all_rules if hits.get(name)}
    never = [name for name in all_rules if not hits.get(name)]
    kill_fired = {name: count for name, count in fired.items()
                  if name in kill_rules}
    kill_never = [name for name in kill_rules if not hits.get(name)]
    return {
        "hits": fired,
        "never_fired": never,
        "kill_fired": kill_fired,
        "kill_never_fired": kill_never,
    }


def _coverage_lines(state: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """Строки о покрытии правил — для отчётов.

    Различаем два случая, потому что они означают разное:
      * kill-правило не сработало ни разу — его порог НЕ ПРОВЕРЕН, и пилоту
        не из чего набирать события;
      * защитное правило не сработало — это не беда: значит, защищать было
        некого (демоны либо не простаивали, либо не были заметены).
    """
    coverage = rule_coverage(state, cfg)
    fired = coverage["hits"]
    if not fired and not coverage["never_fired"]:
        return []
    lines = []
    if fired:
        parts = ", ".join(f"{name} ×{count}" for name, count in
                          sorted(fired.items(), key=lambda kv: -kv[1]))
        lines.append(f"покрытие правил: сработали {parts}")
    if coverage["kill_never_fired"]:
        lines.append("  kill-правила НИ РАЗУ не сработали: "
                     + ", ".join(coverage["kill_never_fired"])
                     + " → путь «убить» на живых демонах не проверен, "
                       "пилоту не из чего набирать события")
    protective = [name for name in coverage["never_fired"]
                  if name not in coverage["kill_never_fired"]
                  and name not in coverage["kill_fired"]]
    if protective:
        lines.append("  защитные правила ни разу не сработали: "
                     + ", ".join(protective)
                     + " (защищать было некого)")
    return lines


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
        "log_path": redact_home(d.log_path),
        "stuck_build": bool(d.stuck_build),
        "canceled_at": d.canceled_at,
        "recreate_cost_seconds": dec.recreate_cost_seconds,
        "rule": dec.rule,
    }


def format_json(decisions: list[Decision], state: dict[str, Any],
                cfg: dict[str, Any], report: list[dict[str, Any]] | None = None,
                now: float | None = None, extra: dict[str, Any] | None = None,
                targets: TargetSet | None = None,
                ) -> str:
    now = time.time() if now is None else now
    target_set = _targets(targets)
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
        "targets": {
            name: {
                "label": kind_label(target_set.labels(), name),
                "idle_limit_seconds": spec.idle_seconds,
                "recreate_cost_seconds": spec.recreate_cost_seconds,
                "activity": spec.activity,
                "protects": list(spec.protects),
                "rebuildable": spec.rebuildable,
            }
            for name, spec in target_set.specs_by_name().items()
        },
        "kinds": kind_summary(decisions, cfg, target_set),
        "memory": {
            "available_mb": state.get("mem_available_mb"),
            "pressure_mb": cfg.get("MEMORY_PRESSURE_MB"),
            "large_daemon_mb": cfg.get("KILL_LARGE_DAEMON_MB"),
        },
        "daemons": [decision_to_dict(d) for d in decisions],
        "metric": state.get("metric", {}),
        "rule_hits": state.get("rule_hits", {}),
        "rule_hits_last": state.get("rule_hits_last", {}),
        "measured_count": state.get("measured_count", 0),
        "degradation": state.get("degradation", []),
        "freed_mb": state.get("freed_mb", 0),
        "would_free_mb": state.get("would_free_mb", 0),
        "would_free_pending_mb": state.get("would_free_pending_mb", 0),
        "last_sweep_at": state.get("last_sweep_at", 0.0),
        "pilot_started_at": state.get("pilot_started_at", 0.0),
        "stale_peer_events": state.get("stale_peer_events", 0),
        "stale_peer_daemons": state.get("stale_peer_daemons", {}),
        "actions": report or [],
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True)


def format_explain(cfg: dict[str, Any],
                   targets: TargetSet | None = None) -> str:
    """Печатает таблицу правил политики — документация и отладочный инструмент."""
    rules = default_policy(cfg)
    target_set = _targets(targets)
    labels = target_set.labels()
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
    idle_by_kind = idle_limits(cfg, target_set)
    limits = []
    for name, spec in target_set.specs_by_name().items():
        limit = idle_by_kind[name]
        notes = []
        if spec.self_timeout_seconds:
            notes.append(f"свой idle-таймаут {spec.self_timeout_seconds / 60:.0f} мин")
        if spec.protects:
            notes.append("защищает: "
                         + ", ".join(kind_label(labels, k) for k in spec.protects))
        suffix = f" ({'; '.join(notes)})" if notes else ""
        limits.append(f"{kind_label(labels, name)} {limit / 60:.0f} мин{suffix}")
    lines.append("Порог простоя: " + "; ".join(limits) + ";")
    lines.append(f"минимум памяти: {cfg['MIN_FOOTPRINT_MB']} МБ; "
                 f"лимит убийств за свип: {cfg.get('MAX_KILLS_PER_SWEEP')}; "
                 f"возраст «зависшего клиента»: "
                 f"{int(cfg.get('PEER_MAX_AGE_SECONDS', 0)) / 3600:.0f} ч")
    pressure = int(cfg.get("MEMORY_PRESSURE_MB", 0) or 0)
    pressure_txt = ("«всегда убирать»" if pressure <= 0
                    else f"ниже {pressure} МБ свободной памяти")
    lines.append(f"давление по памяти: убираем, когда {pressure_txt}; "
                 f"крупные демоны (≥ {cfg.get('KILL_LARGE_DAEMON_MB')} МБ) — "
                 f"всегда")
    costs = ", ".join(f"{kind_label(labels, name)} "
                      f"{spec.recreate_cost_seconds:.0f} с"
                      for name, spec in target_set.specs_by_name().items()
                      if spec.recreate_cost_seconds)
    lines.append(f"цена воссоздания: {costs} (влияет на порядок уборки)")

    # Описание целей по секциям: так видно, что цель умеет и по каким
    # правилам живёт, — ровно то, что нужно проверить после правки YAML.
    lines.append("")
    lines.append("Цели:")
    for name, spec in target_set.specs_by_name().items():
        lines.append(f"  {kind_label(labels, name)} ({name}):")
        match = []
        if spec.match_cmdline:
            match.append(f"cmdline «{spec.match_cmdline}»")
        if spec.match_comm:
            match.append(f"команда «{spec.match_comm}»")
        lines.append("    узнаём по: " + (", ".join(match) if match else "—"))
        log_bits = []
        if spec.log_name_regex:
            log_bits.append(f"имя «{spec.log_name_regex}»")
        if spec.log_globs:
            log_bits.append(f"{len(spec.log_globs)} шаблон(ов) путей")
        if spec.log_pid_regex:
            log_bits.append("PID из имени файла")
        if spec.log_group_regex:
            log_bits.append("группировка по инстансу")
        if spec.forbidden_prefixes:
            log_bits.append(f"{len(spec.forbidden_prefixes)} неприкосновенных префикс(ов)")
        lines.append("    логи: " + (", ".join(log_bits) if log_bits else "своих логов нет"))
        if spec.activity == ACTIVITY_MARKERS:
            work = f"маркеры работы (начало «{spec.build_start or '—'}»)"
        else:
            work = f"активность по mtime лога, формат {spec.ts_format}"
        lines.append("    работа: " + work)
        safety = []
        safety.append("пересоздаваемый" if spec.rebuildable
                      else "НЕ пересоздаваемый (убивать нельзя)")
        if spec.protects:
            safety.append("защищает: "
                          + ", ".join(kind_label(labels, k) for k in spec.protects))
        lines.append("    безопасность: " + ", ".join(safety))
    return "\n".join(lines)


def _kind_verdicts(by_kind: dict[str, Any], min_events: int,
                   max_rate: float) -> list[dict[str, Any]]:
    """Вердикт по каждому виду: у вида своя цена ошибки.

    Общая цифра смешала бы «ложное срабатывание на 40-секундном Gradle» с
    «ложным срабатыванием на 2-секундном демоне», поэтому вердикт выносится
    по видам. Вид, у которого событий мало, своего вердикта не получает
    (данных недостаточно).
    """
    verdicts: list[dict[str, Any]] = []
    for kind, data in sorted(by_kind.items()):
        kind_counted = int(data.get("counted", 0))
        kind_rate = float(data.get("rate", 0.0))
        if kind_counted < min_events:
            kind_code = 2
        elif kind_rate > max_rate:
            kind_code = 1
        else:
            kind_code = 0
        verdicts.append({"kind": kind, "counted": kind_counted,
                         "reused": int(data.get("reused", 0)),
                         "rate": kind_rate,
                         "rate_weighted": float(data.get("rate_weighted", 0.0)),
                         "cost_reused_seconds": float(
                             data.get("cost_reused_seconds", 0.0)),
                         "verdict_code": kind_code})
    return verdicts


def pilot_report(cfg: dict[str, Any], state: dict[str, Any],
                 now: float | None = None) -> tuple[str, int]:
    """Вердикт по пилоту: можно ли включать kill-режим.

    Критерий: доля «понадобился снова в течение REUSE_WINDOW_SECONDS» не выше
    PILOT_MAX_REUSE_RATE, при этом набрано не меньше PILOT_MIN_EVENTS событий
    (иначе выборка слишком мала и вердикт не считается).
    """
    now = time.time() if now is None else now
    metric = state.get("metric", {}) or {}
    # В знаменатель попадают только события, которые мы наблюдали целиком:
    # «unknown» (наблюдение прерывалось) и «killed» (демон убит) вопроса
    # «понадобился ли» не имеют.
    counted = int(metric.get("counted", metric.get("events", 0)))
    reused = int(metric.get("reused", 0))
    not_reused = int(metric.get("not_reused", 0))
    unknown = int(metric.get("unknown", 0))
    killed = int(metric.get("killed", 0))
    pending = int(metric.get("pending", 0))
    rate = float(metric.get("rate", 0.0))
    started = float(state.get("pilot_started_at", 0.0))
    days = (now - started) / 86400.0 if started else 0.0
    min_events = int(cfg.get("PILOT_MIN_EVENTS", PILOT_MIN_EVENTS))
    max_rate = float(cfg.get("PILOT_MAX_REUSE_RATE", PILOT_MAX_REUSE_RATE))

    # Вердикт — по видам: у каждого своя цена ошибки, и общая цифра смешала
    # бы «ложное срабатывание на 40-секундном Gradle» с «ложным
    # срабатыванием на 2-секундном демоне». Вид, у которого событий мало,
    # своего вердикта не получает (данных недостаточно).
    by_kind = metric.get("by_kind") or {}
    kind_verdicts = _kind_verdicts(by_kind, min_events, max_rate)
    judged = [v for v in kind_verdicts if v["verdict_code"] != 2]
    coverage = rule_coverage(state, cfg)
    kill_never = coverage.get("kill_never_fired") or []
    # Если kill-правила не сработали ни разу, то событий «would kill» не будет:
    # пилоту не из чего набирать выборку. Это не «мало данных» — это «данные не
    # могут появиться»: ждать их нужно не временем, а самим зависшим демоном.
    no_events_reason = (
        f"kill-правила ({', '.join(kill_never)}) не сработали ни разу — путь "
        f"«убить» на живых демонах не проверен, и пилоту не из чего набирать "
        f"события: кандидатов на убийство не было ни разу. "
        f"Kill-режим включать рано."
        if kill_never and counted == 0 else "")
    if not kind_verdicts and counted:
        # Метрика старого формата (без разбивки по видам) — вердикт как раньше,
        # по общей цифре. Так читаются состояния, набранные до появления целей.
        if counted < min_events:
            verdict = (f"ДАННЫХ МАЛО: наблюдённых событий {counted} из нужных "
                       f"{min_events} — вердикт не выносим")
            code = 2
        elif rate > max_rate:
            verdict = (f"НЕ ВКЛЮЧАТЬ: доля ложных срабатываний "
                       f"{100 * rate:.0f} % > {100 * max_rate:.0f} %")
            code = 1
        else:
            verdict = (f"МОЖНО ВКЛЮЧАТЬ: доля ложных срабатываний "
                       f"{100 * rate:.0f} % ≤ {100 * max_rate:.0f} %, "
                       f"наблюдённых событий {counted}")
            code = 0
    elif not judged:
        verdict = (f"ДАННЫХ МАЛО: ни у одного вида не набралось {min_events} "
                   f"наблюдённых событий — вердикт не выносим")
        if no_events_reason:
            verdict += ". " + no_events_reason
        code = 2
    elif any(v["verdict_code"] == 1 for v in judged):
        worst = next(v for v in judged if v["verdict_code"] == 1)
        verdict = (f"НЕ ВКЛЮЧАТЬ: у вида «{worst['kind']}» доля ложных "
                   f"срабатываний {100 * worst['rate']:.0f} % > "
                   f"{100 * max_rate:.0f} %")
        code = 1
    else:
        verdict = (f"МОЖНО ВКЛЮЧАТЬ: у всех видов с достаточной выборкой доля "
                   f"ложных ≤ {100 * max_rate:.0f} %")
        code = 0

    kills = read_kills(cfg, limit=0)
    killed_mb = sum(int(k.get("footprint_mb", 0)) for k in kills)
    lines = [
        "Пилот java-reaper",
        "=" * 60,
        f"идёт: {days:.1f} сут (с {time.strftime('%d.%m %H:%M', time.localtime(started))})"
        if started else "идёт: неизвестно (нет отметки старта)",
        f"наблюдённых событий «would kill»: {counted} (в ожидании ещё {pending})",
        f"из них понадобились снова: {reused} ({100 * rate:.0f} %)"
        f" — базис по ретроспективе 47 %",
        f"не понадобились: {not_reused}",
        f"вне знаменателя: наблюдение прерывалось {unknown}, демон убит {killed}",
        f"критерий: доля ≤ {100 * max_rate:.0f} % при ≥ {min_events} "
        f"наблюдённых событиях",
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
    ]
    if kind_verdicts:
        lines.append("По видам демонов (у каждого своя цена ошибки):")
        for item in kind_verdicts:
            mark = {0: "ок", 1: "ПЛОХО", 2: "мало данных"}.get(item["verdict_code"])
            lines.append(
                f"  {item['kind']}: событий {item['counted']}, ложных "
                f"{100 * item['rate']:.0f} % (взвешенно по цене "
                f"{100 * item['rate_weighted']:.0f} %), цена ложных "
                f"{item['cost_reused_seconds']:.0f} с — {mark}")
        lines.append("")
    lines.extend(_coverage_lines(state, cfg))
    if lines[-1] != "":
        lines.append("")
    lines.append(f"ВЕРДИКТ: {verdict}")
    return "\n".join(lines), code


def audit_report(cfg: dict[str, Any], decisions: list[Decision],
                 state: dict[str, Any],
                 targets: TargetSet | None = None) -> tuple[str, int]:
    """Критерий приёмки из плана: демонов с простоями длиннее порога
    не должно остаться. Ноль — цель, неноль — недоработанный рейпер.

    Порог свой у каждого класса (Gradle / Kotlin), поэтому сравнение идёт
    с порогом того демона, а не с одним общим числом.
    """
    target_set = _targets(targets)
    labels = target_set.labels()
    limits = idle_limits(cfg, target_set)

    def limit_of(kind: str) -> float:
        """Порог простоя вида: у известных — из набора целей, у прочих — из конфига."""
        if kind in limits:
            return limits[kind]
        return idle_limit_for_kind(cfg, kind, target_set)

    stale = [dec for dec in decisions
             if dec.idle_seconds is not None
             and dec.idle_seconds > limit_of(dec.daemon.kind)]
    stale.sort(key=lambda d: -d.daemon.footprint_mb)
    total = sum(d.daemon.footprint_mb for d in stale)
    limits_txt = ", ".join(f"{kind_label(labels, k)} {limit_of(k) / 3600:.0f} ч"
                           for k in target_set.names())
    lines = [f"Аудит: демоны с простоями длиннее порога ({limits_txt})", "=" * 60]
    if not stale:
        lines.append("  таких демонов нет — критерий приёмки выполнен (footprint = 0)")
        return "\n".join(lines), 0
    for dec in stale:
        d = dec.daemon
        lines.append(f"  pid={d.pid:>8} {d.kind:>7} {d.footprint_mb:>6} МБ  "
                     f"простой {dec.idle_seconds / 3600:>6.1f} ч  {dec.reason}")
    lines.append(f"  итого: {len(stale)} демонов, {total} МБ — критерий приёмки НЕ выполнен")
    return "\n".join(lines), 1


def canary(daemons: list[Any], decisions: list[Decision],
           targets: TargetSet | None = None) -> str | None:
    """Предупреждение: детектор молча перестал видеть демонов.

    Проверяем по целям: если на машине есть живой процесс, который подходит
    под какую-то цель, а в списке демонов его нет — детектор этой цели
    ослеп. Раньше проверка искала в cmdline слова «gradle»/«kotlin» — то
    есть о новых целях она молчала, и «кандидатов 0» выглядело бы как
    «нечего было убивать».
    """
    if daemons:
        return None
    from .proc import proc_comm, proc_cmdline, read_proc_stat

    target_set = _targets(targets)
    labels = target_set.labels()
    known = {getattr(d, "pid", None) for d in daemons}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in known:
            continue
        cmdline = proc_cmdline(pid)
        if not cmdline:
            continue
        spec = target_set.match(cmdline, proc_comm(pid))
        if spec is None:
            continue
        if read_proc_stat(pid) is None:
            continue  # процесс успел умереть — не сбой детектора
        return (f"ВНИМАНИЕ: кандидатов 0, но есть живой процесс цели "
                f"«{kind_label(labels, spec.name)}» (pid {pid}) — "
                f"детектор её не увидел: проверьте match_cmdline и формат лога")
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
        lines.append(f"java-reaper: метрика: наблюдённых событий "
                     f"{metric.get('counted', metric.get('events', 0))}, "
                     f"повторно нужны {metric.get('reused', 0)} "
                     f"({100 * metric.get('rate', 0):.0f} %), "
                     f"вне знаменателя {metric.get('unknown', 0)} + "
                     f"{metric.get('killed', 0)}")
    for w in state.get("degradation", []) or []:
        lines.append(f"java-reaper: ВНИМАНИЕ: {w}")
    if age is None:
        lines.append("java-reaper: СТАТУС: свипов не было — таймер не запущен "
                     "или рейпер ещё ни разу не отработал")
        return "\n".join(lines), 1
    if age > 2 * sweep:
        lines.append(f"java-reaper: СТАТУС: свип старше двух интервалов "
                     f"({age / 60:.1f} мин) — таймер молчит")
        return "\n".join(lines), 1
    if state.get("degradation"):
        lines.append("java-reaper: СТАТУС: деградация детекции")
        return "\n".join(lines), 1
    lines.append("java-reaper: СТАТУС: ок")
    return "\n".join(lines), 0
