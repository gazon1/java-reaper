"""Модель: демон, вердикт, защиты."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

GRADLE_MAIN = "org.gradle.launcher.daemon.bootstrap.GradleDaemon"
KOTLIN_MAIN = "org.jetbrains.kotlin.daemon.KotlinCompileDaemon"

# Как цель измеряет активность демона. Константы живут здесь, а не в
# `targets`, потому что модель демона тоже ими пользуется (а `targets`
# импортирует модель — иначе получился бы круг импорта).
ACTIVITY_MTIME = "mtime"       # признак активности — mtime лога
ACTIVITY_MARKERS = "markers"   # активность — маркеры в тексте лога

# Дети, которые не считаются «работой» и потому не защищают протухший демон.
# Сопоставляется ТОЛЬКО имя исполняемого файла: поиск подстроки по всему
# cmdline ловил classpath-пути вроде «…playwright-1.49.0.jar» и ошибочно
# объявлял шумом реально работающий fork приложения.
CHILD_IGNORE_EXECUTABLES = frozenset({
    "adb", "node", "nodejs", "python", "python2", "python3", "ollama", "mcp",
    "playwright", "chrome", "chromium", "google-chrome", "chromium-browser",
})


class Action(str, Enum):
    """Что делаем с демоном. Решение считается ровно один раз (см. Decision)."""

    KILL = "kill"
    KEEP = "keep"


@dataclass
class Protector:
    """Причина, по которой демон остаётся."""

    kind: str                 # peer | child | cpu | log | baseline | ...
    detail: str               # человекочитаемое описание
    pid: int | None = None
    name: str | None = None   # имя процесса, если применимо


@dataclass
class PeerConn:
    """Established-соединение демона с локальным клиентом."""

    inode: str
    local_port: int
    remote_port: int
    owner_pid: int | None = None
    owner_name: str | None = None
    first_seen: float | None = None   # когда впервые увидели (из состояния)
    age: float | None = None          # now - first_seen
    peer_inode: str | None = None     # inode второй стороны соединения


@dataclass
class Daemon:
    pid: int
    kind: str
    starttime: int
    cpu_ticks: int
    rss_mb: int
    swap_mb: int
    log_path: str | None = None
    log_ok: bool = True
    idle_known: bool = True
    log_offset: int = 0
    last_build_start: float | None = None
    last_build_end: float | None = None
    log_mtime: float | None = None
    children: list[int] = field(default_factory=list)
    ignored_children: list[int] = field(default_factory=list)
    peer_pids: list[int] = field(default_factory=list)
    peer_unknown: int = 0
    peer_conns: list[PeerConn] = field(default_factory=list)
    peer_first_seen: dict[str, float] = field(default_factory=dict)
    # Демон «завис»: клиент отвалился во время сборки, сборка отменена, но
    # демон остался в состоянии «идёт сборка». Gradle не может его убрать —
    # его idle-таймаут не действует, пока демон «в сборке».
    # Признак виден по логу: «client disconnection detected, canceling the build».
    stuck_build: bool = False
    # Момент последней отмены сборки (по логу).
    canceled_at: float | None = None
    # Как эта цель измеряет активность («mtime» или «markers»). Заполняет
    # сборщик из описания цели (targets.yaml). Пусто — вид не описан,
    # и тогда действует запасное правило (см. observed_activity).
    activity: str = ""

    @property
    def footprint_mb(self) -> int:
        return self.rss_mb + self.swap_mb

    @property
    def build_in_progress(self) -> bool:
        if self.last_build_start is None:
            return False
        if self.last_build_end is None:
            return True
        return self.last_build_start > self.last_build_end


def observed_activity(d: Daemon) -> float | None:
    """Когда демон последний раз что-то делал — по его логу.

    У цели с маркерами (activity=markers) это конец последней сборки,
    у остальных — mtime лога. Вид активности задаёт описание цели, а не
    код: так новая программа работает по тем же правилам, что и Gradle.

    Если вид не описан (демон собран вручную, минуя сборщик), берём то,
    что есть: конец сборки, если он есть, иначе mtime лога.
    """
    if d.activity == ACTIVITY_MTIME:
        return d.log_mtime
    if d.activity == ACTIVITY_MARKERS:
        return d.last_build_end
    if d.last_build_end is not None:
        return d.last_build_end
    return d.log_mtime


@dataclass
class Context:
    """Контекст всего набора демонов.

    То, что невозможно знать по одному демону: сколько в системе свободной
    памяти, идёт ли где-то свежая сборка. Правила читают это через
    `Signals.context`.

    Отдельный объект — а не поля в `Signals`, потому что контекст общий, а
    демонный. Смешивать их опасно: когда «сборка идёт» лежало рядом с
    признаками одного демона, оно означало «последняя сборка не завершилась»
    — а у зависшего демона это верно навсегда, и один зависший Gradle-демон
    блокировал уборку всех Kotlin-демонов.

    Свежая сборка хранится по ВИДУ цели (`build_ages`), а не по имени
    программы: «Gradle защищает Kotlin» — это отношение между видами,
    описанное в targets.yaml (`protects`), а не знание о Gradle.
    """

    now: float = 0.0
    # Сколько памяти доступно системе (МБ). None — измерить не удалось.
    mem_available_mb: int | None = None
    # Сколько секунд прошло с начала самой свежей сборки, по видам демонов.
    # None — свежей сборки нет.
    build_ages: dict[str, float] = field(default_factory=dict)
    daemon_count: int = 0
    total_footprint_mb: int = 0

    @property
    def gradle_build_age(self) -> float | None:
        """Совместимость со старым полем: возраст свежей сборки Gradle."""
        return self.build_ages.get("gradle")


@dataclass
class Decision:
    """Единственный источник правды о судьбе демона.

    Из него читают и отчёт, и запись состояния, и исполнитель — чтобы
    проверка «занят / защищён / убивать» не расходилась между местами.
    """

    daemon: Daemon
    action: Action
    reason: str
    idle_seconds: float | None = None
    last_activity: float | None = None
    busy: bool = False
    protected: bool = False
    protectors: list[Protector] = field(default_factory=list)
    stale_peers: list[Protector] = field(default_factory=list)
    # Цена воссоздания демона (секунды) — влияет на порядок уборки.
    recreate_cost_seconds: float = 0.0
    # Контекст, в котором принято решение (свободная память, свежая сборка).
    context: "Context | None" = None
    # Имя правила, которое сработало («not-idle», «stuck», «kill»…).
    # Нужно для наблюдаемости: видно, какие правила вообще работают,
    # и деградацию детекции («все демоны ушли в unknown-history»).
    rule: str | None = None

    def __post_init__(self) -> None:
        # Совместимость: Decision(d, True, "…") — как в старом монолите.
        if isinstance(self.action, bool):
            self.action = Action.KILL if self.action else Action.KEEP

    @property
    def kill(self) -> bool:
        return self.action is Action.KILL
