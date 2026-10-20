#!/usr/bin/env python3
"""Тесты общего модуля корпуса: чтение журналов и меры простоя.

Журналы читают три инструмента (симулятор, прибор измерения, месячный
отчёт), и все они должны получать одно и то же из одного и того же файла.
Иначе цифры разъедутся, и сравнивать их будет нельзя.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import _api as reaper  # noqa: E402
from reaper import corpus  # noqa: E402


class TestReadJsonl(unittest.TestCase):
    """Чтение журнала: битая строка не должна ронять разбор."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "journal.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, lines):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_reads_records(self):
        self._write(['{"a": 1}', '{"b": 2}'])
        rows = corpus.read_jsonl(self.path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["b"], 2)

    def test_broken_line_is_skipped(self):
        self._write(['{"a": 1}', "{not json", "", '{"b": 2}'])
        rows = corpus.read_jsonl(self.path)
        self.assertEqual(len(rows), 2)

    def test_missing_file_is_empty(self):
        self.assertEqual(corpus.read_jsonl(os.path.join(self.tmp.name, "нет")),
                         [])


class TestPercentile(unittest.TestCase):
    def test_middle(self):
        self.assertAlmostEqual(corpus.percentile([1, 2, 3], 50), 2.0)
        self.assertAlmostEqual(corpus.percentile([1, 2, 3, 4], 50), 2.5)

    def test_empty(self):
        self.assertEqual(corpus.percentile([], 50), 0.0)


class TestIdleMeasures(unittest.TestCase):
    """Меры простоя: между сборками и хвостовой."""

    def test_gap_between_builds_sums_intervals(self):
        record = {"builds": [[0.0, 60.0], [3600.0, 3660.0]]}
        self.assertAlmostEqual(corpus.gaps_between_builds(record), 3540.0)

    def test_single_build_has_no_gap(self):
        """Одна сборка — между сборками простоев нет (ноль, а не «не измерили»)."""
        self.assertEqual(corpus.gaps_between_builds({"builds": [[0.0, 60.0]]}), 0.0)

    def test_no_builds_is_not_measured(self):
        self.assertIsNone(corpus.gaps_between_builds({}))

    def test_unclosed_build_is_not_a_gap(self):
        record = {"builds": [[0.0, None], [3600.0, 3660.0]]}
        self.assertEqual(corpus.gaps_between_builds(record), 0.0)

    def test_tail_idle_needs_death(self):
        self.assertIsNone(corpus.tail_idle({"last_activity": 100.0}))

    def test_tail_idle_counts_from_last_activity(self):
        record = {"last_activity": 100.0, "death_observed_at": 400.0}
        self.assertAlmostEqual(corpus.tail_idle(record), 300.0)

    def test_tail_bounds_use_last_seen_when_known(self):
        """Смерть известна с точностью до свипа: нижняя граница — last_seen."""
        record = {"last_activity": 100.0, "death_observed_at": 400.0,
                  "last_seen_at": 200.0}
        bounds = corpus.tail_idle_bounds(record)
        self.assertIsNotNone(bounds)
        lower, upper = bounds
        self.assertAlmostEqual(lower, 100.0)   # 200 - 100
        self.assertAlmostEqual(upper, 300.0)   # 400 - 100

    def test_tail_bounds_fall_back_to_upper(self):
        record = {"last_activity": 100.0, "death_observed_at": 400.0}
        lower, upper = corpus.tail_idle_bounds(record)
        self.assertAlmostEqual(lower, 300.0)
        self.assertAlmostEqual(upper, 300.0)


class TestRuleCoverage(unittest.TestCase):
    """Покрытие правил: какие пороги проверены, а какие нет."""

    def test_counts_hits_and_never_fired(self):
        sweeps = [
            {"rules": {"not-idle": 2, "kill": 0}},
            {"rules": {"not-idle": 1, "kill": 0}},
        ]
        coverage = corpus.rule_coverage(sweeps)
        self.assertEqual(coverage["fired"]["not-idle"], 3)
        self.assertIn("kill", coverage["never_fired"])
        self.assertNotIn("not-idle", coverage["never_fired"])

    def test_empty_journal(self):
        coverage = corpus.rule_coverage([])
        self.assertEqual(coverage["sweeps"], 0)
        self.assertEqual(coverage["fired"], {})


class TestStuckDaemons(unittest.TestCase):
    """«Зависшие» — единственный класс, который программа сама не убирает."""

    def test_stuck_means_cancel_during_build(self):
        self.assertTrue(corpus.is_stuck({"canceled_at": 100.0}))
        self.assertFalse(corpus.is_stuck({"canceled_at": None}))

    def test_lifetime_from_mtime_when_death_not_observed(self):
        record = {"canceled_at": 100.0, "mtime": 400.0}
        measured = corpus.stuck_lifetime(record)
        self.assertIsNotNone(measured)
        seconds, source = measured
        self.assertAlmostEqual(seconds, 300.0)
        self.assertEqual(source, "mtime")

    def test_lifetime_uses_last_seen_as_lower_bound(self):
        """Рейпер видел демона живым — значит, он дожил до этого момента."""
        record = {"canceled_at": 100.0, "mtime": 400.0,
                  "death_observed_at": 900.0, "last_seen_at": 300.0}
        measured = corpus.stuck_lifetime(record)
        self.assertIsNotNone(measured)
        seconds, source = measured
        self.assertAlmostEqual(seconds, 200.0)
        self.assertEqual(source, "last_seen")

    def test_no_cancel_no_lifetime(self):
        self.assertIsNone(corpus.stuck_lifetime({"mtime": 400.0}))

    def test_summaries_group_by_kind(self):
        records = [
            {"kind": "gradle", "canceled_at": 100.0, "mtime": 400.0},
            {"kind": "gradle", "canceled_at": None, "mtime": 500.0},
            {"kind": "kotlin", "canceled_at": 100.0, "mtime": 200.0},
        ]
        summary = corpus.stuck_summaries(records)
        self.assertEqual(summary["count_by_kind"], {"gradle": 1, "kotlin": 1})
        self.assertEqual(len(summary["lifetimes_by_kind"]["gradle"]), 1)


if __name__ == "__main__":
    unittest.main()
