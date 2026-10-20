#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Прибор измерения порогов по накопленным журналам.

Зачем он нужен: пороги рейпера (простоя, «зависшего», давления памяти)
должны быть измерены, а не выбраны. Но «измерить» для рейпера — значит
накопить данные: пока демонов мало и памяти хватает, ни одно правило
не срабатывает, и порог остаётся непроверенным. Этот инструмент честно
показывает и то, что измерено, и то, что НЕ измерено.

Два источника данных:

  1. Журнал свипов (`sweeps.jsonl`) — что рейпер видел в каждом свипе:
     сколько памяти висело под ударом, сколько было доступно системе,
     какое распределение footprint'ов демонов. Отсюда — вывод про порог
     давления по памяти: при каком уровне свободной памяти уборка что-то
     даёт.

  2. История сводок демонов (`logs-history.jsonl`) — жизнь демонов:
     когда жили, когда шли сборки, когда умерли. Отсюда — распределение
     простоя по видам: сколько демонов простаивало дольше текущего
     порога цели (то есть сколько уборка задела бы) и какая доля
     ложных срабатываний.

Честность важнее удобства: если данных мало, инструмент так и говорит
«данных недостаточно» и печатает размер выборки, а не делает вид, что
вывод есть.

Только стандартная библиотека Python 3. Ничего не изменяет на диске.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reaper.config import DEFAULTS, load_config, resolve_targets  # noqa: E402
from reaper.corpus import (  # noqa: E402
    gaps_between_builds,
    human,
    percentile,
    read_jsonl,
    rule_coverage,
    stuck_memory_report,
    stuck_summaries,
    tail_idle,
    tail_idle_bounds,
)
from reaper.policy import default_policy, kill_rule_names  # noqa: E402
from reaper.targets import (  # noqa: E402
    ACTIVITY_MARKERS,
    TargetSet,
    load_targets,
)

# Минимальный размер выборки, при котором вывод можно принимать к сведению.
# Ниже — вывод не делается, но показывается, чего не хватает.
MIN_SAMPLE = 30


def measure_sweeps(rows: List[dict[str, Any]], cfg: dict[str, Any],
                   targets: TargetSet) -> Dict[str, Any]:
    """Что показывает журнал свипов: сколько памяти висело под ударом."""
    if not rows:
        return {"sweeps": 0, "note": "журнал свипов пуст"}

    pressure_mb = int(cfg.get("MEMORY_PRESSURE_MB", 4096))
    large_mb = int(cfg.get("KILL_LARGE_DAEMON_MB", 4096))
    under_impact = [float(r.get("would_free_mb", 0) or 0) for r in rows]
    free = [float(r["mem_available_mb"]) for r in rows
            if r.get("mem_available_mb") is not None]
    footprint_by_kind: Dict[str, List[float]] = {}
    for row in rows:
        for kind, data in (row.get("by_kind") or {}).items():
            value = float(data.get("footprint_mb", 0) or 0)
            footprint_by_kind.setdefault(kind, []).append(value)

    sweeps_with_impact = sum(1 for v in under_impact if v > 0)
    # Полный список правил — из политики: журнал хранит только сработавшие
    # правила, и без этого списка «не сработало ни разу» было бы пустым.
    policy_cfg = {**DEFAULTS, **cfg}
    rule_names = [rule.name for rule in default_policy(policy_cfg)]
    coverage = rule_coverage(rows, rule_names)
    result: Dict[str, Any] = {
        "sweeps": len(rows),
        "period_days": (max(r["at"] for r in rows)
                        - min(r["at"] for r in rows)) / 86400.0
        if rows else 0.0,
        "would_free_mb_max": max(under_impact) if under_impact else 0.0,
        "would_free_mb_mean": (statistics.fmean(under_impact)
                               if under_impact else 0.0),
        "sweeps_with_impact": sweeps_with_impact,
        "pressure_mb": pressure_mb,
        "large_daemon_mb": large_mb,
        "footprint_by_kind": {},
        "free_memory": {},
        "rule_coverage": coverage,
        "kill_rules": kill_rule_names(policy_cfg),
    }

    for kind, values in sorted(footprint_by_kind.items()):
        result["footprint_by_kind"][kind] = {
            "sweeps": len(values),
            "max_mb": max(values),
            "p95_mb": percentile(values, 95),
            "mean_mb": statistics.fmean(values),
        }

    if free:
        result["free_memory"] = {
            "measured_sweeps": len(free),
            "min_mb": min(free),
            "p50_mb": percentile(free, 50),
            "max_mb": max(free),
            "sweeps_below_pressure": sum(1 for v in free if v < pressure_mb),
        }
    else:
        result["free_memory"] = {
            "measured_sweeps": 0,
            "note": "в журнале нет поля mem_available_mb — оно появилось только "
                    "в свежих свипах",
        }

    # Вывод про порог давления: честный — по факту, а не по умолчанию.
    if sweeps_with_impact == 0:
        result["verdict"] = (
            f"порог давления {pressure_mb} МБ НЕ ПРОВЕРЕН: ни в одном из "
            f"{len(rows)} свипов под ударом не висело ни байта, то есть "
            f"убирать было нечего. Либо свободной памяти всегда хватало, "
            f"либо демоны не простаивали дольше порога простоя")
    else:
        result["verdict"] = (
            f"под ударом висело в {sweeps_with_impact} свипах из {len(rows)}, "
            f"максимум {human(result['would_free_mb_max'])} МБ — порог "
            f"давления {pressure_mb} МБ оказывается рабочим")
    return result


