"""Исполнение решений: сигналы через pidfd, эскалация, лимиты."""

from __future__ import annotations

import errno
import os
import signal
import time
from typing import Any

from .model import Decision


def open_pidfd(pid: int) -> int | None:
    try:
        return os.pidfd_open(pid)
    except (AttributeError, OSError):
        return None


def send_signal(pid: int, pidfd: int | None, sig: int) -> bool:
    try:
        if pidfd is not None:
            signal.pidfd_send_signal(pidfd, sig)
        else:
            os.kill(pid, sig)
        return True
    except (ProcessLookupError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.ESRCH:
            return False
        return False


def still_same(pid: int, starttime: int) -> bool:
    """Тот ли это процесс? Зомби (умер, но не подобран) — уже не процесс."""
    from .proc import read_proc_stat
    st = read_proc_stat(pid)
    return (st is not None and st["starttime"] == starttime
            and st["state"] != "Z")


def kill_order(decisions: list[Decision]) -> list[Decision]:
    """Порядок уборки: по выгоде — сколько мегабайт освобождаем на секунду
    цены воссоздания.

    Дорогой Gradle-демон (холодный старт ~40 с) уступает дешёвому Kotlin-ному
    (~5 с): тот даёт больше освобождённой памяти на единицу потраченного
    времени. Второй ключ — Gradle первыми: их shutdown-hook чистит маркеры
    клиентов, и это нужно сделать до того, как ляжет Kotlin-демон.
    """
    return sorted(decisions, key=lambda d: (
        -(d.daemon.footprint_mb / max(d.recreate_cost_seconds, 1.0)),
        0 if d.daemon.kind == "gradle" else 1))


def execute(decisions: list[Decision], state: dict[str, Any], now: float,
            cfg: dict[str, Any], dry_run: bool) -> list[dict[str, Any]]:
    """Убивает подтверждённых кандидатов. Возвращает отчёт о действиях."""
    report: list[dict[str, Any]] = []
    to_kill = kill_order([d for d in decisions if d.kill])
    if not to_kill:
        return report

    # Страховка от массовой ошибки правил: за свип — не больше N демонов.
    # Отбор идёт по тому же порядку выгоды, что и сама уборка.
    limit = int(cfg.get("MAX_KILLS_PER_SWEEP", 3))
    if limit > 0 and len(to_kill) > limit:
        overflow = to_kill[limit:]
        to_kill = to_kill[:limit]
        for d in overflow:
            report.append({"pid": d.daemon.pid, "action": "skipped",
                           "reason": f"превышен лимит {limit} убийств за свип"})

    # Защита Kotlin-демонов, если где-то идёт сборка Gradle, живёт в таблице
    # правил (правило «gradle-building»), а не здесь: решение, отчёт и
    # метрика должны говорить одно и то же.

    handles: dict[int, int | None] = {}
    confirmed: list[Decision] = []
    for dec in to_kill:
        d = dec.daemon
        if not still_same(d.pid, d.starttime):
            report.append({"pid": d.pid, "action": "skipped", "reason": "PID переиспользован"})
            continue
        pidfd = open_pidfd(d.pid)
        if pidfd is not None and not still_same(d.pid, d.starttime):
            report.append({"pid": d.pid, "action": "skipped", "reason": "PID переиспользован"})
            continue
        handles[d.pid] = pidfd
        confirmed.append(dec)

    if dry_run:
        for dec in confirmed:
            report.append({"pid": dec.daemon.pid, "action": "dry-run",
                           "reason": dec.reason,
                           "footprint_mb": dec.daemon.footprint_mb})
        for pid, pidfd in handles.items():
            if pidfd is not None:
                os.close(pidfd)
        return report

    for dec in confirmed:
        ok = send_signal(dec.daemon.pid, handles.get(dec.daemon.pid), signal.SIGTERM)
        report.append({"pid": dec.daemon.pid, "action": "SIGTERM" if ok else "SIGTERM failed",
                       "reason": dec.reason, "footprint_mb": dec.daemon.footprint_mb})
        rec = state.setdefault("daemons", {}).setdefault(str(dec.daemon.pid), {})
        rec["sigterm_attempts"] = int(rec.get("sigterm_attempts", 0)) + 1

    deadline = time.time() + float(cfg["GRACE_SECONDS"])
    while time.time() < deadline:
        if not any(still_same(d.daemon.pid, d.daemon.starttime) for d in confirmed):
            break
        time.sleep(0.5)

    for dec in confirmed:
        d = dec.daemon
        if not still_same(d.pid, d.starttime):
            continue
        rec = state.setdefault("daemons", {}).setdefault(str(d.pid), {})
        if d.kind == "kotlin":
            send_signal(d.pid, handles.get(d.pid), signal.SIGKILL)
            report.append({"pid": d.pid, "action": "SIGKILL", "reason": "не вышел за grace",
                           "footprint_mb": d.footprint_mb})
        else:
            attempts = int(rec.get("sigterm_attempts", 0))
            if attempts >= int(cfg["GRADLE_SIGKILL_AFTER_TRIES"]):
                send_signal(d.pid, handles.get(d.pid), signal.SIGKILL)
                report.append({"pid": d.pid, "action": "SIGKILL",
                               "reason": f"{attempts} попыток SIGTERM безуспешны",
                               "footprint_mb": d.footprint_mb})

    for pid, pidfd in handles.items():
        if pidfd is not None:
            os.close(pidfd)
    return report
