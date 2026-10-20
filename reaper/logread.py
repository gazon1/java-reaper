"""Чтение логов демонов — рейпер.

Имя модуля отражает его роль: это ЧТЕНИЕ (инкрементальное), а не разбор
формата. Сам формат лога (маркеры, таймстемпы, сопоставление сборок) живёт
в `reaper/logparse.py` — общем модуле с `tools/analyze-idle.py` и с
историей демонов (`reaper/history.py`).

Здесь только выборка байтов: смещение хранится в состоянии, поэтому за
свип читаются только новые байты.

Схема чтения: первый свип — файл целиком (до LOG_SCAN_MAX_BYTES), дальше —
только новые байты.
"""

from __future__ import annotations

import os
from typing import Any

from .logparse import (
    MARK_BUILD_CANCELED,
    MARK_BUILD_END,
    MARK_BUILD_END_ALT,
    MARK_BUILD_START,
    parse_ts,
    scan_lines,
    scan_markers,
)

__all__ = [
    "MARK_BUILD_CANCELED",
    "MARK_BUILD_END",
    "MARK_BUILD_END_ALT",
    "MARK_BUILD_START",
    "parse_ts",
    "scan_markers",
    "scan_gradle_log",
    "parse_gradle_log",
]


def scan_gradle_log(path: str, offset: int, max_bytes: int,
                    prev_start: float | None = None,
                    prev_canceled: float | None = None) -> dict[str, Any]:
    """Разбор лога Gradle-демона.

    offset — сколько байт уже разобрано (из состояния).
    prev_start / prev_canceled — начало последней сборки и момент отмены
    из прошлого чтения: маркер отмены может попасть в новый кусок, а
    таймстемпа у него нет (см. reaper.logparse.scan_lines).

    Возвращает:
      offset   — новое смещение;
      start/end — найденные маркеры (может быть None);
      canceled — момент отмены сборки (клиент отвалился), если была;
      complete — True, если история лога известна целиком.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"offset": offset, "start": None, "end": None,
                "canceled": None, "complete": False}
    if offset > size:
        offset = 0  # лог усечён или перезаписан
    if offset <= 0:
        if size <= max_bytes:
            start, complete = 0, True
        else:
            start, complete = size - max_bytes, False
    else:
        start, complete = offset, True
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return {"offset": offset, "start": None, "end": None,
                "canceled": None, "complete": False}
    # Разбор — тем же сканером, что и полный файл (reaper.logparse):
    # формат лога знает одно место, а не два.
    scan = scan_lines(blob.splitlines(), prev_start=prev_start,
                      prev_canceled=prev_canceled)
    starts = [b.start for b in scan.builds]
    ends = [b.end for b in scan.builds if b.end is not None]
    if scan.last_end is not None:
        ends.append(scan.last_end)
    s = max(starts) if starts else None
    e = max(ends) if ends else None
    canceled = scan.canceled_at
    if offset <= 0 and not complete and e is None:
        # Файл больше лимита и маркеры в его начале не найдены: историю
        # пока не знаем — читаем с этого места дальше в следующих свипах.
        new_offset = start
    else:
        new_offset = size
    return {"offset": new_offset, "start": s, "end": e, "canceled": canceled,
            "complete": complete or e is not None}


def parse_gradle_log(path: str, tail_bytes: int) -> tuple[float | None, float | None]:
    """Совместимая обёртка: разбор лога целиком (для тестов и разовых нужд)."""
    res = scan_gradle_log(path, 0, tail_bytes)
    return res["start"], res["end"]
