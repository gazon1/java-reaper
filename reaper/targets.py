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
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from .model import GRADLE_MAIN, KOTLIN_MAIN

# Куда искать файл целей по умолчанию.
DEFAULT_TARGETS_PATH = "~/.config/java-reaper/targets.yaml"

# Как понимать активность демона по его логу.
ACTIVITY_MTIME = "mtime"      # признак активности — mtime лога (Kotlin)
ACTIVITY_MARKERS = "markers"  # активность — маркеры в тексте лога (Gradle)

# Формат таймстемпа в начале строки лога.
TS_ISO = "iso"        # «2026-10-19T19:36:17.864+0300» — с смещением
TS_LOCAL = "local"    # «2026-10-20 08:55:16.690» — местное время, без смещения


@dataclass(frozen=True)
class TargetSpec:
    """Описание одного вида демонов.

    name           — ключ вида (`kind` в демоне, в состоянии и в отчётах);
    label          — человекочитаемое имя для отчётов;
    match_cmdline  — подстрока в cmdline, по которой узнаём процесс;
    match_comm     — имя исполняемого файла (команды) процесса;
    log_name_regex — регулярка на имя файла лога (ищем среди открытых fd);
    activity       — «mtime» или «markers» (см. константы выше);
    ts_format      — формат таймстемпа для activity=markers;
    build_start    — маркер начала работы (для activity=markers);
    build_end      — маркер конца работы;
    build_end_alt  — запасной маркер конца работы;
    cancel         — маркер «клиент отвалился» (даёт признак «завис»);
    idle_seconds   — порог простоя (None → берётся из плоского конфига);
    recreate_cost_seconds — цена холодного старта (None → плоский конфиг);
    self_timeout_seconds  — через сколько демон умирает сам (Gradle: 180 мин);
    protects       — какие виды демонов этот вид защищает, пока работает;
    rebuildable    — «это пересоздаваемый кэш, его можно убить»;
    log_trim       — «лог этого демона можно усекать» (усечение сдвигает
                     mtime, а у целей, где mtime — признак простоя, это
                     врёт: демон выглядел бы «только что активным»).
    """

    name: str
    label: str = ""
    match_cmdline: str = ""
    match_comm: str = ""
    log_name_regex: str = ""
    activity: str = ACTIVITY_MTIME
    ts_format: str = TS_ISO
    build_start: str = ""
    build_end: str = ""
    build_end_alt: str = ""
    cancel: str = ""
    idle_seconds: float | None = None
    recreate_cost_seconds: float | None = None
    self_timeout_seconds: float | None = None
    protects: tuple[str, ...] = ()
    rebuildable: bool = False
    log_trim: bool = False

    def matches(self, cmdline: str, comm: str = "") -> bool:
        """Похоже ли этот процесс на нашу цель."""
        if self.match_cmdline and self.match_cmdline in cmdline:
            return True
        if self.match_comm and comm == self.match_comm:
            return True
        return False

    def protects_kind(self, kind: str) -> bool:
        """Защищает ли наша цель данный вид демонов."""
        return kind in self.protects


# Встроенные цели: Gradle и Kotlin. Используются, если файла целей нет или
# если он не описал соответствующий вид. Значения соответствуют прежнему
# поведению рейпера — см. config.DEFAULTS.
BUILTIN_TARGETS: tuple[TargetSpec, ...] = (
    TargetSpec(
        name="gradle",
        label="Gradle",
        match_cmdline=GRADLE_MAIN,
        log_name_regex=r"^daemon-\d+\.out\.log$",
        activity=ACTIVITY_MARKERS,
        ts_format=TS_ISO,
        build_start="Received command: Build",
        build_end="Marking the daemon as idle",
        build_end_alt="after build finished",
        cancel="client disconnection detected",
        # У Gradle есть собственный idle-таймаут: демон, простоявший дольше,
        # умирает сам. Наш порог обязан быть выше — иначе мы конкурируем с
        # Gradle за одни и те же демоны и проигрываем.
        self_timeout_seconds=10800,
        # Пока идёт сборка Gradle, компилятор нужен прямо сейчас.
        protects=("kotlin",),
        rebuildable=True,
        # Лог Gradle можно усекать: простой измеряется по маркерам сборок,
        # а не по mtime.
        log_trim=True,
    ),
    TargetSpec(
        name="kotlin",
        label="Kotlin",
        match_cmdline=KOTLIN_MAIN,
        log_name_regex=r"^kotlin-daemon\..*\.log$",
        activity=ACTIVITY_MTIME,
        rebuildable=True,
    ),
)


class TargetSet:
    """Набор целей: спецификации + разрешённые пороги.

    Пороги разрешаются один раз при загрузке (см. `load_targets`), а не
    на каждый вызов: правила читают их десятки раз за свип.
    """

    def __init__(self, specs: dict[str, TargetSpec]):
        self._specs = dict(specs)

    # --- доступ к целям -------------------------------------------------
    def get(self, name: str) -> TargetSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def specs(self) -> Iterable[TargetSpec]:
        return self._specs.values()

    def specs_by_name(self) -> dict[str, TargetSpec]:
        return dict(self._specs)

    def labels(self) -> dict[str, str]:
        """Подписи видов для отчётов (по умолчанию — имя цели)."""
        return {name: (spec.label or name) for name, spec in self._specs.items()}

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    # --- сопоставление процесса с целью --------------------------------
    def match(self, cmdline: str, comm: str = "") -> TargetSpec | None:
        """Какая цель соответствует процессу (None — не наш).

        Порядок целей важен: первая совпавшая выигрывает. Поэтому более
        специфичные цели стоит описывать раньше общих.
        """
        for spec in self._specs.values():
            if spec.matches(cmdline, comm):
                return spec
        return None

    # --- отношения между целями ----------------------------------------
    def protectors_of(self, kind: str) -> list[str]:
        """Какие цели защищают данный вид демонов, пока сами работают."""
        return [name for name, spec in self._specs.items()
                if spec.protects_kind(kind)]


