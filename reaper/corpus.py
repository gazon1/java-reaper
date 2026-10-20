"""Чтение накопленных журналов и меры по корпусу.

Журналы рейпера — это данные, накопленные наблюдением: журнал свипов
(`sweeps.jsonl`) и история сводок по демонам (`logs-history.jsonl`). Ими
пользуются несколько инструментов (симулятор, прибор измерения порогов,
месячный отчёт), и все они должны считать ОДНО И ТО ЖЕ: иначе цифры
разъедутся, и сравнивать их будет нельзя.

Поэтому чтение журналов и меры живут здесь, а инструменты занимаются своим
делом: один моделирует правила, другой меряет пороги.

Особое место занимает «покрытие правил»: по журналу свипов видно, какие
правила срабатывали, а какие — ни разу. Это прямой ответ на вопрос
«какие пороги проверены, а какие висят на одном не проверенном значении».
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Правила, которые не выполнялись НИ РАЗУ за весь журнал: значит, порог,
# который за ними стоит, не проверен ни на одном реальном демоне.
UNPROVEN_HINT = "ни разу не сработало — порог за этим правилом не проверен"


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    """Читает JSONL-журнал, пропуская битые строки.

    Одна нечитаемая строка не должна ронять разбор: журнал растёт месяцами,
    и потерять его из-за одного сбоя записи — обидно.
    """
    rows: List[Dict[str, Any]] = []
    if not path or not os.path.isfile(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def percentile(values: Sequence[float], q: float) -> float:
    """Перцентиль (линейная интерполяция, как в numpy)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * (q / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] * (1.0 - frac) + ordered[high] * frac)


def human(value: float, digits: int = 1) -> str:
    """Число с разделителем разрядов (для читаемого вывода)."""
    return f"{value:,.{digits}f}".replace(",", " ")


# --------------------------------------------------------------------------- #
# Меры простоя по сводке демона
# --------------------------------------------------------------------------- #


def gaps_between_builds(record: Dict[str, Any]) -> Optional[float]:
    """Простой МЕЖДУ сборками, секунды (сумма промежутков).

    Это «живой простой»: сколько демон ждал между сборками, пока ещё был
    нужен. Порог простоя обязан быть выше него — иначе мы будем мешать
    работающей программе.
    """
    builds = record.get("builds") or []
    if not builds:
        return None
    total = 0.0
    for i in range(len(builds) - 1):
        end = builds[i][1]
        if end is None:
            continue
        start_next = builds[i + 1][0]
        if start_next is not None and start_next > end:
            total += start_next - end
    return total


def tail_idle(record: Dict[str, Any]) -> Optional[float]:
    """Простой от последней работы до смерти демона, секунды.

    Именно это решает рейпер: «простой = прошло с последней измеренной
    активности». Измерить можно там, где момент смерти известен — то есть
    где рейпер заметил пропажу демона.

    Границы: момент смерти известен с точностью до интервала свипа — демон
    умер где-то между тем, как его видели в последний раз, и тем, как его
    заметили, что он исчез. Если в сводке есть `last_seen_at`, это нижняя
    граница (демон точно жил в этот момент), а `death_observed_at` —
    верхняя. Без нижней границы оценка завышена: мы считаем, что демон
    жил до момента обнаружения, хотя он умер раньше.
    """
    last = record.get("last_activity")
    death = record.get("death_observed_at")
    if last is None or death is None or death <= last:
        return None
    return death - last


def tail_idle_bounds(record: Dict[str, Any]) -> Optional[tuple[float, float]]:
    """Границы хвостового простоя: (нижняя, верхняя) в секундах.

    Нижняя граница — простой до последнего подтверждённого sightings'а
    (демон точно жил), верхняя — до момента обнаружения пропажи. Когда
    нижняя граница известна, её и берём: она не завышает простой и потому
    не создаёт мнимых «долгих простоев».
    """
    upper = tail_idle(record)
    if upper is None:
        return None
    last = record.get("last_activity")
    seen = record.get("last_seen_at")
    if last is None or seen is None or seen <= last:
        return (upper, upper)
    return (seen - last, upper)


# --------------------------------------------------------------------------- #
# Журнал свипов
# --------------------------------------------------------------------------- #


