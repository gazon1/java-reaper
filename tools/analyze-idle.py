#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ретроспективный анализ простоев демонов по их логам.

Задача скрипта: ответить на вопрос «какой порог простоя (idle threshold)
оптимален, если убивать демонов, простаивающих дольше порога».

Анализ идёт ПО ЦЕЛЯМ (targets.yaml), а не по двум прописаным программам:

  * где лежат логи цели — `log_globs` в описании цели;
  * как понять, что демон работал, — `activity: markers` и сами маркеры
    `build_start` / `build_end` в описании цели;
  * как разбирать таймстемпы — `ts_format` в описании цели;
  * PID демона (нужен, чтобы отличить живой демон от умершего) —
    `log_pid_regex` в описании цели.

Цели, у которых активность измеряется по mtime лога (`activity: mtime`),
этим анализом НЕ охватываются: там нет маркеров начала и конца работы,
а значит нет «простоя между сборками». Такие цели пропускаются с явным
сообщением — молча пропустить было бы хуже, чем сказать.

Сам расчёт живёт в `reaper/idlestats.py` (модель простоя, агрегация,
подбор порога), здесь — только разбор аргументов и печать отчёта.

Скрипт только читает логи и печатает отчёт; ничего не изменяет на диске.
Только стандартная библиотека Python 3.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

# Пакет reaper лежит в родительском каталоге — нужен ради описаний целей.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reaper.config import load_config, resolve_targets  # noqa: E402
from reaper.idlestats import (  # noqa: E402
    analyze_target,
    analyzable,
    parse_thresholds,
)
from reaper.targets import load_targets  # noqa: E402


def fmt(value: float, digits: int = 1) -> str:
    """Форматирование числа с фиксированным числом знаков."""
    return f"{value:,.{digits}f}".replace(",", " ")