# --------------------------------------------------------------------------- #
# Часть 2: история сводок — простой по видам
# --------------------------------------------------------------------------- #


def measure_history(rows: List[dict[str, Any]],
                    targets: TargetSet) -> Dict[str, Any]:
    """Что показывает история сводок: простой по видам против порогов целей."""
    if not rows:
        return {"records": 0, "note": "история сводок пуста"}

    gaps: Dict[str, List[float]] = {}
    tails: Dict[str, List[float]] = {}
    tail_bounds: Dict[str, List[tuple[float, float]]] = {}
    unknown: Dict[str, int] = {}
    deaths: Dict[str, int] = {}
    totals_by_kind: Dict[str, int] = {}
    for record in rows:
        kind = str(record.get("kind") or "unknown")
        totals_by_kind[kind] = totals_by_kind.get(kind, 0) + 1
        if record.get("death_observed_at") is not None:
            deaths[kind] = deaths.get(kind, 0) + 1
        gap = gaps_between_builds(record)
        if gap is not None:
            gaps.setdefault(kind, []).append(gap)
        tail = tail_idle(record)
        if tail is not None:
            tails.setdefault(kind, []).append(tail)
        bounds = tail_idle_bounds(record)
        if bounds is not None:
            tail_bounds.setdefault(kind, []).append(bounds)
        if gap is None and tail is None:
            unknown[kind] = unknown.get(kind, 0) + 1

    result: Dict[str, Any] = {
        "records": len(rows),
        "kinds": {},
        "unmeasurable_by_kind": unknown,
        "deaths_observed_by_kind": deaths,
        "stuck": stuck_summaries(rows),
        "totals_by_kind": totals_by_kind,
    }
    kinds = sorted(set(gaps) | set(tails))
    for kind in kinds:
        spec = targets.get(kind)
        limit = (float(spec.idle_seconds)
                 if spec is not None and spec.idle_seconds else None)
        bucket: Dict[str, Any] = {
            "measured": len(gaps.get(kind, [])) + len(tails.get(kind, [])),
            "idle_limit_seconds": limit,
        }

        gap_values = gaps.get(kind, [])
        if gap_values:
            bucket["between_builds"] = {
                "measured": len(gap_values),
                "p50_min": percentile(gap_values, 50) / 60.0,
                "p90_min": percentile(gap_values, 90) / 60.0,
                "max_min": max(gap_values) / 60.0,
            }
            if limit is not None:
                over = sum(1 for v in gap_values if v > limit)
                bucket["between_builds"]["over_limit"] = over

        tail_values = tails.get(kind, [])
        if tail_values:
            bucket["tail"] = {
                "measured": len(tail_values),
                "p50_min": percentile(tail_values, 50) / 60.0,
                "p90_min": percentile(tail_values, 90) / 60.0,
                "max_min": max(tail_values) / 60.0,
            }
            if limit is not None:
                over = sum(1 for v in tail_values if v > limit)
                bucket["tail"]["over_limit"] = over
                bucket["tail"]["over_limit_pct"] = (
                    round(100.0 * over / len(tail_values), 1) if tail_values else 0.0)

        # Границы хвостового простоя: момент смерти известен с точностью до
        # интервала свипа. Нижняя граница (демон точно жил) не завышает
        # простой — значит, не создаёт мнимых «долгих простоев».
        bounds_list = tail_bounds.get(kind, [])
        if bounds_list:
            lowers = [b[0] for b in bounds_list]
            uppers = [b[1] for b in bounds_list]
            bucket["tail_bounds"] = {
                "measured": len(bounds_list),
                "lower_max_min": max(lowers) / 60.0,
                "upper_max_min": max(uppers) / 60.0,
                "lower_p90_min": percentile(lowers, 90) / 60.0,
                "upper_p90_min": percentile(uppers, 90) / 60.0,
            }
            if limit is not None:
                bucket["tail_bounds"]["over_limit_lower"] = sum(
                    1 for b in bounds_list if b[0] > limit)
                bucket["tail_bounds"]["over_limit_upper"] = sum(
                    1 for b in bounds_list if b[1] > limit)

        # Вердикт: данных мало — вывода нет; данных хватает — говорим прямо.
        if bucket["measured"] < MIN_SAMPLE:
            bucket["verdict"] = (
                f"данных недостаточно: {bucket['measured']} замеров "
                f"(нужно ≥ {MIN_SAMPLE}) — вывод по простоям этого вида "
                f"делать нельзя")
        else:
            parts = []
            if "between_builds" in bucket:
                parts.append(
                    f"простой между сборками p90 = "
                    f"{human(bucket['between_builds']['p90_min'])} мин, "
                    f"максимум {human(bucket['between_builds']['max_min'])} мин")
            if "tail" in bucket:
                parts.append(
                    f"хвостовой простой p90 = "
                    f"{human(bucket['tail']['p90_min'])} мин, "
                    f"максимум {human(bucket['tail']['max_min'])} мин")
            bucket["verdict"] = "; ".join(parts)
        result["kinds"][kind] = bucket
    return result


