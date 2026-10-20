#!/usr/bin/env python3
"""Инвариантные тесты политики.

Обычные тесты проверяют отдельные ветки. Эти — свойства, которые должны
выполняться для ЛЮБОГО сочетания признаков:

  * если у демона есть хоть один признак защиты — его нельзя убить;
  * если демона убивают — значит выполнены все условия кандидата;
  * решение детерминировано;
  * «неизвестное ⇒ оставить» никогда не нарушается.

Набор признаков перебирается детерминированно (seeded random), чтобы
проверка была воспроизводимой.
"""

from __future__ import annotations

import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import _api as reaper  # noqa: E402

NOW = 1_800_000_000.0
HOUR = 3600.0


def make_daemon(pid=1000, kind="gradle", **kw):
    d = reaper.Daemon(
        pid=pid,
        kind=kind,
        starttime=kw.pop("starttime", 12345),
        cpu_ticks=kw.pop("cpu_ticks", 1000),
        rss_mb=kw.pop("rss_mb", 512),
        swap_mb=kw.pop("swap_mb", 0),
    )
    for key, value in kw.items():
        setattr(d, key, value)
    return d


def baseline(daemon, first_seen=NOW - 10 * HOUR, cpu_ticks=1000):
    return {"daemons": {str(daemon.pid): {"first_seen": first_seen,
                                          "cpu_ticks": cpu_ticks,
                                          "last_cpu_at": NOW - 300}}}


def random_case(rng: random.Random):
    """Случайный демон со случайными признаками."""
    kind = rng.choice(["gradle", "kotlin"])
    idle_hours = rng.choice([0.01, 0.5, 1.0, 2.0, 3.0, 5.0, 48.0])
    age = idle_hours * HOUR
    d = make_daemon(pid=rng.randint(1000, 9999), kind=kind,
                    rss_mb=rng.choice([10, 200, 4000]),
                    log_ok=rng.random() > 0.1,
                    idle_known=rng.random() > 0.1,
                    children=[5000] if rng.random() < 0.2 else [],
                    ignored_children=[5001] if rng.random() < 0.1 else [],
                    cpu_ticks=rng.choice([1000, 1000 + 500 * os.sysconf("SC_CLK_TCK")]),
                    peer_conns=([reaper.PeerConn(inode="1", local_port=1,
                                                 remote_port=2,
                                                 owner_pid=rng.choice([None, 424242]),
                                                 owner_name="client",
                                                 age=rng.choice([None, 10.0, 100000.0]))]
                                if rng.random() < 0.3 else []))
    if kind == "gradle":
        d.last_build_end = NOW - age
        d.last_build_start = NOW - age + 1 if rng.random() < 0.2 else None
    else:
        d.log_mtime = NOW - age
    state = baseline(d, first_seen=NOW - 100 * HOUR)
    if rng.random() < 0.1:
        state["daemons"][str(d.pid)].pop("cpu_ticks")  # нет baseline
    return d, state, age


