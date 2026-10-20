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

Модель данных:
  Сборка i одного демона = интервал [start_i, end_i].
  Простой (idle gap) между сборками = end_i -> start_{i+1}.
  Простой после последней сборки (до mtime файла) НЕ считается событием простоя,
  но показывается отдельно как «хвостовой» простой.

Reuse-критерий (для kill-события, то есть простоя длиннее порога):
  «демон понадобился снова» — это значит, что после сборки i+1 (то есть после
  момента end_{i+1}, когда демон окончательно вернулся в работу) следующая
  сборка того же демона началась менее чем через reuse_window секунд
  (по умолчанию 7200 с = 2 ч). Длинное окно — 24 ч (константа).

  Такой kill-событие — ложное срабатывание: демон всё равно понадобился бы
  в ближайшие часы, и его убийство привело бы лишь к холодному старту.

Скрипт только читает логи и печатает отчёт; ничего не изменяет на диске.
Только стандартная библиотека Python 3.
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Пакет reaper лежит в родительском каталоге — нужен ради описаний целей.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reaper.config import load_config, resolve_targets  # noqa: E402
from reaper.logparse import FileScan, Markers, scan_file  # noqa: E402
from reaper.targets import (  # noqa: E402
    ACTIVITY_MARKERS,
    TargetSet,
    TargetSpec,
    load_targets,
)

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

# Окно «понадобился снова» для длинной метрики (24 ч).
LONG_REUSE_WINDOW_S = 86400

# Порог, при котором reuse-событие считается «ложным срабатыванием»
# при выборе рекомендуемого порога (эвристика, не жёсткое правило).
REUSE_TOLERANCE_PCT = 5.0


# ---------------------------------------------------------------------------
# Модель данных
# ---------------------------------------------------------------------------


class Gap:
    """Простой между двумя соседними сборками одного демона."""

    __slots__ = ("daemon", "start", "end", "duration", "reuse_delta",
                 "is_tail", "alive")

    def __init__(
        self,
        daemon: str,
        start: float,
        end: float,
        duration: float,
        reuse_delta: Optional[float],
        is_tail: bool = False,
        alive: bool = False,
    ) -> None:
        self.daemon = daemon
        self.start = start
        self.end = end
        self.duration = duration
        # Через сколько секунд после конца сборки i+1 началась сборка i+2.
        # None — определить нельзя (сборка i+1 не закрыта).
        self.reuse_delta = reuse_delta
        # Хвост — простой от последней закрытой сборки до конца наблюдения
        # (смерти демона или текущего момента, если демон жив).
        self.is_tail = is_tail
        self.alive = alive


# ---------------------------------------------------------------------------
# Разбор логов
# ---------------------------------------------------------------------------
#
# Сам разбор — в reaper/logparse.py (общий с рейпером): scan_file, маркеры
# и парсер таймстемпа. Здесь только модель простоя.


def build_gaps(scan: FileScan) -> List[Gap]:
    """
    Собирает простои между соседними сборками одного демона.

    Простой между сборками i и i+1 существует, только если у сборки i есть
    реальный маркер «стал idle». Если сборка i незакрыта, конец простоя
    неизвестен и такой простой не учитывается.
    """
    gaps: List[Gap] = []
    builds = scan.builds

    for i in range(len(builds) - 1):
        end_i = builds[i].end
        if end_i is None:
            # Незакрытая сборка: простой после неё определить нельзя.
            continue
        start_next = builds[i + 1].start
        duration = start_next - end_i
        if duration < 0:
            # Аномалия: часы идут назад (переход через смену часового пояса
            # или повреждённый лог). Такой простой исключаем.
            continue

        # Reuse-дельта: интервал от конца сборки i+1 до начала сборки i+2.
        end_next = builds[i + 1].end
        if i + 2 < len(builds):
            if end_next is None:
                reuse_delta: Optional[float] = None
            else:
                reuse_delta = builds[i + 2].start - end_next
        else:
            # Следующей сборки не было вовсе — демон больше не понадобился.
            reuse_delta = None if end_next is None else float("inf")

        gaps.append(Gap(scan.path, end_i, start_next, duration, reuse_delta))

    return gaps


