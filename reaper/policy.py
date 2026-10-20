"""Политика: декларативная таблица правил «когда убивать».

Каждое правило — это условие и следствие. Порядок правил кодирует приоритет:
первое совпавшее правило решает. Таблица читается как спецификация и
проверяется инвариантными тестами (см. tests/test_policy.py).

Каждое правило несёт `doc` (что это значит по-человечески) и `condition`
(формальное условие) — они печатаются командой `--explain` и проверяются
тестом «у каждого правила есть описание».

Базовый принцип: **неизвестное ⇒ оставить**. Демон, который мы не можем
измерить (нет лога, нет baseline, неизвестный владелец соединения), не
убивается никогда.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .model import Action, Context, Protector


@dataclass
class Signals:
    """Наблюдаемые признаки одного демона — все в одном месте.

    Правила читают только их; ничего не вычисляется повторно в разных местах.
    """

    daemon: object                       # reaper.model.Daemon
    now: float
    age: float                           # секунд простоя
    idle_limit: float
    min_footprint: int
    cpu_delta: float | None = None
    cpu_busy: bool = False
    has_children: bool = False
    log_recent: bool = False
    build_in_progress: bool = False
    baseline_known: bool = False
    log_ok: bool = True
    idle_known: bool = True
    allowed: bool = True
    peer_protection: list[Protector] = field(default_factory=list)
    peer_stale: list[Protector] = field(default_factory=list)
    # Демон «завис»: клиент отвалился во время сборки. Gradle не может его
    # убрать — idle-таймаут не действует, пока демон «в сборке».
    stuck_build: bool = False
    # Сколько секунд прошло с момента отмены сборки.
    stuck_age: float = 0.0
    # Контекст всего набора демонов: свободная память, свежая сборка
    # (по видам демонов). Правила читают его, а не каждый демон сам по себе.
    context: Context = field(default_factory=Context)
    # Пороги для ЭТОГО вида демонов (из targets.yaml). Ноль/None — правило
    # берёт значение из плоского конфига (запасной путь).
    min_footprint: int = 0
    large_daemon_mb: int = 0
    stuck_after: float = 0.0
    build_active: float = 0.0
    # Пересоздаваем ли этого демона (targets.yaml → rebuildable). False —
    # это сервис или база данных: убивать его нельзя никогда.
    # По умолчанию False: «не знаем ⇒ не трогаем».
    rebuildable: bool = False
    # Какие виды демонов защищают этого, пока сами работают (из targets.yaml).
    protector_kinds: list[str] = field(default_factory=list)
    # То же самое человеческими именами («Gradle», а не «gradle») — для отчёта.
    protector_labels: list[str] = field(default_factory=list)
    # Цена ошибки: сколько секунд стоит воссоздать этого демона (холодный
    # старт). Влияет на порядок уборки, а не на сам факт.
    recreate_cost_seconds: float = 0.0


@dataclass(frozen=True)
class Rule:
    """Одно правило политики.

    name      — идентификатор (уникален в таблице);
    doc       — что правило значит по-человечески;
    condition — формальное условие срабатывания;
    action    — что делаем, если условие выполнено;
    check     — предикат;
    reason    — текст причины для отчёта.
    """

    name: str
    doc: str
    condition: str
    action: Action
    check: Callable[[Signals], bool]
    reason: Callable[[Signals], str]
    busy: bool = False          # демон признан занятым (сбрасывает отсчёт простоя)
    protected: bool = False     # демон защищён неизвестным/наблюдателем


def kill_rule_names(cfg: dict) -> list[str]:
    """Имена правил, которые убивают.

    Список берётся из таблицы политики, а не пишется руками: правило может
    стать убивающим или защищающим при правке таблицы, и тогда записанный
    где-то список разошёлся бы с реальностью. «Kill-правило не сработало»
    и «защитное правило не сработало» — разные вещи, поэтому различать их
    нужно по таблице, а не по памяти.
    """
    return [rule.name for rule in default_policy(cfg) if rule.action is Action.KILL]


def _fmt_protectors(protectors: list[Protector]) -> str:
    return "; ".join(p.detail for p in protectors)


def default_policy(cfg: dict) -> list[Rule]:
    """Таблица правил по убыванию приоритета.

    Порядок: ограничение области → «не знаем» → наблюдатель → активность →
    размер → простой → вывод.
    """
    idle_limit = float(cfg["IDLE_SECONDS"])
    stuck_after = float(cfg.get("STUCK_AFTER_SECONDS", 3600))
    build_active = float(cfg.get("GRADLE_BUILD_ACTIVE_SECONDS", 1800))
    min_footprint = int(cfg["MIN_FOOTPRINT_MB"])
    pressure_mb = int(cfg.get("MEMORY_PRESSURE_MB", 4096))
    large_mb = int(cfg.get("KILL_LARGE_DAEMON_MB", 4096))
    peer_max_age = float(cfg.get("PEER_MAX_AGE_SECONDS", 21600))

    def not_allowed(s: Signals) -> bool:
        return not s.allowed

    def not_rebuildable(s: Signals) -> bool:
        return not s.rebuildable

    def no_log(s: Signals) -> bool:
        return not s.log_ok

    def unknown_history(s: Signals) -> bool:
        return not s.idle_known

    def no_baseline(s: Signals) -> bool:
        return not s.baseline_known

    def has_peer(s: Signals) -> bool:
        return bool(s.peer_protection)

    def busy_cpu(s: Signals) -> bool:
        return s.cpu_busy

    def busy_children(s: Signals) -> bool:
        return s.has_children

    def recent_log(s: Signals) -> bool:
        return s.log_recent

    def small(s: Signals) -> bool:
        limit = s.min_footprint or min_footprint
        return s.daemon.footprint_mb < limit

    def building(s: Signals) -> bool:
        return s.build_in_progress and s.age < s.idle_limit

    def not_idle(s: Signals) -> bool:
        return s.age < s.idle_limit

    def protected_by_build(s: Signals) -> bool:
        """Защита демонов одного вида, пока идёт свежая сборка другого.

        Правило живёт в таблице, а не в исполнителе: тогда решение,
        отчёт и метрика говорят одно и то же (раньше исполнитель резал
        кандидатов в последний момент, а отчёт и метрика считали их
        «убитыми»).

        Кто кого защищает — отношение между видами (`protects` в
        targets.yaml), а не знание о конкретной программе: так правило
        работает и для новых целей.

        Защищает только СВЕЖАЯ сборка (началась недавно): сборка,
        начавшаяся несколько часов назад и не завершившаяся, — это
        зависший демон, и он сам подлежит уборке.
        """
        window = s.build_active or build_active
        for protector in s.protector_kinds:
            age = s.context.build_ages.get(protector)
            if age is not None and age <= window:
                return True
        return False

    def always(s: Signals) -> bool:
        return True

    def memory_not_needed(s: Signals) -> bool:
        """Памяти хватает, а демон небольшой — убирать незачем.

        Рейпер существует ради памяти: если свободной памяти достаточно,
        а демон занимает мало, то его убийство — это только цена (холодный
        старт) без выгоды. Крупные демоны убираются в любом случае:
        столько памяти жалко держать простаивающим.
        """
        available = s.context.mem_available_mb
        if available is None:
            return False  # не измерили — ведём себя как раньше
        if pressure_mb <= 0:
            return False  # режим «всегда убирать»
        if available < pressure_mb:
            return False  # память нужна — убираем
        large = s.large_daemon_mb or large_mb
        return s.daemon.footprint_mb < large

    def stuck(s: Signals) -> bool:
        """Демон завис: клиент отвалился во время сборки.

        Работать некому, и работать уже некому. Gradle его не убирает:
        его idle-таймаут (180 мин) не действует, пока демон считается
        «в сборке». Это единственная категория, которую убирает рейпер,
        а Gradle — нет.

        Порог СВОЙ (STUCK_AFTER_SECONDS), а не общий порог простоя: ждать
        шесть часов демон, которому работать некому, незачем. Демон с
        живым клиентом защищён правилом `peer` — раньше него.
        """
        threshold = s.stuck_after or stuck_after
        return s.stuck_build and s.stuck_age > threshold

    return [
        Rule("allowlist",
             "Вне прицельного списка PID — не трогаем вообще.",
             "PID_ALLOWLIST задан и PID в него не входит",
             Action.KEEP, not_allowed,
             lambda s: "PID вне списка разрешённых — оставляю"),
        Rule("not-rebuildable",
             "Демон не пересоздаваемый: это сервис или данные, а не кэш. "
             "Убивать его нельзя никогда — цена ошибки здесь не «холодный "
             "старт», а потерянная работа.",
             "цель не помечена rebuildable в targets.yaml",
             Action.KEEP, not_rebuildable,
             lambda s: "цель не пересоздаваемая — не убиваю никогда",
             protected=True),
        Rule("no-log",
             "Демон не держит свой лог — нечем измерить простой.",
             "лог не найден через /proc/PID/fd",
             Action.KEEP, no_log,
             lambda s: "лог не найден — защищаю", protected=True),
        Rule("unknown-history",
             "Лог есть, но его история неизвестна (файл больше предела разбора).",
             "разбор лога неполный и маркеры не найдены",
             Action.KEEP, unknown_history,
             lambda s: "история лога неизвестна (лог не разобран) — защищаю",
             protected=True),
        Rule("no-baseline",
             "Первый свип по демону: не с чем сравнить CPU — наблюдаем.",
             "нет предыдущего измерения CPU",
             Action.KEEP, no_baseline,
             lambda s: "нет baseline (первый свип) — защищаю", protected=True),
        Rule("peer",
             "За демоном наблюдает живой клиент (или висит соединение с владельцем, "
             "которого мы не видим).",
             f"есть локальное Established-соединение с клиентом (свежее, < {peer_max_age / 3600:.0f} ч)",
             Action.KEEP, has_peer,
             lambda s: _fmt_protectors(s.peer_protection), protected=True),
        Rule("cpu-busy",
             "Демон наработал CPU за свип — он работает.",
             "CPU за свип > BUSY_CPU_SECONDS",
             Action.KEEP, busy_cpu,
             lambda s: f"занят: CPU {s.cpu_delta:.1f} с за свип", busy=True),
        Rule("children",
             "У демона есть живой дочерний процесс — он работает.",
             "есть живой ребёнок (не из ignore-списка)",
             Action.KEEP, busy_children,
             lambda s: "есть живые дети: "
                       + ", ".join(str(c) for c in s.daemon.children), busy=True),
        Rule("recent-log",
             "Лог обновлялся в пределах свипа — демон активен.",
             "mtime лога или конец сборки свежее SWEEP_SECONDS",
             Action.KEEP, recent_log,
             lambda s: "лог обновлялся недавно", busy=True),
        Rule("small-footprint",
             "Демон занимает мало памяти — дешевле пересоздать, чем рисковать.",
             f"footprint (RSS+swap) < {min_footprint} МБ",
             Action.KEEP, small,
             lambda s: f"footprint {s.daemon.footprint_mb} МБ < {min_footprint} МБ"),
        Rule("stuck",
             "Зависший демон: клиент отвалился во время сборки. Работать "
             "некому, и работать уже некому — сам демон такой не убирает "
             "(его idle-таймаут не действует, пока демон «в сборке»).",
             f"была отмена сборки (клиент отвалился), сборка не завершилась, "
             f"и с отмены прошло > {stuck_after / 60:.0f} мин",
             Action.KILL, stuck,
             lambda s: (f"завис: клиент отвалился {s.stuck_age / 3600:.1f} ч назад, "
                        f"работать некому; footprint {s.daemon.footprint_mb} МБ")),
        Rule("building",
             "Идёт сборка: демон начал её недавно.",
             "есть старт сборки без конца и простой < порога простоя",
             Action.KEEP, building,
             lambda s: "сборка идёт (старт без конца)", busy=True),
        Rule("not-idle",
             "Демон простаивает недостаточно долго.",
             "простой < порога простоя для этого класса демона",
             Action.KEEP, not_idle,
             lambda s: f"простой {s.age / 60:.0f} мин < {s.idle_limit / 60:.0f} мин"),
        Rule("protected-by-build",
             "Идёт свежая сборка другого вида демонов — они сейчас нужны.",
             f"есть демон вида, который защищает этот вид, со сборкой без конца, "
             f"начавшейся не позже {build_active / 60:.0f} мин назад",
             Action.KEEP, protected_by_build,
             lambda s: ("идёт сборка "
                        + ", ".join(s.protector_labels or s.protector_kinds)
                        + " — не трогаю"),
             protected=True),
        Rule("memory-not-needed",
             "Свободной памяти хватает, а демон небольшой: убивать его — "
             "платить холодным стартом без выгоды.",
             f"MemAvailable ≥ MEMORY_PRESSURE_MB ({pressure_mb} МБ) "
             f"и footprint < KILL_LARGE_DAEMON_MB ({large_mb} МБ)",
             Action.KEEP, memory_not_needed,
             lambda s: (f"свободной памяти хватает "
                        f"({s.context.mem_available_mb} МБ), демон небольшой — "
                        f"не трогаю")),
        Rule("kill",
             "Все условия кандидата выполнены: простоя достаточно, памяти достаточно, "
             "демон никем не наблюдается.",
             "иначе (сюда попадают только прошедшие все проверки)",
             Action.KILL, always,
             lambda s: (f"простой {s.age / 3600:.1f} ч "
                        f"(порог {s.idle_limit / 3600:.0f} ч), "
                        f"footprint {s.daemon.footprint_mb} МБ"
                        + (f", цена воссоздания ~{s.recreate_cost_seconds:.0f} с"
                           if s.recreate_cost_seconds else "")
                        + (", зависшая сборка (старт без конца)"
                           if s.build_in_progress else ""))),
    ]
