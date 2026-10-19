"""Разбор логов демонов.

Логи Gradle засоряются DEBUG-спамом («Waiting to acquire shared lock on
daemon addresses registry»): простаивающий демон пишет в лог постоянно
(~124 Б/с). Поэтому mtime лога не признак активности, а маркеры сборки
уезжают далеко назад и обычное чтение хвоста их теряет.

Схема чтения: первый свип — файл целиком (до LOG_SCAN_MAX_BYTES), дальше —
только новые байты, смещение хранится в состоянии.
"""

from __future__ import annotations

import os
from typing import Any

from .proc import parse_ts

# Маркеры в логе Gradle-демона.
#   «Received command: Build»    — INFO, начало сборки.
#   «Marking the daemon as idle» — INFO, демон освободился (1:1 со сборкой).
# Второй маркер надёжнее «after build finished»: тот — DEBUG-уровень и
# исчезает, когда DEBUG-логирование выключено.
MARK_BUILD_START = "Received command: Build"
MARK_BUILD_END = "Marking the daemon as idle"
MARK_BUILD_END_ALT = "after build finished"


def scan_markers(text: str) -> tuple[float | None, float | None]:
    """Последние таймстемпы старта и конца сборки в блоке текста."""
    last_start = last_end = None
    for line in text.splitlines():
        if MARK_BUILD_START in line:
            ts = parse_ts(line)
            if ts is not None:
                last_start = ts
        elif MARK_BUILD_END in line or MARK_BUILD_END_ALT in line:
            ts = parse_ts(line)
            if ts is not None:
                last_end = ts
    return last_start, last_end


def scan_gradle_log(path: str, offset: int, max_bytes: int) -> dict[str, Any]:
    """Разбор лога Gradle-демона.

    offset — сколько байт уже разобрано (из состояния). Возвращает:
      offset   — новое смещение;
      start/end — найденные маркеры (может быть None);
      complete — True, если история лога известна целиком.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"offset": offset, "start": None, "end": None, "complete": False}
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
        return {"offset": offset, "start": None, "end": None, "complete": False}
    s, e = scan_markers(blob.decode("utf-8", "replace"))
    if offset <= 0 and not complete and e is None:
        # Файл больше лимита и маркеры в его начале не найдены: историю
        # пока не знаем — читаем с этого места дальше в следующих свипах.
        new_offset = start
    else:
        new_offset = size
    return {"offset": new_offset, "start": s, "end": e,
            "complete": complete or e is not None}


def parse_gradle_log(path: str, tail_bytes: int) -> tuple[float | None, float | None]:
    """Совместимая обёртка: разбор лога целиком (для тестов и разовых нужд)."""
    res = scan_gradle_log(path, 0, tail_bytes)
    return res["start"], res["end"]