def pid_from_name(spec: TargetSpec, path: str) -> Optional[int]:
    """PID демона из имени файла — по описанию цели (log_pid_regex).

    У цели, у которой PID в имени настоящий (Gradle), вернётся число; у
    цели без такой регулярки — None, и живость демона определить нельзя.
    """
    if not spec.log_pid_regex:
        return None
    try:
        pattern = re.compile(spec.log_pid_regex)
    except re.error:
        return None
    match = pattern.match(os.path.basename(path))
    if not match:
        return None
    try:
        return int(match.groupdict().get("pid"))
    except (TypeError, ValueError):
        return None


def daemon_alive(spec: TargetSpec, path: str) -> Optional[bool]:
    """Жив ли демон прямо сейчас (None — определить нельзя).

    ВНИМАНИЕ: PID мог быть переиспользован другим процессом — тогда alive
    покажет «да» ошибочно. Для оценки объёма это приемлемо.

    None возвращается, когда у цели в имени файла нет PID: тогда угадывать
    нечего, и молча считать демона мёртвым нельзя.
    """
    pid = pid_from_name(spec, path)
    if pid is None:
        return None
    return os.path.isdir(f"/proc/{pid}")


def build_tail_gap(scan: FileScan, now: float,
                   alive: Optional[bool]) -> Optional[Gap]:
    """Хвостовой простой: от конца последней закрытой сборки до конца жизни демона.

    Это и есть главный класс «вечных» демонов: они перестали работать и
    просто висели дальше. Для живого демона конец наблюдения — текущий
    момент, для мёртвого — время изменения лога.
    """
    if not scan.builds or scan.builds[-1].end is None:
        return None  # последняя сборка не закрыта — хвост включал бы её время
    end_of_idle = float(scan.builds[-1].end)
    is_alive = bool(alive)
    observation_end = now if is_alive else scan.mtime
    duration = observation_end - end_of_idle
    if duration <= 0:
        return None
    return Gap(
        daemon=scan.path,
        start=end_of_idle,
        end=observation_end,
        duration=duration,
        reuse_delta=None,   # после последней сборки «повторной нужности» не бывает
        is_tail=True,
        alive=is_alive,
    )


# ---------------------------------------------------------------------------
# Агрегация
# ---------------------------------------------------------------------------


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Перцентиль (линейная интерполяция, как в numpy)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * (q / 100.0)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return float(sorted_values[low] * (1.0 - frac) + sorted_values[high] * frac)


