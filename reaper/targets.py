"""Цели рейпера: описания программ-демонов, которых мы убираем.

**Цель** (target) — это описание одного вида демонов: как его узнать среди
прочих процессов, где его лог, как понять по логу, что он работал, сколько
ждать простоя и кого он защищает.

Раньше описание целей было размазано по коду: main-класс Gradle — в
`proc.daemon_kind`, имя лога — в `proc.find_log_path`, маркеры сборок — в
`logparse`, пороги — в `config.DEFAULTS`, «Gradle защищает Kotlin» — в
таблице правил. Добавить третью программу было невозможно, не правя
четыре файла.

Теперь всё это — в одном месте: `targets.yaml`. Код спрашивает у набора целей
«кто это за процесс» и «как его читать», а не знает про Gradle.

Правила политики при этом остались прежними: они оперируют словом «вид
демона» (`kind`), а не именами программ. Добавить программу — значит
добавить цель, а не переписать политику.

Что НЕ переносится в конфиг (и остаётся в коде):

  * смысл самих правил («неизвестное ⇒ оставить», «память нужна» и т. п.);
  * умение читать логи и понимать таймстемпы (`logparse`);
  * всё, что касается сигналов и безопасности (`execute`).

ФОРМАТ ФАЙЛА — YAML (`~/.config/java-reaper/targets.yaml`), необязательный:
если файла нет, работают встроенные цели Gradle и Kotlin — ровно как раньше.

Этот модуль — ТОЧКА ВХОДА: сам он держит только переэкспорт, а всё остальное
разнесено по смыслу:
  * `reaper.target_spec` — описание одной цели (признаки вида демона);
  * `reaper.target_registry` — реестр целей: встроенные цели и набор целей,
    с которым работают правила, сбор демонов и отчёты;
  * `reaper.target_yaml` — файл целей: разбор YAML и чтение файла.

Имена из этих модулей переэкспортируются здесь без изменений:
`from reaper.targets import TargetSpec` продолжает работать — это публичный
контракт модуля, на него завязаны остальные части пакета и инструменты в
`tools/`.
"""

from __future__ import annotations

# Имена, которые были видны в модуле и к целям прямого отношения не имеют:
# импорты и помощники разбора. Оставлены, чтобы после разбиения на модули
# публичный контракт `reaper.targets` не сузился ни для кого.
import os  # noqa: F401
import re  # noqa: F401
from dataclasses import dataclass, field, replace  # noqa: F401
from typing import Any, Iterable  # noqa: F401

from .model import (  # noqa: F401  (публичный API, переэкспорт)
    ACTIVITY_MARKERS,
    ACTIVITY_MTIME,
    GRADLE_MAIN,
    KOTLIN_MAIN,
)
from .target_registry import (  # noqa: F401  (публичный API, переэкспорт)
    BUILTIN_TARGETS,
    TargetSet,
    builtin_targets,
)
from .target_spec import (  # noqa: F401  (публичный API, переэкспорт)
    TS_ISO,
    TS_LOCAL,
    TargetSpec,
)
from .target_yaml import (  # noqa: F401  (публичный API, переэкспорт)
    DEFAULT_TARGETS_PATH,
    _optional_float,
    _spec_from_dict,
    _str_tuple,
    load_targets,
    parse_targets,
)

__all__ = [
    # описание цели
    "TargetSpec",
    "TS_ISO",
    "TS_LOCAL",
    # реестр целей
    "BUILTIN_TARGETS",
    "TargetSet",
    "builtin_targets",
    # файл целей
    "DEFAULT_TARGETS_PATH",
    "load_targets",
    "parse_targets",
    # признаки активности и main-классы (живут в `model`, доступны и здесь)
    "ACTIVITY_MARKERS",
    "ACTIVITY_MTIME",
    "GRADLE_MAIN",
    "KOTLIN_MAIN",
]
