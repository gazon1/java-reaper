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

МОДЕЛЬ, А НЕ РЕЙПЕР. Настоящие правила живут в `reaper/policy.py`, и они
умеют то, чего не умеет эта модель (размер, клиенты, CPU, дети). Модель
отвечает на более узкий вопрос: «когда рейпер убил бы демона, если бы
смотрел только на простой». Поэтому:

  * сравнения простоя сделаны как в правиле `not-idle` рейпера (убиваем на
    пороге, а не после него) — иначе на границе модель и рейпер говорили бы
    разное;
  * свип считается непрерывным, а рейпер свипает раз в `SWEEP_SECONDS`:
    в реальности убийство наступит позже, чем в модели, — на интервал свипа;
  * расхождение моделей ловится тестом `tests/test_model_drift.py`: он
    строит одну ситуацию с двух сторон и требует, чтобы выводы совпали.

Чего модель НЕ знает (и потому занижает точность):

  * размер демона (правило `small-footprint`) — считаем, что прошёл;
  * клиенты и наблюдатели (правило `peer`) — считаем, что защиты не было;
  * CPU и дети (правила `cpu-busy`, `children`) — считаем, что демон не работал;
  * момент смерти, если демона видел рейпер (`death_observed_at`): без этой
    отметки лог замолкает в момент последней активности, и простой виден
    лишь у тех демонов, чей лог кто-то ещё пережил (Gradle, у которого есть
    маркеры сборок). Для Kotlin без отметки рейпера простой не виден вовсе.

Поэтому симулятор даёт ВЕРХНЮЮ ОЦЕНКУ количества убийств: реальный рейпер
убил бы меньше. Для вопроса «какая доля ложных срабатываний» это, наоборот,
консервативная оценка.

Два независимых скана:

  * порог простоя (`--thresholds`) — сколько демонов переживёт застой;
  * порог «зависшего» (`--stuck-thresholds`) — сколько зависших демонов
    переживёт отведённое им время. Это отдельная популяция: зависший демон
    умирает не сам (таймаут Gradle не действует, пока демон «в сборке»),
    и убираем его мы — значит, порог надо измерять, а не выбирать на глаз.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reaper.config import load_config, resolve_targets  # noqa: E402
from reaper.history import (  # noqa: E402
    default_history_path,
    effective_canceled,
    intervals as history_intervals,
    read_history,
)
from reaper.targets import TargetSet, builtin_targets, load_targets  # noqa: E402

# Порог простоя, при котором демона убивает САМА программа. У Gradle это
# idleTimeout=10800000 мс = 180 мин: демон, простоявший дольше, до нашего
# свипа просто не доживает. Значение приходит из спецификации цели
# (targets.yaml → self_timeout_seconds) и пишется в сводку демона, поэтому
# симулятор не знает про конкретные программы.
GRADLE_SELF_TIMEOUT = 180 * 60


def _death(record: dict[str, Any]) -> float:
    """Когда демон умер.

    Обычно это последняя запись в логе (`mtime`). Но для демона, который
    простаивал и потом умер, лог замолчал задолго до смерти: последняя
    запись — это конец активности, а не конец жизни. Поэтому, если свидетелем
    смерти был рейпер (`death_observed_at`), берём более позднее из двух:
    это верхняя оценка простоя, и она честнее, чем «умер сразу после
    последней записи».
    """
    mtime = float(record.get("mtime") or 0.0)
    observed = record.get("death_observed_at")
    if observed is None:
        return mtime
    return max(mtime, float(observed))


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


def _self_death(spans: list[tuple[float, float]],
                self_timeout: float) -> float:
    """Когда демона убьёт САМА программа (свой idle-таймаут).

    У Gradle есть собственный таймаут (180 мин): демон, простоявший дольше,
    умирает сам. Значит, наш порог выше 180 мин бесполезен для Gradle —
    к моменту нашего свипа демона уже нет.
    """
    closed = [end for _start, end in spans if end != float("inf")]
    if not closed:
        return float("inf")
    return max(closed) + self_timeout


def kill_time(record: dict[str, Any], threshold: float,
              stuck_threshold: float = 3600.0,
              self_timeout: float | None = None) -> float | None:
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
    own_timeout = record.get("self_timeout_seconds")
    if own_timeout is None:
        own_timeout = self_timeout
    if own_timeout:
        limit = min(limit, _self_death(spans, float(own_timeout)))

    cursor = _first(record)
    for start, end in spans:
        if end == float("inf"):
            # Сборка без конца, но без маркера отмены: демон умер во время
            # сборки. Простой считается от начала сборки.
            kt = start + threshold
            return kt if kt < limit else None
        # Простой перед этой активностью.
        # Сравнение `>=`, а не `>`, — как в правиле `not-idle` рейпера:
        # демон простаивает, пока простой МЕНЬШЕ порога («простой < порога ⇒
        # оставить»), то есть на самом пороге его уже убивают. Модель, где
        # на пороге демон ещё жив, расходилась бы с рейпером ровно на
        # границе (tests/test_model_drift.py).
        if start - cursor >= threshold:
            kt = cursor + threshold
            return kt if kt < limit else None
        # Сама активность длиннее порога: демон «в сборке» дольше порога.
        # Рейпер считает простой от начала сборки и убьёт демона.
        if end - start >= threshold:
            kt = start + threshold
            return kt if kt < limit else None
        cursor = max(cursor, end)
    # Простой после последней активности — до самой смерти демона.
    if limit - cursor >= threshold:
        kt = cursor + threshold
        return kt if kt < limit else None
    return None