def aggregate(
    gaps: List[Gap],
    thresholds: List[int],
    reuse_window: int,
    tails: Optional[List[Gap]] = None,
) -> Dict[str, object]:
    """Считает сводные цифры и per-threshold статистику.

    tails — хвостовые простои (демон перестал работать и висел дальше).
    Они попадают в объединённую таблицу, но не в reuse-статистику:
    после последней сборки «повторной нужности» не бывает.
    """
    durations = sorted(g.duration for g in gaps)
    total_idle_hours = sum(durations) / 3600.0

    totals: Dict[str, object] = {
        "idle_gaps": len(gaps),
        "total_idle_hours": round(total_idle_hours, 3),
        "p50_min": round(percentile(durations, 50) / 60.0, 3),
        "p90_min": round(percentile(durations, 90) / 60.0, 3),
        "p99_min": round(percentile(durations, 99) / 60.0, 3),
        "min_min": round(durations[0] / 60.0, 3) if durations else 0.0,
        "max_min": round(durations[-1] / 60.0, 3) if durations else 0.0,
        "mean_min": round((sum(durations) / len(durations)) / 60.0, 3) if durations else 0.0,
        "share_under_30min_pct": round(
            100.0 * sum(1 for d in durations if d < 30 * 60) / len(durations), 2
        )
        if durations
        else 0.0,
        "share_under_2h_pct": round(
            100.0 * sum(1 for d in durations if d < 7200) / len(durations), 2
        )
        if durations
        else 0.0,
    }

    per_threshold: List[Dict[str, object]] = []
    for threshold_min in thresholds:
        threshold_s = threshold_min * 60
        kills = [g for g in gaps if g.duration > threshold_s]
        kill_count = len(kills)
        demo_hours = sum(g.duration for g in kills) / 3600.0

        # Для reuse считаем только те kill-события, для которых интервал
        # до следующей сборки вообще известен.
        known = [g for g in kills if g.reuse_delta is not None]
        unknown = kill_count - len(known)
        reused_2h = sum(1 for g in known if g.reuse_delta < reuse_window)
        reused_24h = sum(1 for g in known if g.reuse_delta < LONG_REUSE_WINDOW_S)

        per_threshold.append(
            {
                "threshold_min": threshold_min,
                "kill_events": kill_count,
                "kill_events_pct": round(100.0 * kill_count / len(gaps), 2) if gaps else 0.0,
                "demo_hours": round(demo_hours, 3),
                "demo_hours_pct": round(100.0 * demo_hours / total_idle_hours, 2)
                if total_idle_hours
                else 0.0,
                "daemons_affected": len({g.daemon for g in kills}),
                "avg_kill_gap_min": round(
                    (sum(g.duration for g in kills) / kill_count) / 60.0, 2
                )
                if kill_count
                else 0.0,
                "reused_2h": reused_2h,
                "reused_2h_pct": round(100.0 * reused_2h / len(known), 2) if known else 0.0,
                "reused_24h": reused_24h,
                "reused_24h_pct": round(100.0 * reused_24h / len(known), 2) if known else 0.0,
                "reuse_unknown": unknown,
            }
        )

    tail_list = tails or []
    tail_stats: Dict[str, object] = {
        "tails": len(tail_list),
        "tail_demo_hours": round(sum(g.duration for g in tail_list) / 3600.0, 3),
        "tail_alive": sum(1 for g in tail_list if g.alive),
        "per_threshold": [],
    }
    for threshold_min in thresholds:
        threshold_s = threshold_min * 60
        tail_kills = [g for g in tail_list if g.duration > threshold_s]
        tail_stats["per_threshold"].append(  # type: ignore[union-attr]
            {
                "threshold_min": threshold_min,
                "tail_kills": len(tail_kills),
                "tail_demo_hours": round(
                    sum(g.duration for g in tail_kills) / 3600.0, 3),
                "tail_alive": sum(1 for g in tail_kills if g.alive),
            }
        )

    # Объединённая таблица: gaps + tails (то, что видит рейпер на практике).
    combined = gaps + tail_list
    combined_durations = sorted(g.duration for g in combined)
    per_threshold_all: List[Dict[str, object]] = []
    for threshold_min in thresholds:
        threshold_s = threshold_min * 60
        kills = [g for g in combined if g.duration > threshold_s]
        known = [g for g in kills if g.reuse_delta is not None]
        reused_2h = sum(1 for g in known if g.reuse_delta < reuse_window)
        per_threshold_all.append(
            {
                "threshold_min": threshold_min,
                "kill_events": len(kills),
                "kill_events_pct": round(
                    100.0 * len(kills) / len(combined), 2) if combined else 0.0,
                "demo_hours": round(sum(g.duration for g in kills) / 3600.0, 3),
                "daemons_affected": len({g.daemon for g in kills}),
                "from_tail": sum(1 for g in kills if g.is_tail),
                "still_alive": sum(1 for g in kills if g.alive),
                "reused_2h": reused_2h,
                "reused_2h_pct": round(
                    100.0 * reused_2h / len(known), 2) if known else 0.0,
                "reuse_unknown": len(kills) - len(known),
            }
        )

    return {
        "totals": totals,
        "per_threshold": per_threshold,
        "per_threshold_all": per_threshold_all,
        "tail": tail_stats,
    }


