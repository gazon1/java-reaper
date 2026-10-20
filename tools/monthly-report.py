#!/usr/bin/env python3
"""Месячный отчёт по java-reaper: что рейпер сделал и не навредил.

Отвечает на вопросы, ради которых рейпер вообще существует:

  * сколько памяти освобождено (и сколько «могло бы быть» в dry-run);
  * сколько демонов убито и по каким правилам;
  * сколько было ложных срабатываний — демон убили, а через пару часов
    он понадобился снова (доля reuse из метрики пилота);
  * сколько раз правило «зависший клиент» снимало защиту — насколько это
    правило вообще работает.

Источники данных:
  * state.json    — метрика пилота (события «would kill» и повторные
                    обращения), счётчики правил, освобождённая память;
  * kills.jsonl   — журнал фактических убийств (по одной JSON-записи на
                    убийство).

Использование:
    python3 tools/monthly-report.py [--days 30] [--json]

По умолчанию отчёт за последние 30 дней. Данные берутся из state-dir
конфигурации рейпера.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from reaper.config import load_config, resolve_targets  # noqa: E402
from reaper.state import load_state, read_kills, sweeps_path  # noqa: E402
from reaper.targets import load_targets  # noqa: E402


def read_sweeps(cfg: dict, since: float) -> list[dict]:
    """Читает журнал свипов за период — это ИСТОРИЯ решений."""
    out: list[dict] = []
    try:
        with open(sweeps_path(cfg), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if float(rec.get("at", 0)) >= since:
                    out.append(rec)
    except OSError:
        return []
    return out


def period_days(days: int) -> float:
    return days * 86400.0


def collect(cfg: dict, days: int, targets=None) -> dict:
    """Собирает цифры отчёта."""
    state = load_state(cfg)
    now = time.time()
    since = now - period_days(days)

    kills = read_kills(cfg, limit=0)
    kills_in_period = [k for k in kills if float(k.get("at", 0)) >= since]

    by_action: dict[str, int] = {}
    killed_mb = 0
    by_reason: dict[str, int] = {}
    for k in kills_in_period:
        action = str(k.get("action", "?"))
        by_action[action] = by_action.get(action, 0) + 1
        killed_mb += int(k.get("footprint_mb", 0))
        reason = str(k.get("reason", "?"))
        # Причина длинная: группируем по первому слову-признаку.
        key = reason.split(",")[0].split("(")[0].strip() or "?"
        by_reason[key] = by_reason.get(key, 0) + 1

    # Журнал свипов: сколько памяти висело под ударом и как менялась
    # картина. Это и есть эффект рейпера в непрерывном измерении.
    sweeps = read_sweeps(cfg, since)
    under_threat = [int(x.get("would_free_mb", 0)) for x in sweeps]
    max_under_threat = max(under_threat) if under_threat else 0
    avg_under_threat = (sum(under_threat) / len(under_threat)) if under_threat else 0
    rule_hits: dict[str, int] = {}
    for x in sweeps:
        for name, count in (x.get("rules") or {}).items():
            rule_hits[name] = rule_hits.get(name, 0) + int(count)
    kinds_footprint = {"gradle": 0, "kotlin": 0}
    for x in sweeps[-1:]:
        for kind, data in (x.get("by_kind") or {}).items():
            kinds_footprint[kind] = int(data.get("footprint_mb", 0))

    metric = state.get("metric", {}) or {}
    events = int(metric.get("events", 0))
    reused = int(metric.get("reused", 0))
    rate = float(metric.get("rate", 0.0))
    pending = int(metric.get("pending", 0))

    return {
        "generated_at": now,
        "period_days": days,
        "since": since,
        "kills_total": len(kills),
        "kills_in_period": len(kills_in_period),
        "kills_by_action": by_action,
        "kills_by_reason": by_reason,
        "killed_mb": killed_mb,
        "freed_mb": int(state.get("freed_mb", 0)),
        "would_free_mb": int(state.get("would_free_mb", 0)),
        "metric": {"events": events, "reused": reused, "rate": rate,
                   "by_kind": metric.get("by_kind", {}),
                   "pending": pending},
        "false_positive_rate": rate,
        "min_events": int(cfg.get("PILOT_MIN_EVENTS", 8)),
        "max_reuse_rate": float(cfg.get("PILOT_MAX_REUSE_RATE", 0.6)),
        "sweeps": len(sweeps),
        "max_under_threat_mb": max_under_threat,
        "avg_under_threat_mb": int(avg_under_threat),
        "rule_hits": rule_hits,
        "current_footprint_mb": kinds_footprint,
        "kind_labels": (targets.labels() if targets is not None else {}),
        "stale_peer_events": int(state.get("stale_peer_events", 0)),
        "stale_peer_daemons": state.get("stale_peer_daemons", {}) or {},
        "pilot_started_at": float(state.get("pilot_started_at", 0.0)),
        "last_sweep_at": float(state.get("last_sweep_at", 0.0)),
    }


def format_report(data: dict) -> str:
    days = data["period_days"]
    lines = [
        f"Месячный отчёт java-reaper (за {days} дн.)",
        "=" * 62,
        "",
        "Эффект:",
        f"  освобождено памяти: {data['freed_mb']} МБ",
        f"  могло бы быть освобождено (dry-run): {data['would_free_mb']} МБ",
        "",
        "Убийства:",
        f"  всего за период: {data['kills_in_period']} "
        f"(всего в журнале: {data['kills_total']})",
        f"  освобождено убийствами: {data['killed_mb']} МБ",
    ]
    if data["kills_by_action"]:
        for action, count in sorted(data["kills_by_action"].items()):
            lines.append(f"    {action}: {count}")
    if data["kills_by_reason"]:
        lines.append("  по причинам:")
        for reason, count in sorted(data["kills_by_reason"].items(),
                                    key=lambda kv: -kv[1]):
            lines.append(f"    {reason}: {count}")
    if not data["kills_in_period"]:
        lines.append("    (за период убийств не было — рейпер в dry-run)")

    lines += [
        "",
        "Картина по журналу свипов:",
        f"  свипов за период: {data['sweeps']}",
        f"  под ударом висело: максимум {data['max_under_threat_mb']} МБ, "
        f"в среднем {data['avg_under_threat_mb']} МБ",
    ]
    if data["current_footprint_mb"]:
        # Виды не зашиты: сколько их — столько и строк. Подпись — из
        # targets.yaml («Gradle», а не «gradle»).
        labels = data.get("kind_labels") or {}
        parts = ", ".join(f"{labels.get(name, name)} {mb} МБ" for name, mb
                          in sorted(data["current_footprint_mb"].items()))
        lines.append(f"  сейчас держат демоны: {parts}")
    if data["rule_hits"]:
        lines.append("  правила за период:")
        for name, count in sorted(data["rule_hits"].items(), key=lambda kv: -kv[1]):
            lines.append(f"    {name}: {count}")
    lines.append("")

    metric = data["metric"]
    lines += [
        "",
        "Ложные срабатывания (демон понадобился снова):",
        f"  событий «would kill»: {metric['events']} (в ожидании {metric['pending']})",
        f"  понадобились снова: {metric['reused']} ({100 * data['false_positive_rate']:.0f} %)",
        f"  критерий перехода в бой: доля ≤ {100 * data['max_reuse_rate']:.0f} % "
        f"при ≥ {data['min_events']} наблюдённых событиях",
    ]
    if metric["events"] < data["min_events"]:
        lines.append("    данных пока мало — вердикт не выносится (см. --pilot)")
    by_kind = metric.get("by_kind") or {}
    if by_kind:
        lines.append("  по видам (у каждого своя цена ошибки):")
        for kind, data in sorted(by_kind.items()):
            lines.append(
                f"    {kind}: событий {data.get('counted', 0)}, ложных "
                f"{100 * data.get('rate', 0.0):.0f} % (взвешенно по цене "
                f"{100 * data.get('rate_weighted', 0.0):.0f} %), цена ложных "
                f"{data.get('cost_reused_seconds', 0.0):.0f} с")

    stale = data["stale_peer_events"]
    lines += [
        "",
        "Наблюдаемость правил:",
        f"  «зависший клиент» снял защиту: {stale} раз "
        f"(демонов: {len(data['stale_peer_daemons'])})",
    ]

    started = data["pilot_started_at"]
    if started:
        days_run = (data["generated_at"] - started) / 86400.0
        lines.append(f"  пилот идёт {days_run:.1f} сут")
    last = data["last_sweep_at"]
    if last:
        lines.append(f"  последний свип: {(data['generated_at'] - last) / 60:.1f} мин назад")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Месячный отчёт java-reaper")
    parser.add_argument("--days", type=int, default=30,
                        help="за сколько дней (по умолчанию 30)")
    parser.add_argument("--config", help="путь к конфигу рейпера")
    parser.add_argument("--json", action="store_true",
                        help="машиночитаемый вывод")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    targets = resolve_targets(load_targets(None, []), cfg, set(), [])
    data = collect(cfg, args.days, targets)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        print(format_report(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