def reused_after(record: dict[str, Any], moment: float) -> bool:
    """Понадобился ли демон снова после момента `moment`."""
    for start, _end in history_intervals(record):
        if start > moment:
            return True
    return False


def stuck_lifetime(record: dict[str, Any]) -> float | None:
    """Сколько секунд прожил зависший демон после отмены сборки.

    None — демон не завис (отмены не было, либо она была перекрыта
    последующей сборкой).
    """
    canceled = effective_canceled(record)
    if canceled is None:
        return None
    return max(0.0, _death(record) - float(canceled))


def simulate_stuck(records: list[dict[str, Any]],
                   stuck_thresholds: dict[str, float]) -> dict[str, Any]:
    """Сканирует порог «зависшего» (STUCK_AFTER_SECONDS) по истории.

    Зависшие демоны — отдельная популяция: их убирает рейпер, а не Gradle
    (его idle-таймаут не действует, пока демон «в сборке»). Порог для них
    свой, и он должен переизмеряться по мере накопления истории: слишком
    малый — убьём демона, который ещё понадобится; слишком большой — будем
    держать мусор часами.

    Для каждого порога: сколько зависших демонов пережило его (то есть были
    бы убиты), сколько из них после этого понадобились снова (ложные
    срабатывания) и как долго зависшие живут в среднем.

    Порог простоя здесь НЕ участвует: у зависшего демона момент смерти
    определяется только порогом «зависшего» (см. kill_time).

    О ЛОЖНЫХ СРАБАТЫВАНИЯХ. В корпусе они не измеряются: если клиент вернулся
    к тому же демону, то демон не был зависшим (см. effective_canceled) — то
    есть повторное использование видно только как «это не зависший». Настоящее
    ложное срабатывание (клиент вернулся уже к другому демону) в сводках не
    видно. Поэтому «ложных» здесь честно ноль, и это НЕ доказательство
    безопасности правила — это слепое пятно измерения.
    """
    out: dict[str, Any] = {}
    for kind in _kinds(records):
        items = [r for r in records if r.get("kind") == kind]
        stuck_items = [r for r in items if effective_canceled(r) is not None]
        lifetimes = [stuck_lifetime(r) or 0.0 for r in stuck_items]
        for label, threshold in stuck_thresholds.items():
            kills = 0
            false_positives = 0
            for rec, lifetime in zip(stuck_items, lifetimes):
                if lifetime <= threshold:
                    continue
                kills += 1
                canceled = float(effective_canceled(rec))
                if reused_after(rec, canceled + threshold):
                    false_positives += 1
            out.setdefault(kind, {})[label] = {
                "stuck": len(stuck_items),
                "kills": kills,
                "false_positives": false_positives,
                "rate": (false_positives / kills) if kills else 0.0,
                "max_lifetime_seconds": max(lifetimes) if lifetimes else 0.0,
                "median_lifetime_seconds": (statistics.median(lifetimes)
                                            if lifetimes else 0.0),
            }
    return out


def _duration(seconds: float) -> str:
    """Время по-человечески: минуты, пока не час; дальше — часы."""
    if seconds < 3600:
        return f"{seconds / 60:.0f} мин"
    return f"{seconds / 3600:.1f} ч"


def format_stuck_report(result: dict[str, Any]) -> str:
    """Таблица скана порога «зависшего»."""
    lines = ["Порог «зависшего» (STUCK_AFTER_SECONDS) — скан по истории",
             "=" * 72,
             "  Зависший демон = клиент отвалился во время сборки. Gradle его",
             "  не убирает (таймаут не действует, пока демон «в сборке»),",
             "  поэтому убираем мы — и порог должен быть измерен, а не выбран.",
             ""]
    for kind in result:
        lines.append(f"  {kind}:")
        lines.append(f"    {'порог':>10} {'зависших':>9} {'убито':>7} "
                     f"{'ложных':>8} {'доля':>7} {'медиана':>9} {'максимум':>9}")
        for label, data in result[kind].items():
            lines.append(f"    {label:>10} {data['stuck']:>9} {data['kills']:>7} "
                         f"{data['false_positives']:>8} "
                         f"{100 * data['rate']:>6.0f} % "
                         f"{_duration(data['median_lifetime_seconds']):>9} "
                         f"{_duration(data['max_lifetime_seconds']):>9}")
        lines.append("")
    return "\n".join(lines)


