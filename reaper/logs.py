"""Разбор логов демонов — рейпер.

Сам формат лога (маркеры, таймстемпы, сопоставление сборок) живёт в
`reaper/logparse.py` — общем модуле с `tools/analyze-idle.py`. Здесь только
инкрементальное чтение: смещение хранится в состоянии, поэтому за свип
читаются только новые байты.

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


def scan_gradle_log(path: str, offset: int, max_bytes: int) -> dict[str, Any]:
    """Разбор лога Gradle-демона.

    offset — сколько байт уже разобрано (из состояния). Возвращает:
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
    s, e = scan_markers(blob.decode("utf-8", "replace"))
    canceled = _scan_canceled(blob)
    if offset <= 0 and not complete and e is None:
        # Файл больше лимита и маркеры в его начале не найдены: историю
        # пока не знаем — читаем с этого места дальше в следующих свипах.
        new_offset = start
    else:
        new_offset = size
    return {"offset": new_offset, "start": s, "end": e, "canceled": canceled,
            "complete": complete or e is not None}


def _scan_canceled(blob: bytes) -> float | None:
    """Момент отмены сборки в блоке байтов (клиент отвалился)."""
    if MARK_BUILD_CANCELED not in blob:
        return None
    canceled = None
    for line in blob.splitlines():
        if MARK_BUILD_CANCELED in line:
            ts = parse_ts(line.decode("utf-8", "replace"))
            if ts is not None:
                canceled = ts
    return canceled


def parse_gradle_log(path: str, tail_bytes: int) -> tuple[float | None, float | None]:
    """Совместимая обёртка: разбор лога целиком (для тестов и разовых нужд)."""
    res = scan_gradle_log(path, 0, tail_bytes)
    return res["start"], res["end"]
