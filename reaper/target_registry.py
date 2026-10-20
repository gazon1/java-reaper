"""Реестр целей: встроенные описания и набор целей, с которым работает рейпер.

Здесь две вещи, которые неразлучны:

  * встроенные цели (Gradle и Kotlin) — то, что рейпер знал до появления
    файла целей; значения соответствуют прежнему поведению (см.
    `config.DEFAULTS`);
  * набор целей (`TargetSet`) — то, с чем работают правила политики, сбор
    демонов и отчёты: найти вид по процессу, узнать подписи, описать цели
    для отчёта.

Пороги в наборе не разрешаются: это делает `config.resolve_targets` —
здесь лежат только признаки целей.
"""

from __future__ import annotations

from typing import Any, Iterable

from .model import ACTIVITY_MARKERS, ACTIVITY_MTIME, GRADLE_MAIN, KOTLIN_MAIN
from .target_spec import TS_ISO, TS_LOCAL, TargetSpec

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
        # Логи Gradle: стандартный каталог и отдельный GRADLE_USER_HOME
        # (проекты часто ставят свой — там тоже копится мусор). Пути,
        # зависящие от раскладки машины, задаются в targets.yaml.
        log_globs=(
            "~/.gradle/daemon/*/daemon-*.out.log",
            "~/.gradle-user-home/daemon/*/daemon-*.out.log",
        ),
        # В имени лога Gradle PID настоящий — можно сверяться с живым
        # процессом, даже если файл уже не открыт.
        log_pid_regex=r"^daemon-(?P<pid>\d+)\.out\.log$",
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
        # Таймстемпы Kotlin пишутся без смещения («2026-10-20 08:55:16.690»),
        # то есть местным временем: разбирать их как iso со смещением нельзя.
        ts_format=TS_LOCAL,
        rebuildable=True,
        log_globs=("/tmp/kotlin-daemon.*.log*",),
        # Один демон пишет в несколько файлов (ротация .00/.01/.02) и держит
        # открытым только последний: открытый файл защищает всю группу.
        log_group_regex=(r"^(?P<group>kotlin-daemon\.\d{4}-\d{2}-\d{2}"
                         r"\.\d{2}-\d{2}-\d{2}-\d+)\.\d+\.log"
                         r"(?:\.lck)?$"),
        # Эти маркеры создаёт и удаляет сам Kotlin: убийство демона их не
        # оправдывает, а ломать может.
        forbidden_prefixes=(
            "kotlin-compiler-client-",
            "kotlin-compiler-in-",
            "kotlin-compilation-session-",
        ),
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

    def describe(self) -> dict[str, Any]:
        """Цели для отчёта — сгруппированные по смыслу.

        У цели 25 полей, и в плоском виде отчёт превращается в простыню.
        Здесь они собраны по группам: «как узнаём», «где логи», «как работает»,
        «пороги», «безопасность» — так видно, что цель умеет и по каким
        правилам живёт.
        """
        out: dict[str, Any] = {}
        for name, spec in self._specs.items():
            out[name] = {
                "label": spec.label or name,
                "match": {
                    "cmdline": spec.match_cmdline,
                    "comm": spec.match_comm,
                },
                "log": {
                    "name_regex": spec.log_name_regex,
                    "globs": list(spec.log_globs),
                    "group_regex": spec.log_group_regex,
                    "pid_regex": spec.log_pid_regex,
                    "forbidden_prefixes": list(spec.forbidden_prefixes),
                    "trim": spec.log_trim,
                },
                "work": {
                    "activity": spec.activity,
                    "ts_format": spec.ts_format,
                    "build_start": spec.build_start,
                    "build_end": spec.build_end,
                },
                "limits": {
                    "idle_seconds": spec.idle_seconds,
                    "recreate_cost_seconds": spec.recreate_cost_seconds,
                    "build_active_seconds": spec.build_active_seconds,
                    "stuck_after_seconds": spec.stuck_after_seconds,
                    "min_footprint_mb": spec.min_footprint_mb,
                    "large_daemon_mb": spec.large_daemon_mb,
                    "self_timeout_seconds": spec.self_timeout_seconds,
                },
                "safety": {
                    "rebuildable": spec.rebuildable,
                    "protects": list(spec.protects),
                },
            }
        return out

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