def rule_coverage(sweeps: Sequence[Dict[str, Any]],
                  known_rules: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Покрытие правил: какие срабатывали, какие — ни разу.

    По журналу видно, какие пороги вообще проверены на живых демонах.
    Правило, которое не сработало ни разу, оставляет свой порог
    непроверенным: значение стоит, но никто не знает, верное ли оно.

    `known_rules` — полный список правил политики. Без него «ни разу не
    сработало» пусто: журнал хранит только те правила, которые сработали, и
    молчание о нём означало бы «всё проверено».
    """
    hits: Dict[str, int] = {}
    for row in sweeps:
        for rule, count in (row.get("rules") or {}).items():
            hits[rule] = hits.get(rule, 0) + int(count or 0)
    fired = {rule: count for rule, count in hits.items() if count > 0}
    never = sorted(rule for rule, count in hits.items() if count == 0)
    if known_rules is not None:
        never = sorted(set(known_rules) - set(fired))
    return {
        "sweeps": len(sweeps),
        "hits": dict(sorted(hits.items())),
        "fired": dict(sorted(fired.items())),
        "never_fired": never,
    }


def sweeps_with_pressure(sweeps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Свипы, в которых что-то висело под ударом (would_free_mb > 0)."""
    return [row for row in sweeps if float(row.get("would_free_mb", 0) or 0) > 0]


# --------------------------------------------------------------------------- #
# «Зависшие» демоны — единственный класс, где рейпер нужен
# --------------------------------------------------------------------------- #


def is_stuck(record: Dict[str, Any]) -> bool:
    """Завис ли демон: клиент отвалился во время сборки.

    Это единственный класс, который Gradle сам НЕ убирает: его idle-таймаут
    не действует, пока демон считает себя «в сборке». Значит, это и есть
    работа для рейпера — всё остальное программы убирают сами.
    """
    return record.get("canceled_at") is not None


def death_bounds(record: Dict[str, Any]) -> Optional[tuple[float, float, str]]:
    """Границы момента смерти демона: (нижняя, верхняя, откуда).

    Источники, по убыванию точности:
      * `last_seen_at` — демон точно жил в этот момент (нижняя граница);
      * `death_observed_at` — мы заметили пропажу (верхняя граница: демон
        умер где-то между последним sightings'ом и обнаружением);
      * `mtime` лога — последняя запись: оценка «умер сразу после последней
        активности» (её же использует симулятор).

    Берём нижнюю границу: она не завышает время жизни и не создаёт мнимых
    «долгих простоев».
    """
    observed = record.get("death_observed_at")
    mtime = record.get("mtime")
    upper: Optional[float] = None
    if observed is not None:
        upper = float(observed)
    elif mtime is not None:
        upper = float(mtime)
    if upper is None:
        return None
    seen = record.get("last_seen_at")
    if seen is not None and float(seen) <= upper:
        return (float(seen), upper, "last_seen")
    if observed is not None:
        return (upper, upper, "death_mark")
    return (upper, upper, "mtime")


def stuck_lifetime(record: Dict[str, Any]) -> Optional[tuple[float, str]]:
    """Сколько зависший демон прожил после отмены: (секунды, откуда оценка)."""
    canceled = record.get("canceled_at")
    if canceled is None:
        return None
    bounds = death_bounds(record)
    if bounds is None:
        return None
    lower, _upper, source = bounds
    lifetime = lower - float(canceled)
    if lifetime < 0:
        return None
    return (lifetime, source)


def footprint_per_daemon_mb(sweeps: Sequence[Dict[str, Any]],
                            kind: str) -> List[float]:
    """Footprint одного живого демона вида, МБ — по журналу свипов.

    В свипе хранится суммарный footprint по видам, а не по демонам, поэтому
    «сколько занимает один демон» получается делением суммы на число демонов.
    Это оценка, а не измерение отдельного демона: демоны одного вида различаются
    по настройке JVM. Но для ответа «сколько памяти висело на зависших» нужна
    именно величина порядка — и она отсюда берётся честно.
    """
    values: List[float] = []
    for row in sweeps:
        bucket = (row.get("by_kind") or {}).get(kind) or {}
        count = int(bucket.get("count") or 0)
        if count <= 0:
            continue
        values.append(float(bucket.get("footprint_mb") or 0) / count)
    return values


def stuck_memory_report(records: Iterable[Dict[str, Any]],
                        sweeps: Sequence[Dict[str, Any]],
                        threshold_seconds: float) -> Dict[str, Any]:
    """Сколько памяти удержали зависшие демоны — и стоит ли их класс.

    Отвечает на вопрос «а может, этот класс не нужен?» тремя числами:
      * сколько демонов зависло и сколько демон-часов они удержали память;
      * сколько из них пережило порог «завис» (остальные умерли сами, и
        рейперу они не нужны);
      * сколько памяти в среднем висело на зависших в любой момент.

    Последнее число — решающее: если удерживаемая память мала по сравнению со
    свободной, класс не создаёт давления, и убирать его незачем.
    """
    lifetimes: List[float] = []
    over: List[float] = []
    hours_by_kind: Dict[str, float] = {}
    first: Optional[float] = None
    last: Optional[float] = None
    kinds: Dict[str, int] = {}
    for record in records:
        if not is_stuck(record):
            continue
        kind = str(record.get("kind") or "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
        canceled = record.get("canceled_at")
        bounds = death_bounds(record)
        if canceled is None or bounds is None:
            continue
        lower, upper, _source = bounds
        lifetime = lower - float(canceled)
        if lifetime < 0:
            continue
        lifetimes.append(lifetime)
        hours_by_kind[kind] = hours_by_kind.get(kind, 0.0) + lifetime / 3600.0
        if lifetime > threshold_seconds:
            over.append(lifetime)
        for stamp in (float(canceled), lower):
            first = stamp if first is None else min(first, stamp)
            last = stamp if last is None else max(last, stamp)

    period_hours = ((last - first) / 3600.0) if (first is not None and last is not None
                                                     and last > first) else 0.0
    total_hours = sum(lifetimes) / 3600.0
    over_hours = sum(v - threshold_seconds for v in over) / 3600.0

    footprint_median: Dict[str, float] = {}
    for kind in kinds:
        values = footprint_per_daemon_mb(sweeps, kind)
        if values:
            footprint_median[kind] = percentile(values, 50)
    # Сколько памяти висело на зависших в среднем за период: сумма
    # «демон-часы × footprint демона» делим на длину периода.
    held_mb_total = sum(hours_by_kind.get(kind, 0.0) * footprint_median.get(kind, 0.0)
                         for kind in kinds)
    held_mb = held_mb_total / period_hours if period_hours > 0 else 0.0
    typical_footprint = (percentile(list(footprint_median.values()), 50)
                         if footprint_median else 0.0)

    return {
        "count": sum(kinds.values()),
        "count_by_kind": kinds,
        "measured": len(lifetimes),
        "threshold_seconds": threshold_seconds,
        "over_threshold": len(over),
        "over_threshold_pct": (round(100.0 * len(over) / len(lifetimes), 1)
                               if lifetimes else 0.0),
        "daemon_hours_total": total_hours,
        "daemon_hours_over_threshold": over_hours,
        "period_hours": period_hours,
        "footprint_median_mb": footprint_median,
        "held_mb_average": held_mb,
        "held_mb_average_over_threshold": (
            over_hours * typical_footprint / period_hours
            if period_hours > 0 else 0.0),
    }


def stuck_summaries(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Сводка по зависшим демонам: сколько их и сколько они прожили."""
    by_kind: Dict[str, List[float]] = {}
    count_by_kind: Dict[str, int] = {}
    sources: Dict[str, int] = {}
    for record in records:
        if not is_stuck(record):
            continue
        kind = str(record.get("kind") or "unknown")
        count_by_kind[kind] = count_by_kind.get(kind, 0) + 1
        measured = stuck_lifetime(record)
        if measured is not None:
            lifetime, source = measured
            by_kind.setdefault(kind, []).append(lifetime)
            sources[source] = sources.get(source, 0) + 1
    return {"count_by_kind": count_by_kind, "lifetimes_by_kind": by_kind,
            "sources": sources}
