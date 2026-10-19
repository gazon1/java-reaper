"""Сбор данных о демонах: кто живёт, чем занят, с кем соединён."""

from __future__ import annotations

import os
import time
from typing import Any

from .logs import scan_gradle_log
from .model import Daemon, PeerConn
from .proc import (
    children_map,
    daemon_kind,
    find_log_path,
    find_socket_owners,
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

    Порядок обхода /proc выбран из-за цены: сначала находим демонов и их
    сокеты, потом — только нужных владельцев peer-соединений (прицельный
    поиск с ранним выходом). Полный обход fd всех процессов стоил 0.27 с
    на машине с 600 процессами ради одного-двух нужных inode.
    """
    now = time.time() if now is None else now
    tcp = tcp_table()
    kids = children_map()
    prev = state.get("daemons", {})
    daemons: list[Daemon] = []

    # Обратный индекс TCP: (local_port, remote_port) → inode.
    # Только Established: TIME_WAIT-сокет второй стороны не ищем.
    # Ищем peer-соединение демона за O(1), а не перебором всей таблицы.
    tcp_by_ports: dict[tuple[int, int], list[str]] = {}
    for inode, (lport, rport, st, _ll, _rl) in tcp.items():
        if st != "01":  # 01 = ESTABLISHED
            continue
        tcp_by_ports.setdefault((lport, rport), []).append(inode)

    # Фаза 1: демоны и их логи/память/дети.
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

        d.peer_conns = _daemon_connections(d, tcp, tcp_by_ports)
        daemons.append(d)

    # Фаза 2: владельцы peer-соединений — прицельно и с ранним выходом.
    # Нужны только владельцы второй стороны соединения (клиента).
    wanted: set[str] = set()
    for d in daemons:
        for conn in d.peer_conns:
            if conn.peer_inode:
                wanted.add(conn.peer_inode)
    owners = find_socket_owners(wanted)

    # Фаза 3: склеиваем соединения с владельцами и считаем возраст.
    for d in daemons:
        d.peer_conns = _attach_peers(d, owners, prev.get(str(d.pid)) or {}, now)

    return daemons


def _daemon_connections(d: Daemon, tcp: dict[str, Any],
                        tcp_by_ports: dict[tuple[int, int], list[str]],
                        ) -> list[PeerConn]:
    """Established-соединения демона с локальными клиентами (без владельца)."""
    my_inodes = socket_inodes(d.pid)
    out: list[PeerConn] = []
    for ino in my_inodes:
        entry = tcp.get(ino)
        if entry is None:
            continue
        lport, rport, state, l_loop, r_loop = entry
        if state != "01":  # 01 = ESTABLISHED
            continue
        if not (l_loop or r_loop):
            # Исходящее соединение наружу (например, HTTPS к репозиторию):
            # клиента за ним нет, наблюдателем оно не является.
            continue
        peer_inode = None
        peers = tcp_by_ports.get((rport, lport), [])
        if peers:
            peer_inode = peers[0]
        out.append(PeerConn(inode=ino, local_port=lport, remote_port=rport,
                            peer_inode=peer_inode))
    return out


def _attach_peers(d: Daemon, owners: dict[str, int], rec: dict[str, Any],
                  now: float) -> list[PeerConn]:
    """Проставляет владельца, имя и возраст соединения.

    Возвращает только те соединения, которые действительно наблюдают
    за демоном: соединение с самим собой — не наблюдатель.
    """
    seen_at: dict[str, float] = rec.get("peer_first_seen") or {}
    fresh: dict[str, float] = {}
    kept: list[PeerConn] = []

    for conn in d.peer_conns:
        owner = owners.get(conn.peer_inode) if conn.peer_inode else None
        if owner == d.pid:
            # Соединение с самим собой (внутренний loopback) — не наблюдатель.
            continue
        conn.owner_pid = owner
        conn.owner_name = proc_comm(owner) if owner else None
        key = peer_key(conn)
        first = seen_at.get(key)
        if first is None:
            first = now
        fresh[key] = first
        conn.first_seen = first
        conn.age = now - first
        kept.append(conn)
        if owner is None:
            d.peer_unknown += 1
        else:
            d.peer_pids.append(owner)
    d.peer_first_seen = fresh
    return kept