class TestPolicyInvariants(unittest.TestCase):
    cfg = dict(reaper.DEFAULTS)

    def _decide(self, d, state):
        decisions = reaper.classify([d], state, NOW, self.cfg)
        self.assertEqual(len(decisions), 1)
        return decisions[0]

    def test_protected_never_killed(self):
        """Любой признак защиты ⇒ keep."""
        rng = random.Random(20261020)
        peer_max_age = float(self.cfg["PEER_MAX_AGE_SECONDS"])
        for _ in range(400):
            d, state, _ = random_case(rng)
            rec = state["daemons"][str(d.pid)]
            has_baseline = "cpu_ticks" in rec
            cpu_delta = ((d.cpu_ticks - int(rec["cpu_ticks"]))
                         / float(os.sysconf("SC_CLK_TCK"))
                         if has_baseline else None)
            fresh_peer = any(c.owner_pid is not None
                             and (c.age is None or c.age <= peer_max_age)
                             for c in d.peer_conns)
            protected = (not d.log_ok) or (not d.idle_known) or (not has_baseline) \
                or fresh_peer
            busy = bool(d.children) or (
                cpu_delta is not None
                and cpu_delta > float(self.cfg["BUSY_CPU_SECONDS"]))
            dec = self._decide(d, state)
            if protected or busy:
                self.assertFalse(dec.kill,
                                 f"демон с защитой/занятостью убит: {dec.reason}")

    def test_kill_requires_all_conditions(self):
        rng = random.Random(4242)
        checked = 0
        for _ in range(600):
            d, state, age = random_case(rng)
            dec = self._decide(d, state)
            if not dec.kill:
                continue
            checked += 1
            self.assertTrue(d.log_ok, "убит демон без лога")
            self.assertTrue(d.idle_known, "убит демон с неизвестной историей")
            self.assertIn("cpu_ticks", state["daemons"][str(d.pid)],
                          "убит демон без baseline")
            self.assertEqual(d.children, [], "убит демон с живыми детьми")
            self.assertGreaterEqual(d.footprint_mb, self.cfg["MIN_FOOTPRINT_MB"],
                                    "убит демон с footprint ниже минимума")
            # Порог простоя свой у каждого класса: у Kotlin он вдвое меньше
            # (у Kotlin нет своего idle-таймаута, его не убирает никто).
            limit = (self.cfg["KOTLIN_IDLE_SECONDS"] if d.kind == "kotlin"
                     else self.cfg["IDLE_SECONDS"])
            self.assertGreaterEqual(age, limit,
                                    "убит демон с простоя меньше порога")
            self.assertFalse(dec.busy, "убит демон, признанный занятым")
            self.assertFalse(dec.protected, "убит защищённый демон")
        self.assertGreater(checked, 0, "ни одного kill — тест не проверил ничего")

    def test_unknown_never_killed(self):
        """«Неизвестное ⇒ оставить»: нет лога, нет истории, нет baseline."""
        d = make_daemon(log_ok=False)
        dec = self._decide(d, baseline(d))
        self.assertFalse(dec.kill)

        d = make_daemon(idle_known=False, last_build_end=NOW - 10 * HOUR)
        dec = self._decide(d, baseline(d))
        self.assertFalse(dec.kill)

        d = make_daemon(last_build_end=NOW - 10 * HOUR)
        state = {"daemons": {str(d.pid): {"first_seen": NOW - 10 * HOUR}}}
        dec = self._decide(d, state)
        self.assertFalse(dec.kill)

    def test_determinism(self):
        rng = random.Random(777)
        for _ in range(100):
            d, state, _ = random_case(rng)
            first = self._decide(d, state)
            second = self._decide(d, state)
            self.assertEqual(first.action, second.action)
            self.assertEqual(first.reason, second.reason)

    def test_ignored_child_does_not_protect(self):
        d = make_daemon(last_build_end=NOW - 10 * HOUR,
                        children=[], ignored_children=[4242])
        dec = self._decide(d, baseline(d))
        self.assertTrue(dec.kill)

    def test_live_child_protects(self):
        d = make_daemon(last_build_end=NOW - 10 * HOUR, children=[4242])
        dec = self._decide(d, baseline(d))
        self.assertFalse(dec.kill)
        self.assertTrue(dec.busy)

    def test_stale_peer_stops_protecting(self):
        """Соединение старше PEER_MAX_AGE_SECONDS — зависший клиент."""
        d = make_daemon(last_build_end=NOW - 10 * HOUR,
                        peer_conns=[reaper.PeerConn(inode="1", local_port=1,
                                                    remote_port=2,
                                                    owner_pid=424242,
                                                    owner_name="ide",
                                                    age=7 * HOUR)])
        dec = self._decide(d, baseline(d))
        self.assertTrue(dec.kill, "зависший клиент не должен защищать")

    def test_fresh_peer_still_protects(self):
        d = make_daemon(last_build_end=NOW - 10 * HOUR,
                        peer_conns=[reaper.PeerConn(inode="1", local_port=1,
                                                    remote_port=2,
                                                    owner_pid=424242,
                                                    owner_name="ide",
                                                    age=60.0)])
        dec = self._decide(d, baseline(d))
        self.assertFalse(dec.kill, "свежий клиент должен защищать")

    def test_rule_table_has_catch_all(self):
        rules = reaper.default_policy(self.cfg)
        self.assertTrue(rules)
        self.assertEqual(rules[-1].name, "kill")
        self.assertTrue(rules[-1].check(reaper.Signals(
            daemon=make_daemon(), now=NOW, age=0.0, idle_limit=1.0,
            min_footprint=0)))
        # Имена правил уникальны — иначе таблицу невозможно читать.
        names = [r.name for r in rules]
        self.assertEqual(len(names), len(set(names)))


