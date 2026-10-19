"""Низкоуровневый доступ к /proc: процессы, память, сокеты, дети."""

from __future__ import annotations

import os
import re
from datetime import datetime

from .model import CHILD_IGNORE_EXECUTABLES, GRADLE_MAIN, KOTLIN_MAIN

TS_RE = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[.,]\d+(?:[+-]\d{2}:?\d{2}|Z)?)")


def parse_ts(token: str) -> float | None:
    """Разбор ISO-таймстампа лога Gradle в epoch.

    Смещение часов из лога учитывается: «2026-10-04T12:00:00.000+0300» — это
    09:00 UTC. Иначе сравнение с локальным временем сломается при смене
    таймзоны (и логи разных демонов могут быть с разными смещениями).
    """
    m = TS_RE.match(token.strip())
    if not m:
        return None
    iso = m.group(1).replace(",", ".")
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def read_proc_stat(pid: int) -> dict[str, object] | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        fields = raw[raw.rindex(")") + 2:].split()
    except ValueError:
        return None
    if len(fields) < 22:
        return None
    return {
        "state": fields[0],
        "ppid": int(fields[1]),
        "utime": int(fields[11]),
        "stime": int(fields[12]),
        "starttime": int(fields[19]),
    }


def read_memory(pid: int) -> tuple[int, int]:
    """(VmRSS, VmSwap) в КБ. zram-счёт важен: часть страниц вытеснена в swap."""
    rss = swap = 0
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                elif line.startswith("VmSwap:"):
                    swap = int(line.split()[1])
    except (OSError, ValueError):
        pass
    return rss, swap


def proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().decode("utf-8", "replace").replace("\0", " ").strip()
    except OSError:
        return ""


def proc_comm(pid: int) -> str:
    """Короткое имя процесса — для отчёта «кто защищает»."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        cmd = proc_cmdline(pid)
        return os.path.basename(cmd.split(" ", 1)[0]) if cmd else ""


def daemon_kind(cmdline: str) -> str | None:
    if GRADLE_MAIN in cmdline:
        return "gradle"
    if KOTLIN_MAIN in cmdline:
        return "kotlin"
    return None


def find_log_path(pid: int) -> str | None:
    """Путь к логу демона — точно, через открытые дескрипторы.

    Gradle держит свой daemon-<pid>.out.log на stdout, Kotlin — свой
    kotlin-daemon.*.log. Никаких эвристик по времени старта.
    """
    fddir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fddir)
    except OSError:
        return None
    for fd in fds:
        try:
            target = os.readlink(os.path.join(fddir, fd))
        except OSError:
            continue
        if not target.endswith(".log"):
            continue
        base = os.path.basename(target)
        if base.startswith("daemon-") and base.endswith(".out.log"):
            return target
        if base.startswith("kotlin-daemon."):
            return target
    return None


def is_ignored_child(cmdline: str) -> bool:
    """Считать ли ребёнка «шумом», не защищающим демона.

    Сравниваем только имя исполняемого файла. Поиск подстроки по всему
    cmdline давал ложные срабатывания: classpath форка приложения содержал
    «playwright-1.49.0.jar», и работающий процесс помечался как шум.
    """
    parts = cmdline.split()
    if not parts:
        return False
    return os.path.basename(parts[0]) in CHILD_IGNORE_EXECUTABLES


def children_map() -> dict[int, list[int]]:
    """pid → дети (зомби пропускаем: они не работают)."""
    result: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        st = read_proc_stat(int(entry))
        if st is None or st["state"] == "Z":
            continue
        result.setdefault(int(st["ppid"]), []).append(int(entry))
    return result


def socket_inodes(pid: int) -> set[str]:
    inodes: set[str] = set()
    fddir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fddir)
    except OSError:
        return inodes
    for fd in fds:
        try:
            target = os.readlink(os.path.join(fddir, fd))
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    return inodes


def inode_owner_map() -> dict[str, int]:
    """inode → pid для всех процессов текущего пользователя (один проход)."""
    owners: dict[str, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        fddir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fddir, fd))
            except OSError:
                continue
            if target.startswith("socket:["):
                owners.setdefault(target[8:-1], pid)
    return owners


def is_loopback(addr_hex: str) -> bool:
    """Адрес из /proc/net/tcp — это u32/128-бит в порядке байтов хоста."""
    a = addr_hex.upper()
    if len(a) == 8:
        return a == "0100007F"          # 127.0.0.1
    if len(a) == 32:
        return a in ("00000000000000000000000001000000",  # ::1
                     "00000000000000000000000000000000")  # ::
    return False


def tcp_table() -> dict[str, tuple[int, int, str, bool, bool]]:
    """inode → (local_port, remote_port, state, local_loopback, remote_loopback)."""
    table: dict[str, tuple[int, int, str, bool, bool]] = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) < 10:
                continue
            state = f[3]
            try:
                lhex, lport_hex = f[1].rsplit(":", 1)
                rhex, rport_hex = f[2].rsplit(":", 1)
                lport = int(lport_hex, 16)
                rport = int(rport_hex, 16)
            except (IndexError, ValueError):
                continue
            table[f[9]] = (lport, rport, state, is_loopback(lhex), is_loopback(rhex))
    return table