def _kinds(records: list[dict[str, Any]]) -> list[str]:
    """Виды демонов в корпусе — в порядке появления."""
    seen: list[str] = []
    for record in records:
        kind = str(record.get("kind") or "")
        if kind and kind not in seen:
            seen.append(kind)
    return seen


def simulate(records: list[dict[str, Any]], thresholds: dict[str, float],
             stuck_threshold: float = 3600.0,
             targets: TargetSet | None = None) -> dict[str, Any]:
    """Прогоняет пороги по истории и считает убийства и ложные срабатывания."""
    target_set = targets or builtin_targets()
    out: dict[str, Any] = {}
    for kind in _kinds(records):
        items = [r for r in records if r.get("kind") == kind]
        spec = target_set.get(kind)
        self_timeout = spec.self_timeout_seconds if spec is not None else None
        for label, threshold in thresholds.items():
            kills = 0
            false_positives = 0
            died_own = 0
            stuck = 0
            for rec in items:
                if effective_canceled(rec) is not None:
                    stuck += 1
                kt = kill_time(rec, threshold, stuck_threshold, self_timeout)
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
    for kind in result:
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
    parser.add_argument("--stuck-thresholds", default=None,
                        help="пороги «зависшего» в минутах через запятую; "
                             "сканирует STUCK_AFTER_SECONDS по накопленной "
                             "истории (по умолчанию — из конфига)")
    parser.add_argument("--targets", default=None,
                        help="путь к файлу целей (YAML)")
    parser.add_argument("--json", action="store_true",
                        help="машиночитаемый вывод")
    args = parser.parse_args(argv)

    history_path = args.history or default_history_path()
    records = read_history(history_path)
    if not records:
        # Нет истории — это не поломка прибора, а отсутствие данных. Прибор
        # обязан сказать об этом и выйти с кодом 0: иначе он не запустится
        # на машине, где рейпер ещё не накопил ни одной сводки.
        if args.json:
            print(json.dumps({"history": history_path, "records": 0,
                              "thresholds": {}, "stuck_threshold_seconds": None,
                              "stuck_thresholds": {}, "result": None,
                              "stuck_scan": None},
                             ensure_ascii=False, indent=1, sort_keys=True), flush=True)
        else:
            print(f"история пуста или не найдена: {history_path}", flush=True)
            print(format_report({}, [], {}), flush=True)
        return 0

    thresholds: dict[str, float] = {}
    stuck_threshold = 3600.0
    # Цели (targets.yaml) — те же, что у рейпера: пороги простоя берём
    # оттуда, чтобы симулятор считал по тем же правилам, что и живой свип.
    warnings: list[str] = []
    cfg = load_config(None, warnings)
    targets = resolve_targets(load_targets(args.targets, warnings), cfg,
                              set(), warnings)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr, flush=True)
    if args.thresholds:
        for token in args.thresholds.replace(",", " ").split():
            try:
                minutes = float(token)
            except ValueError:
                continue
            thresholds[f"{minutes:g} мин"] = minutes * 60
    else:
        for name, spec in targets.specs_by_name().items():
            limit = spec.idle_seconds
            if limit is None:
                continue
            thresholds[f"{limit / 60:g} мин"] = limit
        stuck_threshold = float(cfg.get("STUCK_AFTER_SECONDS", 3600))

    # Скан порога «зависшего»: по умолчанию — сам порог из конфига плюс
    # несколько точек вокруг, чтобы был виден ход зависимости, а не одна
    # цифра. Явно заданный список — как есть.
    if args.stuck_thresholds:
        stuck_thresholds: dict[str, float] = {}
        for token in args.stuck_thresholds.replace(",", " ").split():
            try:
                minutes = float(token)
            except ValueError:
                continue
            stuck_thresholds[f"{minutes:g} мин"] = minutes * 60
    else:
        stuck_thresholds = {f"{stuck_threshold / 60:g} мин": stuck_threshold}
    stuck_result = simulate_stuck(records, stuck_thresholds)

    result = simulate(records, thresholds, stuck_threshold, targets)
    if args.json:
        print(json.dumps({"history": history_path, "records": len(records),
                          "thresholds": thresholds,
                          "stuck_threshold_seconds": stuck_threshold,
                          "stuck_thresholds": stuck_thresholds,
                          "result": result,
                          "stuck_scan": stuck_result},
                         ensure_ascii=False, indent=1, sort_keys=True), flush=True)
    else:
        print(format_report(result, records, thresholds), flush=True)
        print(format_stuck_report(stuck_result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