# ---------------------------------------------------------------------------
# Сбор данных по цели
# ---------------------------------------------------------------------------


def analyzable(spec: TargetSpec) -> Optional[str]:
    """Можно ли анализировать простой этой цели. None — можно.

    Возвращает причину, если нельзя: по «activity: mtime» маркеров нет,
    а без них «простоя между сборками» не существует.
    """
    if spec.activity != ACTIVITY_MARKERS:
        return (f"активность измеряется по mtime лога (activity={spec.activity}) — "
                f"маркеров начала/конца работы нет, простой между сборками "
                f"не определить")
    if not spec.build_start:
        return "в описании цели нет маркера начала работы (build_start)"
    if not spec.build_end and not spec.build_end_alt:
        return "в описании цели нет маркера конца работы (build_end)"
    return None


def discover_files(spec: TargetSpec,
                   overrides: Optional[Sequence[str]] = None) -> List[str]:
    """Раскрывает шаблоны путей цели в список существующих файлов (без дублей)."""
    patterns = list(overrides) if overrides else list(spec.log_globs)
    found: List[str] = []
    seen = set()
    for pattern in patterns:
        for path in sorted(globmod.glob(os.path.expanduser(pattern), recursive=True)):
            if path in seen:
                continue
            if not os.path.isfile(path):
                continue
            seen.add(path)
            found.append(path)
    return found


