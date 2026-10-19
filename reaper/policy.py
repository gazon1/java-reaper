"""Политика: декларативная таблица правил «когда убивать».

Каждое правило — это условие и следствие. Порядок правил кодирует приоритет:
первое совпавшее правило решает. Таблица читается как спецификация и
проверяется инвариантными тестами (см. tests/test_policy.py).

Базовый принцип: **неизвестное ⇒ оставить**. Демон, который мы не можем
измерить (нет лога, нет baseline, неизвестный владелец соединения), не
убивается никогда.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .model import Action, Protector


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


@dataclass(frozen=True)
class Rule:
    """Одно правило политики."""

    name: str
    action: Action
    check: Callable[[Signals], bool]
    reason: Callable[[Signals], str]
    busy: bool = False          # демон признан занятым (сбрасывает отсчёт простоя)
    protected: bool = False     # демон защищён неизвестным/наблюдателем


def _fmt_protectors(protectors: list[Protector]) -> str:
    return "; ".join(p.detail for p in protectors)


def default_policy(cfg: dict) -> list[Rule]:
    """Таблица правил по убыванию приоритета.

    Порядок: ограничение области → «не знаем» → наблюдатель → активность →
    размер → простой → вывод.
    """
    idle_limit = float(cfg["IDLE_SECONDS"])
    min_footprint = int(cfg["MIN_FOOTPRINT_MB"])

    def not_allowed(s: Signals) -> bool:
        return not s.allowed

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
        return s.daemon.footprint_mb < min_footprint

    def building(s: Signals) -> bool:
        return s.build_in_progress and s.age < idle_limit

    def not_idle(s: Signals) -> bool:
        return s.age < idle_limit

    def always(s: Signals) -> bool:
        return True

    return [
        Rule("allowlist", Action.KEEP, not_allowed,
             lambda s: "PID вне списка разрешённых — оставляю"),
        Rule("no-log", Action.KEEP, no_log,
             lambda s: "лог не найден — защищаю", protected=True),
        Rule("unknown-history", Action.KEEP, unknown_history,
             lambda s: "история лога неизвестна (лог не разобран) — защищаю",
             protected=True),
        Rule("no-baseline", Action.KEEP, no_baseline,
             lambda s: "нет baseline (первый свип) — защищаю", protected=True),
        Rule("peer", Action.KEEP, has_peer,
             lambda s: _fmt_protectors(s.peer_protection), protected=True),
        Rule("cpu-busy", Action.KEEP, busy_cpu,
             lambda s: f"занят: CPU {s.cpu_delta:.1f} с за свип", busy=True),
        Rule("children", Action.KEEP, busy_children,
             lambda s: "есть живые дети: "
                       + ", ".join(str(c) for c in s.daemon.children), busy=True),
        Rule("recent-log", Action.KEEP, recent_log,
             lambda s: "лог обновлялся недавно", busy=True),
        Rule("small-footprint", Action.KEEP, small,
             lambda s: f"footprint {s.daemon.footprint_mb} МБ < {min_footprint} МБ"),
        Rule("building", Action.KEEP, building,
             lambda s: "сборка идёт (старт без конца)", busy=True),
        Rule("not-idle", Action.KEEP, not_idle,
             lambda s: f"простой {s.age / 60:.0f} мин < {idle_limit / 60:.0f} мин"),
        Rule("kill", Action.KILL, always,
             lambda s: (f"простой {s.age / 3600:.1f} ч, "
                        f"footprint {s.daemon.footprint_mb} МБ"
                        + (", зависшая сборка (старт без конца)"
                           if s.build_in_progress else ""))),
    ]