def measure_stuck_memory(sweep_rows: List[dict[str, Any]],
                         history_rows: List[dict[str, Any]],
                         cfg: dict[str, Any]) -> Dict[str, Any]:
    """Сколько памяти висело на зависших демонах — и стоит ли этот класс."""
    threshold = float(cfg.get("STUCK_AFTER_SECONDS", 3600))
    report = stuck_memory_report(history_rows, sweep_rows, threshold)
    if not report.get("measured"):
        report["note"] = ("зависших демонов в корпусе нет — измерять нечего")
        return report
    # Честный вывод: класс нужен, если удерживаемая память заметна. Сравниваем
    # со свободной памятью из журнала свипов.
    free = [float(r["mem_available_mb"]) for r in sweep_rows
            if r.get("mem_available_mb") is not None]
    if free:
        report["free_memory_p50_mb"] = percentile(free, 50)
        report["share_of_free_memory_pct"] = (
            round(100.0 * report["held_mb_average"] / percentile(free, 50), 2)
            if percentile(free, 50) else 0.0)
    report["verdict"] = (
        f"зависших {report['count']}, из них пережили порог "
        f"{threshold / 60:.0f} мин — {report['over_threshold']} "
        f"({report['over_threshold_pct']} %); в среднем висело "
        f"{human(report['held_mb_average'])} МБ, из них сверх порога — "
        f"{human(report['held_mb_average_over_threshold'])} МБ")
    return report


# --------------------------------------------------------------------------- #
# Вывод
# --------------------------------------------------------------------------- #


