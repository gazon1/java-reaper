#!/usr/bin/env python3
"""Симуляция правил рейпера по истории демонов.

Пилот — это 7 дней ожидания и 8 накопленных событий, чтобы получить ответ
на вопрос «сколько ложных срабатываний у наших правил». Симулятор отвечает
на тот же вопрос по ВСЕЙ накопленной истории — за минуты.

Данные берутся из `logs-history.jsonl`: компактные сводки, которые
`tools/prune-logs.py` сохраняет перед удалением логов (см. reaper/history.py).
Каждая сводка — это жизнь одного демона: когда жил, когда шли сборки, была
ли отмена.

Модель:

  * демон жив с `first_activity` до `mtime` (последняя запись в лог);
  * «занят» — внутри интервала активности (сборка для Gradle, корзина
    активности для Kotlin);
  * простой считается от конца последней активности, а во время сборки —
    от её начала (так же, как в рейпере: демон с начатой сборкой простаивает
    «с начала сборки»);
  * убийство происходит на первом свипе, где простой превысил порог.

Чего модель НЕ знает (и потому занижает точность):

  * размер демона (правило `small-footprint`) — считаем, что прошёл;
  * клиенты и наблюдатели (правило `peer`) — считаем, что защиты не было;
  * CPU и дети (правила `cpu-busy`, `children`) — считаем, что демон не работал.

Поэтому симулятор даёт ВЕРХНЮЮ ОЦЕНКУ количества убийств: реальный рейпер
убил бы меньше. Для вопроса «какая доля ложных срабатываний» это, наоборот,
консервативная оценка.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reaper.history import (  # noqa: E402
    default_history_path,
    effective_canceled,
    intervals as history_intervals,
    read_history,
)

# Порог простоя Gradle-демона, при котором его убивает САМ Gradle
# (idleTimeout=10800000 мс = 180 мин). Демон, простоявший дольше, до нашего
# свипа просто не доживает.
GRADLE_SELF_TIMEOUT = 180 * 60


def _death(record: dict[str, Any]) -> float:
    """Когда демон умер (последняя запись в лог)."""
    return float(record.get("mtime") or 0.0)


def _first(record: dict[str, Any]) -> float:
    """Начало жизни демона.

    Значения проверяются на None, а не на truthiness: момент 0.0 (эпоха) —
    корректное значение, и `or` его бы потерял.
    """
    for key in ("first_activity", "last_activity", "mtime"):
        value = record.get(key)
        if value is not None:
            return float(value)
    return 0.0


def _gradle_self_death(spans: list[tuple[float, float]]) -> float:
    """Когда Gradle убьёт демона СВОИМ idle-таймаутом.

    У Gradle есть собственный таймаут (180 мин): демон, простоявший дольше,
    умирает сам. Значит, наш порог выше 180 мин бесполезен для Gradle —
    к моменту нашего свипа демона уже нет.
    """
    closed = [end for _start, end in spans if end != float("inf")]
    if not closed:
        return float("inf")
    return max(closed) + GRADLE_SELF_TIMEOUT


def kill_time(record: dict[str, Any], threshold: float,
              stuck_threshold: float = 3600.0) -> float | None:
    """Момент, когда рейпер убил бы демона (или None — не убил бы).

    threshold      — порог простоя в секундах;
    stuck_threshold — порог для «зависших» (клиент отвалился во время
                     сборки): их не жалко держать, работать им некому.
    """
    spans = sorted(history_intervals(record))
    if not spans:
        return None
    death = _death(record)
    canceled = effective_canceled(record)

    # Зависший демон: клиент отвалился, сборка не завершилась. Gradle его
    # не убирает (таймаут не действует, пока демон «в сборке»), поэтому
    # смерть демона — это его собственная, а не таймаут Gradle.
    if canceled is not None:
        kt = float(canceled) + stuck_threshold
        return kt if kt < death else None

    # Граница жизни демона: собственная смерть или таймаут Gradle — что
    # раньше.
    limit = death
    if record.get("kind") == "gradle":
        limit = min(limit, _gradle_self_death(spans))

    cursor = _first(record)
    for start, end in spans:
        if end == float("inf"):
            # Сборка без конца, но без маркера отмены: демон умер во время
            # сборки. Простой считается от начала сборки.
            kt = start + threshold
            return kt if kt < limit else None
        # Простой перед этой активностью
        if start - cursor > threshold:
            kt = cursor + threshold
            return kt if kt < limit else None
        # Сама активность длиннее порога: демон «в сборке» дольше порога.
        # Рейпер считает простой от начала сборки и убьёт демона.
        if end - start > threshold:
            kt = start + threshold
            return kt if kt < limit else None
        cursor = max(cursor, end)
    # Простой после последней активности — до самой смерти демона.
    if limit - cursor > threshold:
        kt = cursor + threshold
        return kt if kt < limit else None
    return None


def reused_after(record: dict[str, Any], moment: float) -> bool:
    """Понадобился ли демон снова после момента `moment`."""
    for start, _end in history_intervals(record):
        if start > moment:
            return True
    return False


def simulate(records: list[dict[str, Any]], thresholds: dict[str, float],
             stuck_threshold: float = 3600.0) -> dict[str, Any]:
    """Прогоняет пороги по истории и считает убийства и ложные срабатывания."""
    out: dict[str, Any] = {}
    for kind in ("gradle", "kotlin"):
        items = [r for r in records if r.get("kind") == kind]
        for label, threshold in thresholds.items():
            kills = 0
            false_positives = 0
            died_own = 0
            stuck = 0
            for rec in items:
                if effective_canceled(rec) is not None:
                    stuck += 1
                kt = kill_time(rec, threshold, stuck_threshold)
                if kt is None:
                    died_own += 1
                    continue
                kills += 1
                if reused_after(rec, kt):
                    false_positives += 1
            out.setdefault(kind, {})[label] = {
                "demons": len(items),
                "kills": kills,
                "false_positives": false_positives,
                "died_own": died_own,
                "stuck": stuck,
                "rate": (false_positives / kills) if kills else 0.0,
            }
    return out


def format_report(result: dict[str, Any], records: list[dict[str, Any]],
                  thresholds: dict[str, float]) -> str:
    lines = ["Симуляция правил по истории демонов", "=" * 72]
    if not records:
        lines.append("  история пуста — нечего симулировать")
        return "\n".join(lines)
    firsts = [r.get("first_activity") or r.get("mtime") or 0 for r in records]
    lasts = [r.get("mtime") or 0 for r in records]
    span = (max(lasts) - min(firsts)) / 86400.0 if lasts else 0.0
    lines.append(f"  записей: {len(records)}, период: {span:.1f} сут "
                 f"({time.strftime('%d.%m %H:%M', time.localtime(min(firsts)))} — "
                 f"{time.strftime('%d.%m %H:%M', time.localtime(max(lasts)))})")
    lines.append("")
    lines.append("  Модель не знает про размер, клиентов и CPU → убийств тут "
                 "верхняя оценка, а доля ложных — консервативная.")
    lines.append("")
    for kind in ("gradle", "kotlin"):
        if kind not in result:
            continue
        lines.append(f"  {kind}:")
        lines.append(f"    {'порог':>10} {'убито':>7} {'ложных':>8} {'доля':>7} "
                     f"{'зависших':>9} {'умерли сами':>13}")
        for label, data in result[kind].items():
            lines.append(f"    {label:>10} {data['kills']:>7} "
                         f"{data['false_positives']:>8} "
                         f"{100 * data['rate']:>6.0f} % {data['stuck']:>9} "
                         f"{data['died_own']:>13}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Симуляция правил рейпера по истории демонов")
    parser.add_argument("--history", default=None,
                        help="файл истории сводок (по умолчанию "
                             "logs-history.jsonl в каталоге состояния)")
    parser.add_argument("--thresholds", default=None,
                        help="пороги простоя в минутах через запятую; "
                             "по умолчанию — из конфига рейпера")
    parser.add_argument("--json", action="store_true",
                        help="машиночитаемый вывод")
    args = parser.parse_args(argv)

    history_path = args.history or default_history_path()
    records = read_history(history_path)
    if not records:
        print(f"история пуста или не найдена: {history_path}", flush=True)
        return 1

    thresholds: dict[str, float] = {}
    stuck_threshold = 3600.0
    if args.thresholds:
        for token in args.thresholds.replace(",", " ").split():
            try:
                minutes = float(token)
            except ValueError:
                continue
            thresholds[f"{minutes:g} мин"] = minutes * 60
    else:
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        from reaper.config import load_config

        cfg = load_config(None)
        gradle = float(cfg["IDLE_SECONDS"]) / 60
        kotlin = float(cfg.get("KOTLIN_IDLE_SECONDS", cfg["IDLE_SECONDS"])) / 60
        thresholds = {f"{gradle:g} мин": gradle * 60, f"{kotlin:g} мин": kotlin * 60}
        stuck_threshold = float(cfg.get("STUCK_AFTER_SECONDS", 3600))

    result = simulate(records, thresholds, stuck_threshold)
    if args.json:
        print(json.dumps({"history": history_path, "records": len(records),
                          "thresholds": thresholds,
                          "stuck_threshold_seconds": stuck_threshold,
                          "result": result},
                         ensure_ascii=False, indent=1, sort_keys=True), flush=True)
    else:
        print(format_report(result, records, thresholds), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