def print_report(label: str, report: Dict[str, Any],
                 scan_stats: Dict[str, Any]) -> None:
    """Печатает человекочитаемую таблицу по одной цели."""
    totals: Dict[str, Any] = report["totals"]
    per_threshold: List[Dict[str, Any]] = report["per_threshold"]

    print("=" * 78)
    print(f"Ретроспективный анализ простоев демонов: {label}")
    print("=" * 78)
    print(
        f"Логов прочитано:        {scan_stats['files_read']}"
        f"   (пропущено по размеру: {scan_stats['files_skipped']},"
        f" ошибок чтения: {scan_stats['files_failed']},"
        f" без маркеров: {scan_stats['files_without_markers']})"
    )
    print(
        f"Сборок найдено:         {scan_stats['builds_total']}"
        f"   (закрытых: {scan_stats['builds_closed']},"
        f" незакрытых: {scan_stats['builds_unclosed']})"
    )
    print(f"Простоев учтено:        {totals['idle_gaps']}")
    print(f"Всего простоев:         {fmt(totals['total_idle_hours'], 1)} демо-часов")
    print(
        f"Хвост наблюдения (от последнего события в логе до mtime файла):"
        f" {fmt(scan_stats['tail_observation_hours'], 1)} демо-часов"
        f" в {scan_stats['tail_observation_files']} логах (в события простоя не входит)"
    )
    print(
        f"Чистый хвостовой простой (логи с закрытой последней сборкой):"
        f" {fmt(scan_stats['tail_idle_hours'], 1)} демо-часов"
        f" в {scan_stats['tail_idle_files']} логах"
    )
    unknown_alive = int(scan_stats.get("alive_unknown_files", 0) or 0)
    if unknown_alive:
        print(
            f"Живость не определялась:  {unknown_alive} логов без PID в имени "
            f"(у цели нет log_pid_regex) — хвостовые простои посчитаны по mtime"
        )
    print()

    print("Распределение длительности простоев:")
    print(
        f"  p50 = {fmt(totals['p50_min'], 1)} мин    "
        f"p90 = {fmt(totals['p90_min'], 1)} мин    "
        f"p99 = {fmt(totals['p99_min'], 1)} мин"
    )
    print(
        f"  min = {fmt(totals['min_min'], 1)} мин    "
        f"max = {fmt(totals['max_min'], 1)} мин    "
        f"среднее = {fmt(totals['mean_min'], 1)} мин"
    )
    print(
        f"  доля простоев < 30 мин: {fmt(totals['share_under_30min_pct'], 1)}%"
        f"    доля < 2 ч: {fmt(totals['share_under_2h_pct'], 1)}%"
    )
    print()

    header = (
        f"{'Порог':>7} | {'kill':>5} | {'%прост':>7} | {'демо-ч':>8} | {'%час':>6} |"
        f" {'reuse2ч':>8} | {'%2ч':>6} | {'reuse24ч':>9} | {'%24ч':>6} | {'демонов':>7}"
    )
    print(header)
    print("-" * len(header))
    for row in per_threshold:
        print(
            f"{row['threshold_min']:>6}м | {row['kill_events']:>5} |"
            f" {fmt(row['kill_events_pct'], 1):>7} | {fmt(row['demo_hours'], 1):>8} |"
            f" {fmt(row['demo_hours_pct'], 1):>6} | {row['reused_2h']:>8} |"
            f" {fmt(row['reused_2h_pct'], 1):>6} | {row['reused_24h']:>9} |"
            f" {fmt(row['reused_24h_pct'], 1):>6} | {row['daemons_affected']:>7}"
        )
    print()
    print(
        "Пояснение: kill — число простоев длиннее порога; демо-ч — сумма их длины"
        " в часах; %прост/%час — доля от всех простоев (по числу и по часам);"
    )
    print(
        "reuse2ч/reuse24ч — сколько kill-событий оказались ложными"
        " (демон понадобился снова в течение 2 ч / 24 ч после возобновления работы);"
    )
    print(
        f" %2ч/%24ч — доля от kill-событий с известным reuse-статусом"
        f" (неизвестных: {per_threshold[0]['reuse_unknown'] if per_threshold else 0})."
    )

    tail_stats = report.get("tail") or {}
    per_all = report.get("per_threshold_all") or []
    if tail_stats and per_all:
        print()
        print("С учётом хвостовых простоев (демон перестал работать и висел дальше):")
        header2 = (
            f"{'Порог':>7} | {'kill':>5} | {'из хвоста':>9} | {'живых сейчас':>12} |"
            f" {'демо-ч':>8} | {'демонов':>7}"
        )
        print(header2)
        print("-" * len(header2))
        for row in per_all:
            print(
                f"{row['threshold_min']:>6}м | {row['kill_events']:>5} |"
                f" {row['from_tail']:>9} | {row['still_alive']:>12} |"
                f" {fmt(row['demo_hours'], 1):>8} | {row['daemons_affected']:>7}"
            )
        print(
            f"  хвостов всего: {tail_stats['tails']} "
            f"({fmt(tail_stats['tail_demo_hours'], 1)} демо-часов), "
            f"из них демонов живы сейчас: {tail_stats['tail_alive']}"
        )

    recommendation = report.get("recommendation")
    if recommendation:
        print()
        print(f"Рекомендация (эвристика): {recommendation}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ретроспективный анализ простоев демонов по логам (по целям "
            "из targets.yaml): подбор оптимального порога простоя для kill "
            "по таймауту."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--thresholds",
        default="30,60,120,180,360,720",
        help="пороги простоя в минутах через запятую (по умолчанию: %(default)s)",
    )
    parser.add_argument(
        "--reuse-window",
        type=int,
        default=7200,
        help=(
            "окно «понадобился снова» в секундах (по умолчанию: %(default)s = 2 ч). "
            "Длинная метрика всегда использует 24 ч."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="машинно-читаемый вывод в stdout (JSON), предупреждения — в stderr",
    )
    parser.add_argument(
        "--targets",
        default=None,
        help="путь к файлу целей (YAML); по умолчанию — ~/.config/java-reaper/targets.yaml",
    )
    parser.add_argument(
        "--kind",
        action="append",
        dest="kinds",
        help=(
            "разбирать только указанный вид демонов (можно указать несколько). "
            "По умолчанию — все, которые этим анализом охватываются."
        ),
    )
    parser.add_argument(
        "--glob",
        action="append",
        dest="globs",
        help=(
            "шаблон путей к логам (можно указать несколько) — перекрывает "
            "log_globs цели. Полезно, чтобы разобрать логи одной машины, "
            "не правя описание цели."
        ),
    )
    parser.add_argument(
        "--max-log-mb",
        type=float,
        default=512.0,
        help="пропускать логи больше указанного размера (МБ), по умолчанию: %(default)s",
    )

    args = parser.parse_args(argv)

    try:
        thresholds = parse_thresholds(args.thresholds)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    if args.reuse_window <= 0:
        print("Ошибка: --reuse-window должен быть положительным", file=sys.stderr)
        return 2

    warnings: List[str] = []
    cfg = load_config(None, warnings)
    targets = resolve_targets(load_targets(args.targets, warnings),
                              cfg, set(), warnings)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    if args.kinds:
        wanted = set(args.kinds)
        unknown = wanted - set(targets.names())
        if unknown:
            print(f"Ошибка: неизвестные виды демонов: {', '.join(sorted(unknown))}",
                  file=sys.stderr)
            return 2
        specs = [spec for spec in targets.specs() if spec.name in wanted]
    else:
        specs = list(targets.specs())

    max_bytes = int(args.max_log_mb * 1024 * 1024)
    now = time.time()

    reports: Dict[str, Any] = {}
    skipped: List[Dict[str, str]] = []
    for spec in specs:
        reason = analyzable(spec)
        if reason is not None:
            skipped.append({"kind": spec.name, "reason": reason})
            continue
        report, scan_stats = analyze_target(spec, thresholds, args.reuse_window,
                                            now, max_bytes, args.globs)
        reports[spec.name] = {"report": report, "scan": scan_stats}

    if not reports:
        message = (
            "Ни одна цель не охватывается этим анализом. "
            + "; ".join(f"{s['kind']}: {s['reason']}" for s in skipped)
        )
        if args.json:
            print(json.dumps({"thresholds": thresholds, "targets": {},
                              "skipped": skipped, "message": message},
                             ensure_ascii=False, indent=2))
        else:
            print(message)
        return 0

    if args.json:
        payload = {
            "thresholds": thresholds,
            "targets": {
                kind: {"report": data["report"], "scan": data["scan"]}
                for kind, data in reports.items()
            },
            "skipped": skipped,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    labels = targets.labels()
    for kind, data in reports.items():
        print_report(labels.get(kind, kind), data["report"], data["scan"])
        print()

    if skipped:
        print("Пропущены цели (этим анализом не охватываются):")
        for item in skipped:
            print(f"  {item['kind']}: {item['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
