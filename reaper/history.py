"""История демонов: компактная сводка вместо лога.

Логи мёртвых демонов — это мусор (1.2 ГБ в `~/.gradle/daemon`, 108 МБ
Kotlin-логов в `/tmp`). Но выбрасывать их просто так жалко: по ним можно
ретроспективно сказать, что решили бы нынешние правила. Поэтому перед
удалением лога из него извлекается **сводка** — компактная запись о жизни
демона:

  * когда демон жил (первая активность → последняя запись в лог);
  * когда шли сборки (для Gradle) или когда была активность (для Kotlin);
  * была ли отмена сборки (клиент отвалился — демон «завис»);
  * размер и время изменения лога.

Сводка занимает килобайты вместо мегабайт и остаётся навсегда: по ней
работает симулятор (`tools/simulate.py`), который отвечает на вопрос
«что бы наши правила убили за всю историю» за минуты, а не за дни пилота.

Формат — JSONL: одна строка JSON на демон.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from .logparse import scan_file

# Время жизни демона в логе Kotlin: «2026-10-20 08:55:16.690 [daemon] …»
_KOTLIN_TS_RE = re.compile(
    rb"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})[.,](\d{1,6})")

# Ширина «корзины» активности для Kotlin: 5 минут. Заметно мельче порога
# простоя (2 ч), поэтому простой и последующее оживение видны точно.
KOTLIN_BUCKET_SECONDS = 300


def parse_kotlin_ts(line: bytes) -> float | None:
    """Таймстемп строки лога Kotlin → epoch.

    Формат лога Kotlin другой, чем у Gradle («2026-10-20 08:55:16.690»),
    поэтому и разбор свой. Разбираем вручную — на сотнях тысяч строк
    это заметно быстрее datetime.fromisoformat.
    """
    m = _KOTLIN_TS_RE.match(line)
    if not m:
        return None
    year, month, day, hour, minute, second, frac = (
        g.decode("ascii", "replace") for g in m.groups())
    try:
        import datetime

        # В логе Kotlin смещения нет — время местное, поэтому и разбираем
        # как местное (naive datetime → .timestamp() трактует его как local).
        dt = datetime.datetime(int(year), int(month), int(day),
                               int(hour), int(minute), int(second))
    except ValueError:
        return None
    return dt.timestamp() + int(frac.ljust(6, "0")) / 1_000_000.0


def kotlin_activity(path: str, bucket_seconds: int = KOTLIN_BUCKET_SECONDS
                   ) -> tuple[float | None, float | None, list[list[float]]]:
    """Активность демона Kotlin по логу.

    Возвращает (первая активность, последняя активность, корзины активности).
    Корзина — [время начала интервала, сколько секунд активности в нём].
    Корзины идут подряд, без пустых, — это и есть интервалы жизни.
    """
    first: float | None = None
    last: float | None = None
    buckets: dict[int, float] = {}
    try:
        with open(path, "rb") as fh:
            for line in fh:
                ts = parse_kotlin_ts(line)
                if ts is None:
                    continue
                if first is None:
                    first = ts
                last = ts
                key = int(ts // bucket_seconds) * bucket_seconds
                buckets[key] = buckets.get(key, 0.0) + 1.0
    except OSError:
        return None, None, []
    spans = [[float(k), float(v)] for k, v in sorted(buckets.items())]
    return first, last, spans


def summarize_gradle(path: str, pid: int | None = None,
                     version: str | None = None) -> dict[str, Any]:
    """Сводка по логу Gradle-демона."""
    scan = scan_file(path)
    builds = [[b.start, b.end] for b in scan.builds]
    starts = [b.start for b in scan.builds]
    ends = [b.end for b in scan.builds if b.end is not None]
    first = min(starts) if starts else None
    last = max(ends) if ends else (max(starts) if starts else None)
    record: dict[str, Any] = {
        "kind": "gradle",
        "pid": pid,
        "version": version,
        "path": path,
        "size_bytes": os.path.getsize(path),
        "mtime": scan.mtime,
        "first_activity": first,
        "last_activity": last,
        "builds": builds,
        "unclosed_builds": scan.unclosed_builds,
        "canceled_at": scan.canceled_at,
        "summary_at": time.time(),
    }
    return record


def summarize_kotlin(path: str, instance: str | None = None) -> dict[str, Any]:
    """Сводка по логу Kotlin-демона."""
    first, last, spans = kotlin_activity(path)
    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        size, mtime = 0, 0.0
    return {
        "kind": "kotlin",
        "pid": None,
        "instance": instance,
        "path": path,
        "size_bytes": size,
        "mtime": mtime,
        "first_activity": first,
        "last_activity": last,
        "activity": spans,
        "summary_at": time.time(),
    }


def effective_canceled(record: dict[str, Any]) -> float | None:
    """Момент отмены сборки — если демон после неё действительно «завис».

    Та же логика, что в рейпере (reaper.collect.canceled_state): если после
    отмены началась новая сборка, клиент вернулся, и демон не завис —
    отмена обесценивается.
    """
    canceled = record.get("canceled_at")
    if canceled is None:
        return None
    for build in record.get("builds") or []:
        if float(build[0]) > float(canceled):
            return None
    return float(canceled)


def intervals(record: dict[str, Any]) -> list[tuple[float, float]]:
    """Интервалы активности демона из сводки — единая модель для обоих классов.

    Gradle: интервалы сборок. Kotlin: интервалы активности (корзины).
    """
    out: list[tuple[float, float]] = []
    if record.get("kind") == "gradle":
        for build in record.get("builds") or []:
            start = float(build[0])
            end = build[1]
            if end is None:
                # Сборка без конца: клиент отвалился. Демон «завис» — ждёт
                # (неизвестно, сколько) и никогда не станет idle.
                out.append((start, float("inf")))
            else:
                out.append((start, float(end)))
        return out
    for span in record.get("activity") or []:
        bucket = float(span[0])
        out.append((bucket, bucket + KOTLIN_BUCKET_SECONDS))
    return out


def append_history(path: str, record: dict[str, Any]) -> None:
    """Дописывает сводку в JSONL-историю."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_history(path: str) -> list[dict[str, Any]]:
    """Читает историю сводок."""
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def history_keys(path: str) -> set[tuple[str, float]]:
    """Ключи уже сохранённых сводок: (путь, mtime файла).

    Нужны, чтобы повторный прогон архивирования не засорил историю
    дубликатами: симулятор считает каждую запись, и копии одного демона
    исказили бы и число убийств, и долю ложных срабатываний.
    """
    keys: set[tuple[str, float]] = set()
    for record in read_history(path):
        path_key = record.get("path")
        if path_key is None:
            continue
        keys.add((str(path_key), float(record.get("mtime") or 0.0)))
    return keys


def default_history_path(state_dir: str | None = None) -> str:
    """Путь к истории по умолчанию: рядом с состоянием рейпера."""
    base = state_dir or os.path.expanduser("~/.local/state/java-reaper")
    return os.path.join(base, "logs-history.jsonl")