def analyze_target(spec: TargetSpec, thresholds: List[int], reuse_window: int,
                   now: float, max_bytes: int,
                   overrides: Optional[Sequence[str]] = None
                   ) -> Tuple[Dict[str, object], Dict[str, object]]:
    """Читает логи цели и считает статистику простоя по ним."""
    files = discover_files(spec, overrides)
    markers = Markers.from_spec(spec)
    ts_format = spec.ts_format

    scan_stats: Dict[str, object] = {
        "files_found": len(files),
        "files_read": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "files_without_markers": 0,
        "builds_total": 0,
        "builds_closed": 0,
        "builds_unclosed": 0,
        "idle_without_start": 0,
        "bad_timestamp_lines": 0,
        "tail_idle_hours": 0.0,
        "tail_idle_files": 0,
        "tail_observation_hours": 0.0,
        "tail_observation_files": 0,
        "alive_unknown_files": 0,
    }

    all_gaps: List[Gap] = []
    scans: List[Tuple[FileScan, Optional[bool]]] = []

    for path in files:
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            scan_stats["files_failed"] = int(scan_stats["files_failed"]) + 1
            print(f"ПРЕДУПРЕЖДЕНИЕ: не удалось получить размер {path}: {exc}", file=sys.stderr)
            continue

        if size > max_bytes:
            scan_stats["files_skipped"] = int(scan_stats["files_skipped"]) + 1
            print(
                f"ПРЕДУПРЕЖДЕНИЕ: пропущен большой лог {path}"
                f" ({size / 1024 / 1024:.1f} МБ > {max_bytes / 1024 / 1024:.0f} МБ)",
                file=sys.stderr,
            )
            continue

        try:
            scan = scan_file(path, markers=markers, ts_format=ts_format)
        except OSError as exc:
            # Битый/нечитаемый лог не должен ронять весь прогон.
            scan_stats["files_failed"] = int(scan_stats["files_failed"]) + 1
            print(f"ПРЕДУПРЕЖДЕНИЕ: не удалось прочитать {path}: {exc}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001 — единичный битый файл не валит прогон
            scan_stats["files_failed"] = int(scan_stats["files_failed"]) + 1
            print(f"ПРЕДУПРЕЖДЕНИЕ: ошибка разбора {path}: {exc}", file=sys.stderr)
            continue

        scan_stats["files_read"] = int(scan_stats["files_read"]) + 1
        scan_stats["builds_total"] = int(scan_stats["builds_total"]) + len(scan.builds)
        scan_stats["builds_unclosed"] = int(scan_stats["builds_unclosed"]) + scan.unclosed_builds
        scan_stats["builds_closed"] = int(scan_stats["builds_closed"]) + (
            len(scan.builds) - scan.unclosed_builds
        )
        scan_stats["idle_without_start"] = int(scan_stats["idle_without_start"]) + (
            scan.idle_without_start
        )
        scan_stats["bad_timestamp_lines"] = int(scan_stats["bad_timestamp_lines"]) + (
            scan.bad_timestamp_lines
        )

        if not scan.builds:
            scan_stats["files_without_markers"] = int(scan_stats["files_without_markers"]) + 1

        alive = daemon_alive(spec, path)
        if alive is None:
            scan_stats["alive_unknown_files"] = int(scan_stats["alive_unknown_files"]) + 1
        scans.append((scan, alive))
        all_gaps.extend(build_gaps(scan))

        # Хвост наблюдения: от последнего события в логе до mtime файла.
        # ВАЖНО: если последняя сборка не закрыта (лог оборван), хвост включает
        # время её выполнения, поэтому это верхняя оценка «чистого» простоя.
        last_event: Optional[float] = None
        for build in scan.builds:
            if last_event is None or build.start > last_event:
                last_event = build.start
            if build.end is not None and (last_event is None or build.end > last_event):
                last_event = build.end
        if last_event is not None:
            tail = scan.mtime - last_event
            if tail > 0:
                scan_stats["tail_observation_hours"] = (
                    float(scan_stats["tail_observation_hours"]) + tail / 3600.0
                )
                scan_stats["tail_observation_files"] = int(
                    scan_stats["tail_observation_files"]
                ) + 1

        # Чистый простой: только для логов, чья последняя сборка закрыта
        # маркером idle — тогда хвост гарантированно простой.
        if scan.builds and scan.builds[-1].end is not None:
            tail = scan.mtime - float(scan.builds[-1].end)
            if tail > 0:
                scan_stats["tail_idle_hours"] = float(scan_stats["tail_idle_hours"]) + tail / 3600.0
                scan_stats["tail_idle_files"] = int(scan_stats["tail_idle_files"]) + 1

    tail_gaps: List[Gap] = []
    for scan, alive in scans:
        gap = build_tail_gap(scan, now, alive)
        if gap is not None:
            tail_gaps.append(gap)

    report = aggregate(all_gaps, thresholds, reuse_window, tail_gaps)
    report["recommendation"] = pick_recommendation(
        report["per_threshold"], thresholds,  # type: ignore[arg-type]
        report.get("per_threshold_all"))

    totals: Dict[str, object] = report["totals"]  # type: ignore[assignment]
    totals["files"] = int(scan_stats["files_read"])
    totals["builds"] = int(scan_stats["builds_total"])
    totals["files_found"] = int(scan_stats["files_found"])
    totals["files_skipped"] = int(scan_stats["files_skipped"])
    totals["files_failed"] = int(scan_stats["files_failed"])
    totals["files_without_markers"] = int(scan_stats["files_without_markers"])
    totals["builds_closed"] = int(scan_stats["builds_closed"])
    totals["builds_unclosed"] = int(scan_stats["builds_unclosed"])
    totals["tail_idle_hours"] = round(float(scan_stats["tail_idle_hours"]), 3)
    totals["tail_observation_hours"] = round(float(scan_stats["tail_observation_hours"]), 3)
    totals["idle_without_start"] = int(scan_stats["idle_without_start"])
    totals["bad_timestamp_lines"] = int(scan_stats["bad_timestamp_lines"])
    totals["alive_unknown_files"] = int(scan_stats["alive_unknown_files"])

    return report, scan_stats


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------


def fmt(value: float, digits: int = 1) -> str:
    """Форматирование числа с фиксированным числом знаков."""
    return f"{value:,.{digits}f}".replace(",", " ")


def print_report(label: str, report: Dict[str, object],
                 scan_stats: Dict[str, object]) -> None:
    """Печатает человекочитаемую таблицу по одной цели."""
    totals: Dict[str, object] = report["totals"]  # type: ignore[assignment]
    per_threshold: List[Dict[str, object]] = report["per_threshold"]  # type: ignore[assignment]

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


def pick_recommendation(
    per_threshold: List[Dict[str, object]],
    thresholds: List[int],
    per_threshold_all: Optional[List[Dict[str, object]]] = None,
) -> Optional[str]:
    """
    Эвристика: берём минимальный порог, при котором доля ложных срабатываний
    (reuse ≤ 2 ч) не превышает допуск, и при этом порог реально что-то освобождает.
    Если такого порога нет — рекомендуем следующий за самым «полезным» порог:
    там kill-событий нет вовсе, значит ложных срабатываний тоже нет.
    """
    usable = [row for row in per_threshold if row["kill_events"] > 0]
    combined = {int(row["threshold_min"]): row for row in (per_threshold_all or [])}

    def _tail_part(threshold_min: int) -> str:
        row = combined.get(threshold_min)
        if not row:
            return ""
        return (
            f" Из них {row['from_tail']} — хвостовые (демон перестал работать"
            f" и висел дальше); для хвостов доля ложных срабатываний"
            f" ретроспективно не измеряется (после последней сборки демон"
            f" в этом логе больше не работал) — её даёт пилот dry-run."
        )

    if not usable:
        first = combined.get(int(thresholds[0]))
        if first and first["kill_events"] > 0:
            return (
                f"между сборками нет ни одного простоя длиннее {thresholds[0]} мин"
                f" (ложные срабатывания в этом классе структурно нулевые),"
                f" но есть {first['from_tail']} хвостовых простоев"
                f" ({fmt(first['demo_hours'], 1)} демо-часов)." + _tail_part(int(thresholds[0]))
            )
        return (
            f"ни одного простоя длиннее {thresholds[0]} мин — "
            f"любой порог ≥ {thresholds[0]} мин в этих данных не срабатывает"
        )

    for row in usable:
        if row["reused_2h_pct"] <= REUSE_TOLERANCE_PCT:
            return (
                f"порог {row['threshold_min']} мин — ложные срабатывания"
                f" {fmt(row['reused_2h_pct'], 1)}% (≤ {fmt(REUSE_TOLERANCE_PCT, 0)}%),"
                f" высвобождается {fmt(row['demo_hours'], 1)} демо-часов."
                + _tail_part(int(row["threshold_min"]))
            )

    # Ни один порог с kill-событиями не проходит по доле ложных срабатываний.
    worst = max(usable, key=lambda row: row["reused_2h_pct"])
    higher = [t for t in thresholds if t > int(worst["threshold_min"])]
    if higher:
        return (
            f"порог ≥ {higher[0]} мин — в данных нет ни одного простоя между сборками"
            f" длиннее {worst['threshold_min']} мин, значит в этом классе ложных"
            f" срабатываний нет; на самом пороге {worst['threshold_min']} мин ложных"
            f" срабатываний {fmt(worst['reused_2h_pct'], 1)}% — убивать на нём рискованно."
            + _tail_part(higher[0])
        )
    return (
        f"порог {worst['threshold_min']} мин — ложные срабатывания"
        f" {fmt(worst['reused_2h_pct'], 1)}%, повышать порог нельзя:"
        f" в рассмотренном диапазоне нет ни одного простоя длиннее"
        f" {worst['threshold_min']} мин"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_thresholds(raw: str) -> List[int]:
    """Разбирает строку порогов «30,60,120» в отсортированный список минут."""
    values: List[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            raise ValueError(f"не удалось разобрать порог: {chunk!r}") from None
        if value <= 0:
            raise ValueError(f"порог должен быть положительным: {value}")
        values.append(value)
    if not values:
        raise ValueError("не задано ни одного порога")
    return sorted(set(values))


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
    targets: TargetSet = resolve_targets(load_targets(args.targets, warnings),
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
