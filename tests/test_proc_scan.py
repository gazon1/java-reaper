#!/usr/bin/env python3
"""Тесты прицельного поиска владельца сокета (п.13).

Раньше рейпер строил полную карту inode → pid по всем процессам системы
(0.37 с на 600 процессах), чтобы узнать владельца двух-трёх peer-соединений.
Теперь ищется только нужное — с ранним выходом.

Тесты поднимают настоящие TCP-соединения через loopback: только так
проверяется связка «inode сокета → pid владельца».
"""

from __future__ import annotations

import os
import socket
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import _api as reaper  # noqa: E402


def socket_inode_of(sock: socket.socket) -> str:
    """inode конкретного сокета — через его собственный fd."""
    target = os.readlink(f"/proc/self/fd/{sock.fileno()}")
    if target.startswith("socket:["):
        return target[8:-1]
    return ""


class TestFindSocketOwners(unittest.TestCase):
    def test_empty_request_returns_empty(self):
        self.assertEqual(reaper.find_socket_owners(set()), {})

    def test_finds_owner_of_our_own_socket(self):
        """Сокет текущего процесса: владелец — мы."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            cli.connect(srv.getsockname())
            conn, _ = srv.accept()
            try:
                inode = socket_inode_of(cli)
                self.assertTrue(inode, "не нашли inode сокета")
                owners = reaper.find_socket_owners({inode})
                self.assertIn(inode, owners)
                self.assertEqual(owners[inode], os.getpid())
            finally:
                conn.close()
                cli.close()
        finally:
            srv.close()

    def test_finds_several_owners_at_once(self):
        """Несколько нужных inode находятся за один проход."""
        socks = []
        inodes = set()
        try:
            for _ in range(3):
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.bind(("127.0.0.1", 0))
                srv.listen(1)
                cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                cli.connect(srv.getsockname())
                conn, _ = srv.accept()
                socks += [srv, cli, conn]
                inodes.add(socket_inode_of(cli))
            self.assertEqual(len(inodes), 3)
            owners = reaper.find_socket_owners(inodes)
            for ino in inodes:
                self.assertEqual(owners.get(ino), os.getpid())
        finally:
            for s in socks:
                s.close()

    def test_unknown_inode_is_simply_missing(self):
        """Несуществующий inode не ломает поиск: просто не находится."""
        owners = reaper.find_socket_owners({"999999999"})
        self.assertNotIn("999999999", owners)

    def test_search_is_faster_than_full_map(self):
        """Прицельный поиск не должен быть медленнее полного обхода.

        Полный обход читает fd каждого процесса; прицельный идёт по тем же
        каталогам, но с ранним выходом, когда нужное найдено.
        """
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            cli.connect(srv.getsockname())
            conn, _ = srv.accept()
            try:
                inode = socket_inode_of(cli)

                def targeted():
                    reaper.find_socket_owners({inode})

                def full():
                    reaper.inode_owner_map()

                t0 = time.perf_counter()
                targeted()
                targeted_time = time.perf_counter() - t0

                t0 = time.perf_counter()
                full()
                full_time = time.perf_counter() - t0

                self.assertLessEqual(targeted_time, full_time * 1.2,
                                     f"прицельный {targeted_time:.3f} с, "
                                     f"полный {full_time:.3f} с")
            finally:
                conn.close()
                cli.close()
        finally:
            srv.close()

    def test_does_not_raise_on_unreadable_processes(self):
        """Процессы, чьи fd не читаются (чужие uid), не ломают обход."""
        owners = reaper.find_socket_owners({"1"})
        self.assertIsInstance(owners, dict)


if __name__ == "__main__":
    unittest.main()
