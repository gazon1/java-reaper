"""Сбор данных о демонах: кто живёт, чем занят, с кем соединён."""

from __future__ import annotations

import os
from typing import Any

from .logs import scan_gradle_log
from .model import Daemon, PeerConn
from .proc import (
    children_map,
    daemon_kind,
    find_log_path,
    inode_owner_map,
    is_ignored_child,
    proc_comm,
    proc_cmdline,
    read_memory,
    read_proc_stat,
    socket_inodes,
    tcp_table,
)


def _max_ts(*values: Any) -> float | None:
    known = [float(v) for v in values if v is not None]
    return max(known) if known else None


def peer_key(conn: PeerConn) -> str:
    """Идентичность соединения: inode демонной стороны + порты."""
    return f"{conn.inode}:{conn.local_port}:{conn.remote_port}"


def collect_daemons(cfg: dict[str, Any], state: dict[str, Any],
                    now: float | None = None) -> list[Daemon]:
    """Собирает список демонов и их признаки.

    Состояние используется для двух вещей: инкрементального разбора лога
    (смещение) и возраста peer-соединений (когда мы их увидели впервые).
    """
    now = time_now() if now is None else now
    tcp = tcp_table()
    owners = inode_owner_map()
    kids = children_map()
    prev = state.get("daemons", {})
    daemons: list[Daemon] = []

    for entry in sorted(os.listdir("/proc")):
        if not entry.isdigit():
            continue
        pid = int(entry)
        kind = daemon_kind(proc_cmdline(pid))
        if kind is None:
            continue
        st = read_proc_stat(pid)
        if st is None:
            continue
        rss, swap = read_memory(pid)
        d = Daemon(
            pid=pid,
            kind=kind,
            starttime=int(st["starttime"]),
            cpu_ticks=int(st["utime"]) + int(st["stime"]),
            rss_mb=rss // 1024,
            swap_mb=swap // 1024,
        )

        rec = prev.get(str(pid)) or {}
        if rec.get("starttime") is not None and rec["starttime"] != d.starttime:
            # PID переиспользован — прежние накопления не про этот процесс.
            rec = {}

        d.log_path = find_log_path(pid)
        if d.log_path is None:
            d.log_ok = False
        elif kind == "gradle":
            res = scan_gradle_log(d.log_path, int(rec.get("log_offset", 0)),
                                  int(cfg["LOG_SCAN_MAX_BYTES"]))
            d.log_offset = int(res["offset"])
            d.idle_known = bool(res["complete"])
            d.last_build_start = _max_ts(rec.get("last_build_start"), res["start"])
            d.last_build_end = _max_ts(rec.get("last_build_end"), res["end"])
        else:
            try:
                d.log_mtime = os.path.getmtime(d.log_path)
            except OSError:
                d.log_ok = False

        for child in kids.get(pid, []):
            if is_ignored_child(proc_cmdline(child)):
                d.ignored_children.append(child)
            else:
                d.children.append(child)

        _collect_peers(d, tcp, owners, rec, now)
        daemons.append(d)
    return daemons


def _collect_peers(d: Daemon, tcp: dict[str, Any], owners: dict[str, int],
                   rec: dict[str, Any], now: float) -> None:
    """Находит Established-соединения демона с локальными клиентами."""
    my_inodes = socket_inodes(d.pid)
    seen_at: dict[str, float] = rec.get("peer_first_seen") or {}
    fresh: dict[str, float] = {}

    for ino, (lport, rport, state, l_loop, r_loop) in tcp.items():
        if ino not in my_inodes or state != "01":  # 01 = ESTABLISHED
            continue
        if not (l_loop or r_loop):
            # Исходящее соединение наружу (например, HTTPS к репозиторию):
            # клиента за ним нет, наблюдателем оно не является.
            continue
        peer_inode = None
        for other, (ol, orr, ost, _ol, _or) in tcp.items():
            if ost == "01" and ol == rport and orr == lport:
                peer_inode = other
                break
        owner = owners.get(peer_inode) if peer_inode else None
        if owner == d.pid:
            # Соединение с самим собой (внутренний loopback) — не наблюдатель.
            continue
        conn = PeerConn(inode=ino, local_port=lport, remote_port=rport,
                        owner_pid=owner,
                        owner_name=proc_comm(owner) if owner else None)
        key = peer_key(conn)
        first = seen_at.get(key)
        if first is None:
            first = now
        fresh[key] = first
        conn.first_seen = first
        conn.age = now - first
        d.peer_conns.append(conn)
        if owner is None:
            d.peer_unknown += 1
        else:
            d.peer_pids.append(owner)
    d.peer_first_seen = fresh


def time_now() -> float:
    import time
    return time.time()
