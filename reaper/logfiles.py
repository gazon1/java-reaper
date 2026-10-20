"""Файлы логов целей: где они лежат и как из имени понять, чей это лог.

Общий модуль для уборки логов (`tools/prune-logs.py`), ретроспективы
простоя (`tools/analyze-idle.py`) и истории демонов (`reaper/history.py`).

Почему общий: «какому демону принадлежит этот файл» — это одно и то же
правило, а не три копии. Копии расходились мелочами (группа файлов у одной
цели — путь, у другой — метка инстанса), и уборщик считал один и тот же файл
живым, а история — чужим.

Всё берётся из описания цели (`targets.yaml`): регулярка PID, регулярка
группировки, шаблоны путей и «неприкосновенные» имена. Код не знает, что
такое Gradle и что такое Kotlin: добавление третьей программы — это
описание цели, а не правка кода.
"""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .proc import proc_cmdline

__all__ = [
    "daemon_alive",
    "discover",
    "expand_pattern",
    "group_of",
    "instance_from_log_name",
    "instance_of",
    "is_forbidden",
    "is_target_process",
    "pid_for",
    "pid_from_log_name",
    "pid_of",
]


def expand_pattern(pattern: str) -> list[str]:
    """Раскрывает шаблон пути («~», wildcards) в список существующих файлов.

    Каталоги пропускаются: лог — это файл, и «шаблон развернулся в
    каталог» означало бы, что мы собираемся удалять каталог.
    """
    matches = glob.glob(os.path.expanduser(pattern), recursive=True)
    return sorted(path for path in matches if os.path.isfile(path))


def discover(spec: Any, overrides: Any = None) -> list[str]:
    """Файлы логов цели — по её шаблонам (`log_globs`) или по перекрытию.

    `overrides` — то, чем оператор перекрыл описание цели. Формат зависит от
    вызывающего: словарь «имя цели → шаблоны» (уборка, где перекрытия
    приходят сразу для всех целей) или список шаблонов одной цели
    (ретроспектива, где перекрытие задано для цели). Пустое перекрытие —
    значит «шаблоны цели», а не «ничего не искать».
    """
    out: set[str] = set()
    for pattern in _patterns(spec, overrides):
        out.update(expand_pattern(pattern))
    return sorted(out)


def _patterns(spec: Any, overrides: Any) -> list[str]:
    """Шаблоны поиска логов: перекрытие оператора, иначе — описание цели."""
    if overrides is None:
        return list(getattr(spec, "log_globs", ()) or ())
    if isinstance(overrides, Mapping):
        return list(overrides.get(spec.name) or spec.log_globs)
    if isinstance(overrides, Sequence) and not isinstance(overrides, str):
        return list(overrides) or list(spec.log_globs)
    return list(spec.log_globs)


def pid_of(spec: Any, path: str) -> int | None:
    """PID демона из имени файла — по описанию цели (`log_pid_regex`).

    None, если у цели такой регулярки нет или имя не совпало: у цели PID
    в имени может быть ненастоящим (у Kotlin его нет вовсе), и тогда это
    «неизвестно», а не «демон без PID».
    """
    _matched, pid = _match_pid(spec, path)
    return pid


def pid_for(spec: Any, path: str) -> int | None:
    """PID демона: сначала по описанию цели, иначе — запасной разбор имени.

    Запасной разбор нужен там, где описание цели может не совпасть с
    именем файла (например, лог найден по старому соглашению). Важно:
    если регулярка совпала, но PID в ней не число — это «PID неизвестен»,
    а не повод догадываться по имени.
    """
    matched, pid = _match_pid(spec, path)
    if matched:
        return pid
    return pid_from_log_name(path)


def _match_pid(spec: Any, path: str) -> tuple[bool, int | None]:
    """(совпала ли регулярка, PID) — различаем «не совпала» и «не число»."""
    pattern = str(getattr(spec, "log_pid_regex", "") or "")
    if not pattern:
        return False, None
    try:
        match = re.compile(pattern).match(os.path.basename(path))
    except re.error:
        return False, None
    if not match:
        return False, None
    try:
        return True, int(match.groupdict().get("pid"))
    except (TypeError, ValueError):
        return True, None


def group_of(spec: Any, path: str) -> str | None:
    """Группа файлов одного демона — по описанию цели (`log_group_regex`).

    У Kotlin один демон пишет в несколько файлов (ротация .00/.01/.02), и
    живой демон держит открытым только последний: судить по каждому файлу
    отдельно нельзя, открытый файл защищает всю группу.

    None, если регулярки нет или имя не совпало: тогда группу определяет
    вызывающий (обычно «файл сам себе группа»).
    """
    pattern = str(getattr(spec, "log_group_regex", "") or "")
    if not pattern:
        return None
    try:
        match = re.compile(pattern).match(os.path.basename(path))
    except re.error:
        return None
    if not match:
        return None
    return match.groupdict().get("group") or None


def instance_of(spec: Any, path: str) -> str | None:
    """Инстанс демона: группа из описания цели, иначе — по имени файла.

    Запасной разбор имени нужен для целей, у которых в описании нет
    регулярки группировки: у них инстанс видно только в имени файла.
    """
    group = group_of(spec, path)
    if group:
        return group
    return instance_from_log_name(path)


def pid_from_log_name(path: str) -> int | None:
    """PID из имени лога Gradle (`daemon-<pid>.out.log`)."""
    name = os.path.basename(path)
    try:
        return int(name.split("-")[1].split(".")[0])
    except (IndexError, ValueError):
        return None


def instance_from_log_name(path: str) -> str | None:
    """Инстанс из имени лога Kotlin (`kotlin-daemon.…-<инстанс>.<N>.log`)."""
    name = os.path.basename(path)
    try:
        return name.split("-")[-1].split(".")[0]
    except IndexError:
        return None


def is_forbidden(spec: Any, name: str) -> bool:
    """Файл из «неприкосновенного» списка цели.

    Некоторые файлы в каталоге логов создаёт и удаляет сама программа
    (маркеры Kotlin): убийство демона их не оправдывает, а ломать может.
    """
    prefixes = getattr(spec, "forbidden_prefixes", ()) or ()
    return any(name.startswith(prefix) for prefix in prefixes)


def daemon_alive(spec: Any, path: str) -> bool | None:
    """Жив ли демон прямо сейчас (None — определить нельзя).

    None возвращается, когда у цели в имени файла нет PID: тогда угадывать
    нечего, и молча считать демона мёртвым нельзя.

    ВНИМАНИЕ: PID мог быть переиспользован другим процессом — тогда alive
    покажет «да» ошибочно. Для оценки объёма это приемлемо; для уборки
    логов — нет, там проверка строже (см. `is_target_process`).
    """
    pid = pid_of(spec, path)
    if pid is None:
        return None
    return os.path.isdir(f"/proc/{pid}")


def is_target_process(pid: int, spec: Any, targets: Any) -> bool:
    """Похож ли процесс с таким PID на демона этой цели.

    PID мог быть переиспользован другим процессом — тогда демон мёртв,
    даже если PID существует. Поэтому проверяем не «существует ли PID», а
    «похож ли процесс на демон этой цели».
    """
    cmdline = proc_cmdline(pid)
    if not cmdline:
        return False
    matched = targets.match(cmdline)
    return matched is not None and matched.name == spec.name