def builtin_targets() -> TargetSet:
    """Встроенный набор целей — как было до появления файла целей."""
    return TargetSet({spec.name: spec for spec in BUILTIN_TARGETS})


def _spec_from_dict(raw: dict[str, Any], warnings: list[str],
                    name_hint: str) -> TargetSpec | None:
    """Собирает спецификацию цели из словаря YAML."""
    name = str(raw.get("name") or name_hint or "").strip()
    if not name:
        warnings.append("цели: у записи нет имени — пропущена")
        return None
    protects = raw.get("protects") or ()
    if isinstance(protects, str):
        protects = (protects,)
    spec = TargetSpec(
        name=name,
        label=str(raw.get("label") or ""),
        match_cmdline=str(raw.get("match_cmdline") or ""),
        match_comm=str(raw.get("match_comm") or ""),
        log_name_regex=str(raw.get("log_name_regex") or ""),
        activity=str(raw.get("activity") or ACTIVITY_MTIME),
        ts_format=str(raw.get("ts_format") or TS_ISO),
        build_start=str(raw.get("build_start") or ""),
        build_end=str(raw.get("build_end") or ""),
        build_end_alt=str(raw.get("build_end_alt") or ""),
        cancel=str(raw.get("cancel") or ""),
        idle_seconds=_optional_float(raw.get("idle_seconds")),
        recreate_cost_seconds=_optional_float(raw.get("recreate_cost_seconds")),
        self_timeout_seconds=_optional_float(raw.get("self_timeout_seconds")),
        protects=tuple(str(p) for p in protects),
        rebuildable=bool(raw.get("rebuildable", False)),
        log_trim=bool(raw.get("log_trim", False)),
    )
    if not spec.match_cmdline and not spec.match_comm:
        warnings.append(f"цели: у «{name}» нет ни match_cmdline, ни match_comm "
                        f"— она никогда не сматчится")
    if spec.activity not in (ACTIVITY_MTIME, ACTIVITY_MARKERS):
        warnings.append(f"цели: у «{name}» неизвестный activity="
                        f"{spec.activity!r} — считаем как {ACTIVITY_MTIME}")
        spec = replace(spec, activity=ACTIVITY_MTIME)
    if spec.activity == ACTIVITY_MARKERS and not spec.build_start:
        warnings.append(f"цели: у «{name}» activity=markers, но нет "
                        f"build_start — активность определить нечем")
    try:
        if spec.log_name_regex:
            re.compile(spec.log_name_regex)
    except re.error as exc:
        warnings.append(f"цели: у «{name}» неверная регулярка log_name_regex: "
                        f"{exc}")
        spec = replace(spec, log_name_regex="")
    return spec


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_targets(text: str, warnings: list[str] | None = None
                  ) -> list[TargetSpec]:
    """Разбирает YAML с целями.

    Требуется PyYAML. Если его нет — возвращаем пустой список и предупреждение:
    рейпер обязан работать и без файла целей (встроенные цели).
    """
    warnings = warnings if warnings is not None else []
    try:
        import yaml  # noqa: PLC0415 — импорт по требованию: без PyYAML
    except ImportError:
        warnings.append("цели: не установлен PyYAML — файл целей прочитан "
                        "быть не может, работают встроенные цели "
                        "(pip install PyYAML)")
        return []
    try:
        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 — yaml бросает свой тип ошибки
        warnings.append(f"цели: файл не разобран ({exc}) — работают "
                        f"встроенные цели")
        return []
    if data is None:
        return []
    if not isinstance(data, dict):
        warnings.append("цели: ожидался словарь с ключом «targets» — "
                        "работают встроенные цели")
        return []
    raw_targets = data.get("targets") or []
    if not isinstance(raw_targets, list):
        warnings.append("цели: «targets» должен быть списком — работают "
                        "встроенные цели")
        return []
    out: list[TargetSpec] = []
    for index, raw in enumerate(raw_targets):
        if not isinstance(raw, dict):
            warnings.append(f"цели: запись #{index + 1} не словарь — пропущена")
            continue
        spec = _spec_from_dict(raw, warnings, name_hint=f"#{index + 1}")
        if spec is not None:
            out.append(spec)
    return out


def load_targets(path: str | None, warnings: list[str] | None = None
                 ) -> TargetSet:
    """Читает файл целей и накладывает его на встроенные.

    Файла нет — возвращаются встроенные цели (поведение прежнее).
    Описанные в файле цели заменяют встроенные с тем же именем; остальные
    встроенные остаются.
    """
    warnings = warnings if warnings is not None else []
    specs = {spec.name: spec for spec in BUILTIN_TARGETS}
    target_path = os.path.abspath(os.path.expanduser(
        path or DEFAULT_TARGETS_PATH))
    if not os.path.isfile(target_path):
        return TargetSet(specs)
    try:
        with open(target_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        warnings.append(f"цели: не прочитан {target_path} ({exc}) — работают "
                        f"встроенные цели")
        return TargetSet(specs)
    for spec in parse_targets(text, warnings):
        specs[spec.name] = spec
    return TargetSet(specs)
