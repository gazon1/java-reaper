"""Разбор формата лога Gradle-демона — общий для рейпера и анализатора.

Раньше формат лога знали два независимых места: `reaper/logs.py` (рейпер) и
`tools/analyze-idle.py` (ретроспективный анализ). Две реализации одного
формата разъезжаются при первом же изменении Gradle — поэтому разбор
вынесен сюда, а оба потребителя используют его.

Что здесь есть:

  * маркеры событий (начало/конец сборки);
  * разбор таймстемпа Gradle (`2026-10-19T19:36:17.864+0300` → epoch);
  * потоковое чтение лога с попарным сопоставлением сборок.

Особенность формата, из-за которой всё это нужно: логи Gradle засоряются
DEBUG-спамом, простаивающий демон пишет в лог постоянно (~124 Б/с). Поэтому
mtime лога — не признак активности, а маркеры сборки уезжают далеко назад
и обычное чтение хвоста их теряет.

Разбор таймстемпа сделан вручную, без `datetime.fromisoformat`: на десятках
тысяч маркеров это заметно быстрее и не зависит от настроек локали.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

# Маркеры в логе Gradle-демона.
#   «Received command: Build»    — INFO, начало сборки.
#   «Marking the daemon as idle» — INFO, демон освободился (1:1 со сборкой).
# Второй маркер надёжнее «after build finished»: тот — DEBUG-уровень и
# исчезает, когда DEBUG-логирование выключено.
#
# В байтах — для чтения файлов в бинарном режиме (быстрее и без проблем
# с кодировкой мусорных строк), в строках — для читабельных API и тестов.
MARK_BUILD_BYTES = b"Received command: Build"
MARK_IDLE_BYTES = b"Marking the daemon as idle"

MARK_BUILD_START = MARK_BUILD_BYTES.decode()
MARK_BUILD_END = MARK_IDLE_BYTES.decode()

# Вариант окончания сборки (DEBUG-уровень, в новых логах встречается реже).
MARK_BUILD_END_ALT = "after build finished"
MARK_BUILD_END_ALT_BYTES = MARK_BUILD_END_ALT.encode()

# Отмена сборки из-за отвалившегося клиента. Демон остаётся в состоянии
# «идёт сборка», но работать уже некому — такой демон Gradle не убирает
# (его idle-таймаут не действует, пока демон «в сборке»).
MARK_BUILD_CANCELED = b"client disconnection detected"
MARK_BUILD_CANCELED_STR = MARK_BUILD_CANCELED.decode()

# Число дней между 1970-01-01 и 0001-01-01 (proleptic Gregorian ordinal).
_EPOCH_ORDINAL = 719163

# Минимальная длина таймстемпа: 28 символов («2026-10-19T19:36:17.864+0300»).
_MIN_TS_LEN = 28


def parse_timestamp(line: bytes) -> float | None:
    """Разбирает таймстемп в начале строки лога Gradle → epoch (секунды).

    Смещение часов учитывается: «2026-10-19T19:36:17.864+0300» — это
    16:36 UTC. Иначе сравнение с локальным временем сломается при смене
    тайзоны (и логи разных демонов могут быть с разными смещениями).

    Возвращает None, если строка не начинается с корректного таймстемпа.
    """
    if len(line) < _MIN_TS_LEN:
        return None
    try:
        year = int(line[0:4])
        month = int(line[5:7])
        day = int(line[8:10])
        hour = int(line[11:13])
        minute = int(line[14:16])
        second = int(line[17:19])
        millis = int(line[20:23])
    except ValueError:
        return None

    sign_char = line[23:24]
    if sign_char == b"+":
        offset_sign = 1
    elif sign_char == b"-":
        offset_sign = -1
    else:
        return None
    try:
        offset_hours = int(line[24:26])
        offset_minutes = int(line[26:28])
    except ValueError:
        return None

    days = datetime(year, month, day).toordinal() - _EPOCH_ORDINAL
    return (days * 86400.0
            + hour * 3600.0
            + minute * 60.0
            + second
            + millis / 1000.0
            - offset_sign * (offset_hours * 3600 + offset_minutes * 60))


def parse_ts(text: str) -> float | None:
    """То же, но для строки (text) — с поддержкой вариантов записи.

    Принимает «2026-10-19T19:36:17.864+0300», «...+03:00», «...Z» и
    запятую как разделитель дробной части.
    """
    stripped = text.strip()
    if not stripped:
        return None
    # Быстрый путь: строка начинается с ASCII-таймстемпа Gradle.
    if stripped[0:1].isdigit():
        raw = stripped.encode("utf-8", "replace")
        if len(raw) >= _MIN_TS_LEN and raw[23:24] in (b"+", b"-"):
            ts = parse_timestamp(raw)
            if ts is not None:
                return ts
    iso = stripped[:_MIN_TS_LEN].replace(",", ".")
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


@dataclass
class Build:
    """Сборка в логе демона: начало и (возможно) конец."""

    start: float
    end: float | None = None


@dataclass
class FileScan:
    """Результат разбора одного лога демона."""

    path: str
    mtime: float
    builds: list[Build] = field(default_factory=list)
    unclosed_builds: int = 0
    idle_without_start: int = 0
    bad_timestamp_lines: int = 0
    # Момент отмены сборки (клиент отвалился) — последний по логу.
    canceled_at: float | None = None

    @property
    def last_start(self) -> float | None:
        return self.builds[-1].start if self.builds else None

    @property
    def last_end(self) -> float | None:
        """Последний закрытый конец сборки."""
        for build in reversed(self.builds):
            if build.end is not None:
                return build.end
        return None


def scan_file(path: str) -> FileScan:
    """Потоково (построчно) читает лог и собирает последовательность сборок.

    Файл целиком в память не загружается — логи бывают десятки мегабайт.

    Сопоставление сборок — FIFO: конец сборки закрывает самую старую
    незакрытую сборку. Порядок важен: сборки могут идти параллельно.
    """
    stat = os.stat(path)
    scan = FileScan(path, stat.st_mtime)

    pending_starts: deque[float] = deque()

    with open(path, "rb") as handle:
        for line in handle:
            if MARK_BUILD_BYTES in line:
                ts = parse_timestamp(line)
                if ts is None:
                    scan.bad_timestamp_lines += 1
                    continue
                pending_starts.append(ts)
            elif MARK_IDLE_BYTES in line:
                ts = parse_timestamp(line)
                if ts is None:
                    scan.bad_timestamp_lines += 1
                    continue
                if pending_starts:
                    # FIFO-сопоставление: конец сборки закрывает самую старую
                    # незакрытую сборку.
                    scan.builds.append(Build(pending_starts.popleft(), ts))
                else:
                    # Демон ушёл в idle без единой полученной сборки
                    # (например, в начале жизни демона) — не ошибка.
                    scan.idle_without_start += 1
            elif MARK_BUILD_CANCELED in line:
                ts = parse_timestamp(line)
                if ts is not None:
                    scan.canceled_at = ts

    # Старты без парного end: сборка не завершилась (лог оборван, демон умер).
    # Конец такой сборки условно равен mtime файла, но в простои она НЕ входит,
    # иначе получился бы фиктивный гигантский простой.
    while pending_starts:
        scan.builds.append(Build(pending_starts.popleft(), None))
        scan.unclosed_builds += 1

    return scan


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
