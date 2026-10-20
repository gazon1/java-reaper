"""Файл целей (targets.yaml): разбор YAML и чтение файла.

Здесь всё, что касается файла: где он лежит, как разбирается и как
накладывается на встроенные цели. Само описание цели — в
`reaper.target_spec`, набор целей — в `reaper.target_registry`.

Правило здесь одно: оператор описывает в файле только то, что хочет
изменить, а всё остальное наследуется от встроенной цели с тем же именем.
Иначе переопределение одной цели молча ломало бы остальные её свойства
(например, формат таймстемпа).
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any

from .model import ACTIVITY_MARKERS, ACTIVITY_MTIME
from .target_registry import BUILTIN_TARGETS, TargetSet
from .target_spec import TS_ISO, TargetSpec

# Куда искать файл целей по умолчанию.
DEFAULT_TARGETS_PATH = "~/.config/java-reaper/targets.yaml"


def _spec_from_dict(raw: dict[str, Any], warnings: list[str],
                    name_hint: str,
                    base: TargetSpec | None = None) -> TargetSpec | None:
    """Собирает спецификацию цели из словаря YAML.

    `base` — встроенная цель с тем же именем, если она есть. Поля, которых
    нет в YAML, наследуются от неё: переопределение встроенной цели должно
    менять только то, что оператор написал, а не всё остальное молча.
    Иначе, например, цель «kotlin» без `ts_format` получила бы `iso` вместо
    `local` и перестала бы понимать свои таймстемпы, а цель без
    `log_name_regex` — «своего лога нет», и демон навсегда попадал бы под
    защиту `no-log`.
    """
    name = str(raw.get("name") or name_hint or "").strip()
    if not name:
        warnings.append("цели: у записи нет имени — пропущена")
        return None

    def field(field_name: str, default: Any) -> Any:
        """Значение поля: из YAML, иначе из встроенной цели, иначе дефолт.

        Явно заданное пустое значение (например `log_name_regex: ''` —
        «своего лога нет») уважается: это тоже намерение.
        """
        if field_name in raw and raw[field_name] is not None:
            return raw[field_name]
        if base is not None:
            return getattr(base, field_name)
        return default

    # Поля цели разложены по секциям — «как узнаём», «где логи», «как работает»,
    # «пороги», «безопасность». Каждая секция разбирается своим маленьким
    # помощником: так видно, к какой группе относится поле, и новое поле
    # сразу находит себе место (раньше всё было в одной простыне на 25 полей).
    def match_fields() -> dict[str, Any]:
        return {
            "match_cmdline": str(field("match_cmdline", "")),
            "match_comm": str(field("match_comm", "")),
        }

    def log_fields() -> dict[str, Any]:
        return {
            "log_name_regex": str(field("log_name_regex", "")),
            "log_trim": bool(field("log_trim", False)),
            "log_globs": _str_tuple(field("log_globs", ())),
            "log_group_regex": str(field("log_group_regex", "")),
            "log_pid_regex": str(field("log_pid_regex", "")),
            "forbidden_prefixes": _str_tuple(field("forbidden_prefixes", ())),
        }

    def work_fields() -> dict[str, Any]:
        return {
            "activity": str(field("activity", ACTIVITY_MTIME)),
            "ts_format": str(field("ts_format", TS_ISO)),
            "build_start": str(field("build_start", "")),
            "build_end": str(field("build_end", "")),
            "build_end_alt": str(field("build_end_alt", "")),
            "cancel": str(field("cancel", "")),
            "bucket_seconds": (int(field("bucket_seconds", 0) or 0)
                               if str(field("bucket_seconds", 0) or 0).isdigit()
                               else 0),
        }

    def limit_fields() -> dict[str, Any]:
        return {
            "idle_seconds": _optional_float(field("idle_seconds", None)),
            "recreate_cost_seconds": _optional_float(
                field("recreate_cost_seconds", None)),
            "build_active_seconds": _optional_float(
                field("build_active_seconds", None)),
            "stuck_after_seconds": _optional_float(
                field("stuck_after_seconds", None)),
            "min_footprint_mb": (None if field("min_footprint_mb", None) in (None, "")
                                 else int(field("min_footprint_mb", 0))),
            "large_daemon_mb": (None if field("large_daemon_mb", None) in (None, "")
                                else int(field("large_daemon_mb", 0))),
            "self_timeout_seconds": _optional_float(
                field("self_timeout_seconds", None)),
        }

    def safety_fields() -> dict[str, Any]:
        protects = field("protects", ())
        if isinstance(protects, str):
            protects = (protects,)
        return {
            "protects": tuple(str(p) for p in protects),
            "rebuildable": bool(field("rebuildable", False)),
        }

    spec = TargetSpec(
        name=name,
        label=str(field("label", "")),
        **match_fields(),
        **log_fields(),
        **work_fields(),
        **limit_fields(),
        **safety_fields(),
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


def _str_tuple(value: Any) -> tuple[str, ...]:
    """Строка или список строк → кортеж строк."""
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_targets(text: str, warnings: list[str] | None = None,
                  base_specs: dict[str, TargetSpec] | None = None
                  ) -> list[TargetSpec]:
    """Разбирает YAML с целями.

    Требуется PyYAML. Если его нет — возвращаем пустой список и предупреждение:
    рейпер обязан работать и без файла целей (встроенные цели).

    `base_specs` — цели, поля которых наследуются, если их нет в YAML
    (см. `_spec_from_dict`).
    """
    warnings = warnings if warnings is not None else []
    base_specs = base_specs or {}
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
        name = str(raw.get("name") or "").strip()
        spec = _spec_from_dict(raw, warnings, name_hint=f"#{index + 1}",
                               base=base_specs.get(name))
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
    for spec in parse_targets(text, warnings, base_specs=specs):
        specs[spec.name] = spec
    return TargetSet(specs)
