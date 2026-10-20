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
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

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


@dataclass(frozen=True)
class Markers:
    """Маркеры событий в логе демона.

    Раньше маркеры были константами модуля — то есть формат лога Gradle был
    зашит в код. Теперь формат описывается целью (targets.yaml), и разбор
    лога не знает, чей это лог.
    """

    build_start: bytes = b""
    build_end: bytes = b""
    build_end_alt: bytes = b""
    cancel: bytes = b""

    @classmethod
    def from_spec(cls, spec: Any) -> "Markers":
        """Маркеры из спецификации цели (пустые строки → нет маркера)."""
        return cls(
            build_start=str(spec.build_start or "").encode(),
            build_end=str(spec.build_end or "").encode(),
            build_end_alt=str(spec.build_end_alt or "").encode(),
            cancel=str(spec.cancel or "").encode(),
        )

    @property
    def usable(self) -> bool:
        """Есть ли хоть один маркер (иначе активность по логу не определить)."""
        return bool(self.build_start or self.build_end or self.cancel)


# Маркеры Gradle — то, что было зашито в код до появления targets.yaml.
GRADLE_MARKERS = Markers(
    build_start=MARK_BUILD_BYTES,
    build_end=MARK_IDLE_BYTES,
    build_end_alt=MARK_BUILD_END_ALT_BYTES,
    cancel=MARK_BUILD_CANCELED,
)

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


# Таймстемп без смещения: «2026-10-20 08:55:16.690» — так пишет Kotlin.
# Время местное, поэтому и разбираем как местное.
_LOCAL_TS_RE = re.compile(
    rb"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})[.,](\d{1,6})")


def parse_local_timestamp(line: bytes) -> float | None:
    """Таймстемп строки лога без смещения → epoch (секунды).

    Формат «2026-10-20 08:55:16.690» (Kotlin и подобные). Разбираем вручную:
    на сотнях тысяч строк это заметно быстрее datetime.fromisoformat.
    """
    match = _LOCAL_TS_RE.match(line)
    if not match:
        return None
    year, month, day, hour, minute, second, frac = (
        group.decode("ascii", "replace") for group in match.groups())
    try:
        stamp = datetime(int(year), int(month), int(day), int(hour),
                         int(minute), int(second))
    except ValueError:
        return None
    return stamp.timestamp() + int(frac.ljust(6, "0")) / 1_000_000.0


def parse_line_ts(line: bytes, ts_format: str = "iso") -> float | None:
    """Таймстемп строки лога в заданном формате.

    «iso» — со смещением (Gradle), «local» — без смещения (Kotlin).
    """
    if ts_format == "local":
        return parse_local_timestamp(line)
    return parse_timestamp(line)


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


@dataclass
class LineScan:
    """Что найдено в последовательности строк лога."""

    builds: list[Build] = field(default_factory=list)
    # Момент отмены сборки (клиент отвалился). ВНИМАНИЕ: строка отмены в
    # логе Gradle идёт БЕЗ таймстемпа (это часть стектрейса), поэтому
    # временем отмены считается начало той сборки, во время которой
    # клиент отвалился.
    canceled_at: float | None = None
    unclosed_builds: int = 0
    idle_without_start: int = 0
    bad_timestamp_lines: int = 0
    # Последняя отметка «демон освободился» — даже если начало сборки было
    # в предыдущем куске лога (при инкрементальном чтении это обычное дело).
    last_end: float | None = None


def scan_lines(lines: Iterable[bytes], prev_start: float | None = None,
               prev_canceled: float | None = None,
               markers: Markers = GRADLE_MARKERS,
               ts_format: str = "iso") -> LineScan:
    """Разбирает последовательность строк лога демона.

    Единая реализация для всех потребителей: и для полного файла
    (`scan_file`), и для инкрементального чтения (только новые байты).
    Раньше это были две реализации одного формата — и они разъехались:
    инкрементальный путь ловил отмену по таймстемпу строки, которого у
    строки отмены нет вовсе, то есть никогда.

    prev_start / prev_canceled — состояние из прошлого чтения: начало
    последней сборки и момент отмены, если они были раньше в этом же логе.

    markers — маркеры событий цели (targets.yaml); по умолчанию — Gradle.
    """
    scan = LineScan(canceled_at=prev_canceled)
    pending_starts: deque[float] = deque()
    last_start = prev_start
    mark_start = markers.build_start
    mark_end = markers.build_end
    mark_end_alt = markers.build_end_alt
    mark_cancel = markers.cancel

    for line in lines:
        if mark_cancel and mark_cancel in line:
            # Клиент отвалился. Своего таймстемпа у строки нет — берём
            # начало последней сборки: отмена случилась во время неё.
            ts = parse_line_ts(line, ts_format)
            scan.canceled_at = ts if ts is not None else (last_start or ts)
            continue
        if mark_start and mark_start in line:
            ts = parse_line_ts(line, ts_format)
            if ts is None:
                scan.bad_timestamp_lines += 1
                continue
            pending_starts.append(ts)
            last_start = ts
        elif (mark_end and mark_end in line) or (mark_end_alt
                                                  and mark_end_alt in line):
            ts = parse_line_ts(line, ts_format)
            if ts is None:
                scan.bad_timestamp_lines += 1
                continue
            scan.last_end = ts
            if pending_starts:
                # FIFO-сопоставление: конец сборки закрывает самую старую
                # незакрытую сборку.
                scan.builds.append(Build(pending_starts.popleft(), ts))
            else:
                # Демон ушёл в idle без единой полученной сборки
                # (например, в начале жизни демона) — не ошибка.
                scan.idle_without_start += 1

    # Старты без парного end: сборка не завершилась (лог оборван, демон умер).
    # Конец такой сборки условно равен mtime файла, но в простои она НЕ входит,
    # иначе получился бы фиктивный гигантский простой.
    while pending_starts:
        scan.builds.append(Build(pending_starts.popleft(), None))
        scan.unclosed_builds += 1

    return scan


def scan_file(path: str, markers: Markers = GRADLE_MARKERS,
              ts_format: str = "iso") -> FileScan:
    """Потоково (построчно) читает лог и собирает последовательность сборок.

    Файл целиком в память не загружается — логи бывают десятки мегабайт.

    Сопоставление сборок — FIFO: конец сборки закрывает самую старую
    незакрытую сборку. Порядок важен: сборки могут идти параллельно.
    """
    stat = os.stat(path)
    with open(path, "rb") as handle:
        scan = scan_lines(handle, markers=markers, ts_format=ts_format)
    return FileScan(
        path=path,
        mtime=stat.st_mtime,
        builds=scan.builds,
        unclosed_builds=scan.unclosed_builds,
        idle_without_start=scan.idle_without_start,
        bad_timestamp_lines=scan.bad_timestamp_lines,
        canceled_at=scan.canceled_at,
    )


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
