#!/usr/bin/env python3
"""Страховка от разъезда: симулятор и рейпер должны понимать простой одинаково.

Симулятор (`tools/simulate.py`) отвечает на вопрос «что бы мы убили, если бы
поколей убивали по истории», рейпер — «что убиваем сейчас». Модели у них
разные и это нормально: симулятор не знает про размер демона, клиентов и
CPU, поэтому его убийства — верхняя оценка.

Но ПРОСТОЙ они должны понимать одинаково. Иначе симулятор обещает не то,
что рейпер делает, и измерение порогов по нему становится фикцией.

Этот тест — мост между двумя реализациями: он строит одну и ту же ситуацию
(демон с известным последним закрытым простояем) с двух сторон и требует,
чтобы выводы совпали. Если кто-то изменит правило простоя в `reaper/policy.py`
или модель в симуляторе, тест упадёт — вместо того чтобы тихо разъехаться.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import _api as reaper  # noqa: E402


def _load_tool(name: str, filename: str):
    path = os.path.join(ROOT, "tools", filename)
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


simulate = _load_tool("reaper_simulate_drift", "simulate.py")

NOW = 1_800_000_000.0
HOUR = 3600.0


def make_daemon(pid=1000, kind="gradle", **kw):
    d = reaper.Daemon(
        pid=pid,
        kind=kind,
        starttime=kw.pop("starttime", 12345),
        cpu_ticks=kw.pop("cpu_ticks", 1000),
        rss_mb=kw.pop("rss_mb", 1024),
        swap_mb=kw.pop("swap_mb", 0),
    )
    for key, value in kw.items():
        setattr(d, key, value)
    return d


def baseline(daemon, first_seen):
    return {"daemons": {str(daemon.pid): {"first_seen": first_seen,
                                          "cpu_ticks": 1000,
                                          "last_cpu_at": NOW - 300}}}


class TestSimulatorMatchesRules(unittest.TestCase):
    """Модель симулятора и правила рейпера говорят об одном и том же."""

    def setUp(self):
        self.cfg = dict(reaper.DEFAULTS)
        # «Всегда убирать»: иначе решение упирается в давление памяти, а мы
        # проверяем именно простой.
        self.cfg["MEMORY_PRESSURE_MB"] = 0
        self.cfg["IDLE_SECONDS"] = 3600
        self.cfg["KOTLIN_IDLE_SECONDS"] = 3600
        self.threshold = float(self.cfg["IDLE_SECONDS"])

    # ------------------------------------------------------------------ #

    def _history_record(self, last_end, death, kind="gradle"):
        """Сводка демона: одна закрытая сборка, затем простой до смерти."""
        return {
            "kind": kind,
            "builds": [[last_end - 60.0, last_end]],
            "first_activity": last_end - 60.0,
            "last_activity": last_end,
            "mtime": last_end,
            "death_observed_at": death,
            "unclosed_builds": 0,
            "canceled_at": None,
        }

    def _reaper_decision(self, last_end, now, kind="gradle"):
        """Что рейпер решит в момент `now` про демон, замолчавшего в `last_end`."""
        d = make_daemon(pid=1000, kind=kind, last_build_end=last_end,
                        log_mtime=last_end)
        state = baseline(d, first_seen=last_end - 10 * HOUR)
        ctx = reaper.Context(now=now, mem_available_mb=32000)
        decisions = reaper.classify([d], state, now, self.cfg, context=ctx)
        self.assertEqual(len(decisions), 1)
        return decisions[0]

    def _simulator_kill(self, last_end, death, kind="gradle"):
        """Момент, когда модель симулятора убила бы демона."""
        record = self._history_record(last_end, death, kind)
        return simulate.kill_time(record, self.threshold,
                                  self.cfg["STUCK_AFTER_SECONDS"])

    def _simulator_killed_by(self, last_end, death, now, kind="gradle"):
        """Убил бы симулятор демона К МОМЕНТУ `now` (а не когда-нибудь потом).

        Симулятор отвечает на вопрос «когда рейпер убил бы демона за всю его
        жизнь», а рейпер сейчас отвечает на вопрос «убить ли его СЕЙЧАС».
        Поэтому сравнивать надо «убил бы к моменту now», иначе модель
        «убила бы когда-нибудь позже» будет выглядеть как противоречие.
        """
        kill_at = self._simulator_kill(last_end, death, kind)
        return kill_at is not None and kill_at <= now

    # ------------------------------------------------------------------ #

    def test_idle_longer_than_threshold_is_killed_by_both(self):
        """Простой длиннее порога: и рейпер, и симулятор убивают."""
        last_end = NOW - 2 * HOUR
        death = NOW + HOUR
        decision = self._reaper_decision(last_end, NOW)
        self.assertTrue(decision.kill,
                        f"рейпер не убил простаивающий демон: {decision.reason}")
        self.assertTrue(self._simulator_killed_by(last_end, death, NOW),
                        "симулятор не увидел убийства")
        kill_at = self._simulator_kill(last_end, death)
        self.assertAlmostEqual(kill_at, last_end + self.threshold, places=1)

    def test_idle_shorter_than_threshold_is_kept_by_both(self):
        """Простой короче порога: и рейпер, и симулятор оставляют."""
        last_end = NOW - 600
        death = NOW + HOUR
        decision = self._reaper_decision(last_end, NOW)
        self.assertFalse(decision.kill,
                         f"рейпер убил демона, простаивающего 10 мин: {decision.reason}")
        self.assertEqual(decision.rule, "not-idle")
        self.assertFalse(self._simulator_killed_by(last_end, death, NOW),
                         "симулятор увидел убийство там, где рейпер не убивает")

    def test_threshold_boundary_agrees(self):
        """Ровно на границе порога рейпер и симулятор ведут себя одинаково.

        Ре��пер убивает, когда простой ДОСТИГ порога («простой < порога ⇒
        оставить»). Модель симулятора обязана вести себя так же, иначе на
        границе они говорили бы разное — а граница это ровно то место, где
        ошибка в одном знаке меняет ответ.
        """
        last_end = NOW - self.threshold
        death = NOW + HOUR
        decision = self._reaper_decision(last_end, NOW)
        self.assertTrue(decision.kill,
                        "на границе порога рейпер должен убить: простой "
                        "достиг порога")
        kill_at = self._simulator_kill(last_end, death)
        self.assertIsNotNone(kill_at,
                             "симулятор не убивает на границе, рейпер — убивает")
        self.assertAlmostEqual(kill_at, last_end + self.threshold, places=1)

    def test_build_in_progress_counts_from_build_start(self):
        """Сборка без конца: простой считается от её начала — у обоих одинаково."""
        start = NOW - 2 * HOUR
        record = {
            "kind": "gradle",
            "builds": [[start, None]],
            "first_activity": start,
            "last_activity": start,
            "mtime": start,
            "death_observed_at": NOW + HOUR,
            "unclosed_builds": 1,
            "canceled_at": None,
        }
        kill_at = simulate.kill_time(record, self.threshold,
                                     self.cfg["STUCK_AFTER_SECONDS"])
        self.assertIsNotNone(kill_at, "симулятор не увидел убийства")
        self.assertAlmostEqual(kill_at, start + self.threshold, places=1)

        # build_in_progress — свойство: сборка начата и не закрыта.
        d = make_daemon(pid=1000, kind="gradle", last_build_start=start,
                        last_build_end=None, log_mtime=start)
        self.assertTrue(d.build_in_progress)
        state = baseline(d, first_seen=start - 10 * HOUR)
        ctx = reaper.Context(now=NOW, mem_available_mb=32000)
        decisions = reaper.classify([d], state, NOW, self.cfg, context=ctx)
        self.assertTrue(decisions[0].kill,
                        f"рейпер не убил зависшую сборку: {decisions[0].reason}")

    def test_simulator_is_explicitly_a_model(self):
        """Симулятор — МОДЕЛЬ, и это написано в его коде.

        Модель не знает про размер, клиентов и CPU, поэтому её убийства —
        верхняя оценка. Если это перестанет быть написано, кто-нибудь
        когда-нибудь примет модель за рейпер.
        """
        with open(os.path.join(ROOT, "tools", "simulate.py"),
                  encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("МОДЕЛЬ", source.upper())
        self.assertIn("policy.py", source)