def print_report(sweeps: Dict[str, Any], history: Dict[str, Any],
                 stuck_memory: Optional[Dict[str, Any]] = None) -> None:
    print("=" * 78)
    print("Измерение порогов по накопленным журналам")
    print("=" * 78)

    print()
    print("1. Давление по памяти (по журналу свипов)")
    print("-" * 78)
    if not sweeps.get("sweeps"):
        print(f"  {sweeps.get('note', 'журнал пуст')}")
    else:
        print(f"  свипов: {sweeps['sweeps']}"
              f"   период: {sweeps['period_days']:.1f} сут")
        print(f"  под ударом висело: максимум {human(sweeps['would_free_mb_max'])} МБ,"
              f" в среднем {human(sweeps['would_free_mb_mean'])} МБ,"
              f" свипов с уборкой: {sweeps['sweeps_with_impact']}")
        free = sweeps.get("free_memory") or {}
        if free.get("measured_sweeps"):
            print(f"  свободной памяти: минимум {human(free['min_mb'], 0)} МБ,"
                  f" медиана {human(free['p50_mb'], 0)} МБ,"
                  f" максимум {human(free['max_mb'], 0)} МБ")
            print(f"  свипов ниже порога давления ({sweeps['pressure_mb']} МБ):"
                  f" {free['sweeps_below_pressure']}")
        else:
            print(f"  свободная память: {free.get('note', 'не измерялась')}")
        for kind, data in (sweeps.get("footprint_by_kind") or {}).items():
            print(f"  footprint {kind:>8}: максимум {human(data['max_mb'], 0)} МБ,"
                  f" p95 {human(data['p95_mb'], 0)} МБ,"
                  f" среднее {human(data['mean_mb'], 0)} МБ")
        print()
        print(f"  ВЫВОД: {sweeps['verdict']}")

        coverage = sweeps.get("rule_coverage") or {}
        kill_rules = sweeps.get("kill_rules") or []
        if coverage.get("sweeps"):
            print()
            print("  Покрытие правил (какие пороги вообще проверены):")
            for rule, count in (coverage.get("fired") or {}).items():
                print(f"    сработало {count:>6} раз  {rule}")
            never = coverage.get("never_fired") or []
            kill_never = [rule for rule in never if rule in kill_rules]
            protective = [rule for rule in never if rule not in kill_rules]
            if kill_never:
                print(f"    НИ РАЗУ не сработало (kill-правила): "
                      f"{', '.join(kill_never)}")
                print("      → пороги за этими правилами на живых демонах не "
                      "проверены, и пилоту не из чего набирать события")
            if protective:
                print(f"    НИ РАЗУ не сработало (защитные): "
                      f"{', '.join(protective)}")
                print("      → защищать было некого, а не «порог не проверен»")

    print()
    print("2. Простой по видам (по истории сводок демонов)")
    print("-" * 78)
    if not history.get("records"):
        print(f"  {history.get('note', 'история пуста')}")
    else:
        print(f"  сводок: {history['records']}")
        for kind, data in (history.get("kinds") or {}).items():
            print()
            print(f"  {kind}: замеров {data['measured']}, "
                  f"без замера {history['unmeasurable_by_kind'].get(kind, 0)}")
            if data.get("idle_limit_seconds"):
                print(f"    порог цели: {data['idle_limit_seconds'] / 60:.0f} мин")
            if "between_builds" in data:
                gap = data["between_builds"]
                print(f"    простой МЕЖДУ сборками: замеров {gap['measured']},"
                      f" p50 = {human(gap['p50_min'])} мин,"
                      f" p90 = {human(gap['p90_min'])} мин,"
                      f" максимум = {human(gap['max_min'])} мин")
                if "over_limit" in gap:
                    print(f"      дольше порога цели: {gap['over_limit']}")
            if "tail" in data:
                tail = data["tail"]
                print(f"    хвостовой простой (последняя работа → смерть):"
                      f" замеров {tail['measured']},"
                      f" p50 = {human(tail['p50_min'])} мин,"
                      f" p90 = {human(tail['p90_min'])} мин,"
                      f" максимум = {human(tail['max_min'])} мин")
                if "over_limit" in tail:
                    print(f"      дольше порога цели: {tail['over_limit']}"
                          f" ({tail['over_limit_pct']} %)")
            if "tail_bounds" in data:
                bounds = data["tail_bounds"]
                print(f"    границы хвостового простоя (смерть известна с "
                      f"точностью до свипа): замеров {bounds['measured']},"
                      f" максимум от {human(bounds['lower_max_min'])}"
                      f" до {human(bounds['upper_max_min'])} мин")
                if "over_limit_lower" in bounds:
                    print(f"      дольше порога: по нижней границе "
                          f"{bounds['over_limit_lower']}, по верхней "
                          f"{bounds['over_limit_upper']}")
            print(f"    {data['verdict']}")
        deaths = history.get("deaths_observed_by_kind") or {}
        if deaths:
            print()
            print("  момент смерти замечен у:", ", ".join(
                f"{kind} — {count}" for kind, count in sorted(deaths.items())))

        stuck = history.get("stuck") or {}
        counts = stuck.get("count_by_kind") or {}
        if counts:
            print()
            print("  ЗАВИСШИЕ (клиент отвалился во время сборки) — единственный "
                  "класс, который программа сама не убирает:")
            for kind, count in sorted(counts.items()):
                lifetimes = (stuck.get("lifetimes_by_kind") or {}).get(kind) or []
                share = ""
                total = (history.get("totals_by_kind") or {}).get(kind, 0)
                if total:
                    share = f" ({100.0 * count / total:.1f} % всех демонов вида)"
                print(f"    {kind}: {count}{share}")
                if lifetimes:
                    print(f"      прожили после отмены: замеров {len(lifetimes)},"
                          f" p50 = {human(percentile(lifetimes, 50) / 60.0)} мин,"
                          f" p90 = {human(percentile(lifetimes, 90) / 60.0)} мин,"
                          f" максимум = {human(max(lifetimes) / 60.0)} мин")
                    sources = stuck.get("sources") or {}
                    if sources:
                        print(f"      откуда оценка: " + ", ".join(
                            f"{key} — {count}" for key, count in
                            sorted(sources.items()))
                            + " (mtime — «умер сразу после последней записи»,"
                              " оценка снизу; death_mark — рейпер видел пропажу; "
                              "last_seen — рейпер видел демона живым)")
                else:
                    print("      сколько прожили после отмены — измерить нечем: "
                          "момент смерти не замечен")

    if stuck_memory is not None:
        print()
        print("3. Память на зависших демонах (стоит ли этот класс)")
        print("-" * 78)
        if not stuck_memory.get("measured"):
            print(f"  {stuck_memory.get('note', 'измерять нечего')}")
        else:
            print(f"  зависших: {stuck_memory['count']}"
                  f"   из них пережили порог "
                  f"{stuck_memory['threshold_seconds'] / 60:.0f} мин: "
                  f"{stuck_memory['over_threshold']} "
                  f"({stuck_memory['over_threshold_pct']} %)")
            print(f"  удержали память: {human(stuck_memory['daemon_hours_total'])}"
                  f" демон-часов за {human(stuck_memory['period_hours'])} ч")
            for kind, value in sorted((stuck_memory.get('footprint_median_mb') or {}).items()):
                print(f"    footprint одного {kind} (медиана по свипам): "
                      f"{human(value, 0)} МБ")
            print(f"  в среднем висело на зависших: "
                  f"{human(stuck_memory['held_mb_average'])} МБ; "
                  f"сверх порога — "
                  f"{human(stuck_memory['held_mb_average_over_threshold'])} МБ")
            if "free_memory_p50_mb" in stuck_memory:
                print(f"  это {stuck_memory['share_of_free_memory_pct']} % от "
                      f"свободной памяти (медиана "
                      f"{human(stuck_memory['free_memory_p50_mb'], 0)} МБ)")
            print()
            print(f"  ВЫВОД: {stuck_memory['verdict']}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Измерение порогов рейпера по накопленным журналам.")
    parser.add_argument("--sweeps", default=None,
                        help="путь к sweeps.jsonl (по умолчанию — из конфига)")
    parser.add_argument("--history", default=None,
                        help="путь к logs-history.jsonl "
                             "(по умолчанию — из конфига)")
    parser.add_argument("--targets", default=None,
                        help="путь к файлу целей (YAML)")
    parser.add_argument("--json", action="store_true",
                        help="машинно-читаемый вывод")
    args = parser.parse_args(argv)

    warnings: List[str] = []
    cfg = load_config(None, warnings)
    targets: TargetSet = resolve_targets(load_targets(args.targets, warnings),
                                          cfg, set(), warnings)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    state_dir = os.path.expanduser(str(cfg["STATE_DIR"]))
    sweeps_path = args.sweeps or os.path.join(state_dir, "sweeps.jsonl")
    history_path = args.history or os.path.join(state_dir, "logs-history.jsonl")

    sweep_rows = read_jsonl(sweeps_path)
    history_rows = read_jsonl(history_path)
    sweeps = measure_sweeps(sweep_rows, cfg, targets)
    history = measure_history(history_rows, targets)
    stuck_memory = measure_stuck_memory(sweep_rows, history_rows, cfg)

    if args.json:
        print(json.dumps({"sweeps": sweeps, "history": history,
                          "stuck_memory": stuck_memory},
                         ensure_ascii=False, indent=2))
        return 0
    print_report(sweeps, history, stuck_memory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
