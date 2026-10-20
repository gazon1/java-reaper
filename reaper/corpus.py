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

# Правила, которые защищают демона (не убивают) и правила, которые убивают.
# Разделение нужно, чтобы отчёт различал «порог не проверен, потому что
# правило ни разу не сработало» и «правило сработало и оставило демона
# живым по другой причине».
KILL_RULES = frozenset({"kill"})

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


def rule_coverage(sweeps: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Покрытие правил: какие срабатывали, какие — ни разу.

    По журналу видно, какие пороги вообще проверены на живых демонах.
    Правило, которое не сработало ни разу, оставляет свой порог
    непроверенным: значение стоит, но никто не знает, верное ли оно.
    """
    hits: Dict[str, int] = {}
    for row in sweeps:
        for rule, count in (row.get("rules") or {}).items():
            hits[rule] = hits.get(rule, 0) + int(count or 0)
    fired = {rule: count for rule, count in hits.items() if count > 0}
    never = sorted(rule for rule, count in hits.items() if count == 0)
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