class TestExecutionLimits(unittest.TestCase):
    cfg = dict(reaper.DEFAULTS)
    cfg["MAX_KILLS_PER_SWEEP"] = 2
    cfg["GRACE_SECONDS"] = 0.2

    def test_limit_caps_kills(self):
        """За свип убивается не больше MAX_KILLS_PER_SWEEP демонов."""
        pid = os.getpid()
        st = reaper.read_proc_stat(pid)
        decisions = []
        for i in range(5):
            d = make_daemon(pid=pid, starttime=st["starttime"],
                            last_build_end=NOW - 10 * HOUR, rss_mb=100 * (i + 1))
            decisions.append(reaper.Decision(d, True, "тест"))
        report = reaper.execute(decisions, {}, NOW, self.cfg, dry_run=True)
        dry = [r for r in report if r["action"] == "dry-run"]
        skipped = [r for r in report if r["action"] == "skipped"]
        self.assertEqual(len(dry), 2)
        self.assertEqual(len(skipped), 3)
        self.assertTrue(all("лимит" in r["reason"] for r in skipped))
        # Сами процессы не тронуты (dry-run).
        self.assertTrue(reaper.still_same(pid, st["starttime"]))


class TestLogTrimming(unittest.TestCase):
    """Уборка разросшихся логов простаивающих демонов (опционально)."""

    def _decision(self, path, kind="gradle", idle=10 * HOUR):
        d = make_daemon(kind=kind, last_build_end=NOW - idle, log_path=path)
        return reaper.Decision(d, False, "тест", idle_seconds=idle)

    def test_disabled_by_default(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".out.log", delete=False) as fh:
            fh.write("x" * 2_000_000)
            path = fh.name
        try:
            cfg = dict(reaper.DEFAULTS)
            report = reaper.trim_idle_logs([self._decision(path)], cfg, NOW)
            self.assertEqual(report, [])
            self.assertEqual(os.path.getsize(path), 2_000_000)
        finally:
            os.unlink(path)

    def test_trims_idle_gradle_log(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".out.log", delete=False) as fh:
            fh.write("x" * 2_000_000)
            path = fh.name
        try:
            cfg = dict(reaper.DEFAULTS)
            cfg["TRIM_IDLE_LOGS"] = 1
            report = reaper.trim_idle_logs([self._decision(path)], cfg, NOW)
            self.assertEqual(len(report), 1)
            self.assertEqual(report[0]["action"], "trim")
            self.assertEqual(os.path.getsize(path), 0)
        finally:
            os.unlink(path)

    def test_does_not_trim_kotlin_log(self):
        """У Kotlin mtime лога — признак простоя, трогать его нельзя."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("x" * 2_000_000)
            path = fh.name
        try:
            cfg = dict(reaper.DEFAULTS)
            cfg["TRIM_IDLE_LOGS"] = 1
            dec = self._decision(path, kind="kotlin")
            report = reaper.trim_idle_logs([dec], cfg, NOW)
            self.assertEqual(report, [])
            self.assertEqual(os.path.getsize(path), 2_000_000)
        finally:
            os.unlink(path)

    def test_does_not_trim_busy_log(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".out.log", delete=False) as fh:
            fh.write("x" * 2_000_000)
            path = fh.name
        try:
            cfg = dict(reaper.DEFAULTS)
            cfg["TRIM_IDLE_LOGS"] = 1
            report = reaper.trim_idle_logs(
                [self._decision(path, idle=60)], cfg, NOW)
            self.assertEqual(report, [])
            self.assertEqual(os.path.getsize(path), 2_000_000)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
