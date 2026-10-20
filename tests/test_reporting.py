#!/usr/bin/env python3
"""Тесты отчётности, состояния и разбора настоящего лога Gradle.

Здесь проверяется то, что появилось в раунде «пунктов 1–13»:

  * разбор настоящего лога Gradle (golden-фикстура) — формат не поедет молча;
  * версия схемы JSON и её контракт;
  * версия состояния и миграция (вычистка мёртвых полей);
  * структурированный лог убийств (JSONL);
  * счётчик «зависшего клиента»;
  * round-trip возраста peer-соединений;
  * health/audit/pilot/explain на всех ветках.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import _api as reaper  # noqa: E402
from reaper import report  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "gradle-daemon.log")
NOW = 1_800_000_000.0
HOUR = 3600.0


def _verdict_line(text: str) -> str:
    """Строка «ВЕРДИКТ: …» — отдельно от остального отчёта."""
    for line in text.splitlines():
        if line.startswith("ВЕРДИКТ:"):
            return line
    return ""


def make_daemon(pid=1000, kind="gradle", **kw):
    d = reaper.Daemon(pid=pid, kind=kind,
                      starttime=kw.pop("starttime", 12345),
                      cpu_ticks=kw.pop("cpu_ticks", 1000),
                      rss_mb=kw.pop("rss_mb", 512),
                      swap_mb=kw.pop("swap_mb", 0))
    for key, value in kw.items():
        setattr(d, key, value)
    return d


def make_decision(pid=1000, **kw):
    d = make_daemon(pid=pid, **kw)
    return reaper.Decision(d, kw.pop("kill", False), kw.pop("reason", "тест"),
                           idle_seconds=kw.pop("idle_seconds", 5 * HOUR))


# --------------------------------------------------------------------------- #
# П.5: настоящий лог Gradle
# --------------------------------------------------------------------------- #

class TestGoldenGradleLog(unittest.TestCase):
    """Фикстура — срез настоящего лога демона Gradle 9.7.1.

    Тест падает, если изменится формат строки таймстемпа или текст маркеров:
    тогда рейпер молча перестанет видеть простой.
    """

    def setUp(self):
        if not os.path.exists(FIXTURE):
            self.skipTest("фикстура не найдена")
        with open(FIXTURE, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_fixture_is_not_empty(self):
        self.assertGreater(len(self.text), 1000)

    def test_fixture_contains_real_markers(self):
        self.assertIn(reaper.MARK_BUILD_START, self.text)
        self.assertIn(reaper.MARK_BUILD_END, self.text)

    def test_markers_are_found_and_ordered(self):
        start, end = reaper.scan_markers(self.text)
        self.assertIsNotNone(start, "маркер начала сборки не найден")
        self.assertIsNotNone(end, "маркер конца сборки не найден")
        self.assertLess(start, end, "конец сборки раньше начала")

    def test_timestamps_parse_to_epoch(self):
        """Смещение часов из лога учитывается: метка = epoch, а не локальное время."""
        start, end = reaper.scan_markers(self.text)
        self.assertGreater(start, 1_600_000_000)
        self.assertLess(start, 2_000_000_000)
        self.assertGreater(end, start)

    def test_scan_is_incremental(self):
        """Тот же лог, но со смещением: маркеры в хвосте всё равно находятся."""
        size = os.path.getsize(FIXTURE)
        head = max(0, size - 40_000)
        res = reaper.scan_gradle_log(FIXTURE, head, 512 * 1024 * 1024)
        self.assertIsNotNone(res["start"])
        self.assertIsNotNone(res["end"])
        self.assertTrue(res["complete"])

    def test_full_scan_offsets_to_end(self):
        res = reaper.scan_gradle_log(FIXTURE, 0, 512 * 1024 * 1024)
        self.assertEqual(res["offset"], os.path.getsize(FIXTURE))
        self.assertTrue(res["complete"])

    def test_debug_spam_does_not_hide_markers(self):
        """Между маркерами тысячи строк DEBUG-спама — маркеры теряться не должны."""
        self.assertGreater(self.text.count("Waiting to acquire shared lock"), 5)
        start, end = reaper.scan_markers(self.text)
        self.assertIsNotNone(start)
        self.assertIsNotNone(end)


# --------------------------------------------------------------------------- #
# П.6: версия схемы JSON
# --------------------------------------------------------------------------- #

class TestJsonSchema(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(reaper.DEFAULTS)

    def test_schema_version_present(self):
        payload = json.loads(reaper.format_json([], {"metric": {}}, self.cfg))
        self.assertEqual(payload["schema_version"], reaper.JSON_SCHEMA_VERSION)

    def test_schema_version_is_positive_int(self):
        self.assertIsInstance(reaper.JSON_SCHEMA_VERSION, int)
        self.assertGreater(reaper.JSON_SCHEMA_VERSION, 0)

    def test_json_contains_contract_fields(self):
        """Поля, на которые завязан потребитель, обязаны присутствовать всегда."""
        payload = json.loads(reaper.format_json([], {"metric": {}}, self.cfg))
        for key in ("daemons", "metric", "freed_mb", "would_free_mb",
                    "last_sweep_at", "pilot_started_at", "stale_peer_events",
                    "config", "now"):
            self.assertIn(key, payload, f"в JSON нет поля {key}")

    def test_json_is_valid_and_parsable(self):
        text = reaper.format_json([], {"metric": {}}, self.cfg)
        json.loads(text)  # не должно бросать

    def test_decision_dict_exposes_stale_peers(self):
        d = make_daemon()
        dec = reaper.Decision(d, False, "x", stale_peers=[
            reaper.Protector("peer-stale", "висит 7 ч", pid=42, name="idea")])
        payload = reaper.decision_to_dict(dec)
        self.assertEqual(len(payload["stale_peers"]), 1)
        self.assertEqual(payload["stale_peers"][0]["kind"], "peer-stale")


# --------------------------------------------------------------------------- #
# П.7: версия состояния и миграция
# --------------------------------------------------------------------------- #

class TestStateMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(reaper.DEFAULTS)
        self.cfg["STATE_DIR"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, data):
        os.makedirs(self.tmp.name, exist_ok=True)
        with open(reaper.state_path(self.cfg), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_old_state_is_migrated(self):
        self._write({"version": 1, "daemons": {"1": {"cpu_ticks": 5,
                                                     "last_sweep_at": 111}}})
        state = reaper.load_state(self.cfg)
        self.assertEqual(state["version"], reaper.STATE_VERSION)

    def test_migration_drops_dead_fields(self):
        """Поле last_sweep_at в записи демона — мёртвое, его быть не должно."""
        self._write({"version": 1, "daemons": {"1": {"cpu_ticks": 5,
                                                     "last_sweep_at": 111,
                                                     "неизвестное": "мусор"}}})
        state = reaper.load_state(self.cfg)
        rec = state["daemons"]["1"]
        self.assertNotIn("last_sweep_at", rec)
        self.assertNotIn("неизвестное", rec)
        self.assertEqual(rec["cpu_ticks"], 5)

    def test_migration_keeps_known_fields(self):
        keep = {k: 1 for k in reaper.DAEMON_FIELDS}
        self._write({"version": 1, "daemons": {"1": dict(keep, extra="x")}})
        state = reaper.load_state(self.cfg)
        for key in reaper.DAEMON_FIELDS:
            self.assertIn(key, state["daemons"]["1"], f"миграция вычистила {key}")

    def test_migration_keeps_global_fields(self):
        keep = {k: 1 for k in reaper.GLOBAL_FIELDS if k != "daemons"}
        self._write(dict(keep, version=1, daemons={}, мусор="x"))
        state = reaper.load_state(self.cfg)
        for key in reaper.GLOBAL_FIELDS:
            if key == "daemons":
                continue
            self.assertIn(key, state, f"миграция вычистила {key}")

    def test_state_without_version_is_migrated(self):
        self._write({"daemons": {"1": {"cpu_ticks": 7}}})
        state = reaper.load_state(self.cfg)
        self.assertEqual(state["version"], reaper.STATE_VERSION)
        self.assertEqual(state["daemons"]["1"]["cpu_ticks"], 7)

    def test_migrated_state_has_new_defaults(self):
        self._write({"version": 1, "daemons": {}})
        state = reaper.load_state(self.cfg)
        self.assertEqual(state["stale_peer_events"], 0)
        self.assertEqual(state["stale_peer_daemons"], {})
        self.assertEqual(state["pilot_started_at"], 0.0)


# --------------------------------------------------------------------------- #
# П.8: round-trip состояния
# --------------------------------------------------------------------------- #

class TestStateRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(reaper.DEFAULTS)
        self.cfg["STATE_DIR"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_peer_first_seen_survives_round_trip(self):
        """Возраст peer-соединения переживает перезапуск рейпера.

        Иначе «зависший клиент» никогда не достигнет 6 ч: после каждого
        перезапуска возраст обнулялся бы.
        """
        seen = {"1234:5000:5001": NOW - 7 * HOUR}
        state = {"version": reaper.STATE_VERSION,
                 "daemons": {"1000": {"peer_first_seen": seen,
                                      "first_seen": NOW - 10 * HOUR,
                                      "cpu_ticks": 1, "last_cpu_at": NOW}}}
        reaper.save_state(self.cfg, state)
        loaded = reaper.load_state(self.cfg)
        self.assertEqual(loaded["daemons"]["1000"]["peer_first_seen"], seen)

    def test_peer_first_seen_keeps_float_precision(self):
        seen = {"a:b:c": 1_700_000_000.123456}
        reaper.save_state(self.cfg, {"version": reaper.STATE_VERSION,
                                      "daemons": {"1": {"peer_first_seen": seen}}})
        loaded = reaper.load_state(self.cfg)
        self.assertAlmostEqual(
            loaded["daemons"]["1"]["peer_first_seen"]["a:b:c"],
            seen["a:b:c"], places=4)

    def test_stale_peer_counter_survives_round_trip(self):
        reaper.save_state(self.cfg, {"version": reaper.STATE_VERSION,
                                      "daemons": {},
                                      "stale_peer_events": 4,
                                      "stale_peer_daemons": {"1000": 2}})
        loaded = reaper.load_state(self.cfg)
        self.assertEqual(loaded["stale_peer_events"], 4)
        self.assertEqual(loaded["stale_peer_daemons"]["1000"], 2)


# --------------------------------------------------------------------------- #
# П.3: счётчик «зависшего клиента»
# --------------------------------------------------------------------------- #

class TestStalePeerCounter(unittest.TestCase):
    def test_counts_only_decisions_with_stale_peers(self):
        stale = reaper.Decision(make_daemon(pid=1), False, "x", stale_peers=[
            reaper.Protector("peer-stale", "висит 7 ч")])
        clean = reaper.Decision(make_daemon(pid=2), False, "y")
        state = {}
        touched = reaper.note_stale_peers(state, [stale, clean])
        self.assertEqual(touched, 1)
        self.assertEqual(state["stale_peer_events"], 1)
        self.assertEqual(state["stale_peer_daemons"]["1"], 1)

    def test_accumulates_across_sweeps(self):
        stale = reaper.Decision(make_daemon(pid=1), False, "x", stale_peers=[
            reaper.Protector("peer-stale", "висит 7 ч")])
        state = {}
        reaper.note_stale_peers(state, [stale])
        reaper.note_stale_peers(state, [stale])
        self.assertEqual(state["stale_peer_events"], 2)
        self.assertEqual(state["stale_peer_daemons"]["1"], 2)

    def test_no_stale_peers_leaves_counter_at_zero(self):
        state = {}
        self.assertEqual(reaper.note_stale_peers(state, [make_decision()]), 0)
        self.assertEqual(state.get("stale_peer_events", 0), 0)


# --------------------------------------------------------------------------- #
# П.9: структурированный лог убийств
# --------------------------------------------------------------------------- #

class TestKillLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(reaper.DEFAULTS)
        self.cfg["STATE_DIR"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_kills_file_created_on_first_kill(self):
        self.assertFalse(os.path.exists(reaper.kills_path(self.cfg)))
        reaper.append_kill(self.cfg, {"pid": 1, "action": "SIGTERM",
                                      "reason": "простой", "footprint_mb": 100})
        self.assertTrue(os.path.exists(reaper.kills_path(self.cfg)))

    def test_record_is_valid_jsonl(self):
        reaper.append_kill(self.cfg, {"pid": 1, "action": "SIGTERM",
                                      "reason": "простой", "footprint_mb": 100})
        reaper.append_kill(self.cfg, {"pid": 2, "action": "SIGKILL",
                                      "reason": "не ушёл", "footprint_mb": 200})
        with open(reaper.kills_path(self.cfg), encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)  # каждая строка — отдельный JSON

    def test_record_has_timestamp(self):
        reaper.append_kill(self.cfg, {"pid": 1, "action": "SIGTERM"})
        rows = reaper.read_kills(self.cfg)
        self.assertEqual(len(rows), 1)
        self.assertIn("at", rows[0])
        self.assertGreater(rows[0]["at"], 0)

    def test_read_kills_respects_limit(self):
        for i in range(10):
            reaper.append_kill(self.cfg, {"pid": i, "action": "SIGTERM"})
        self.assertEqual(len(reaper.read_kills(self.cfg, limit=3)), 3)

    def test_read_kills_on_missing_file(self):
        self.assertEqual(reaper.read_kills(self.cfg), [])

    def test_broken_line_does_not_break_reading(self):
        reaper.append_kill(self.cfg, {"pid": 1, "action": "SIGTERM"})
        with open(reaper.kills_path(self.cfg), "a", encoding="utf-8") as fh:
            fh.write("{это не json\n")
        reaper.append_kill(self.cfg, {"pid": 2, "action": "SIGKILL"})
        rows = reaper.read_kills(self.cfg)
        self.assertEqual(len(rows), 2)

    def test_pilot_report_mentions_kills(self):
        reaper.append_kill(self.cfg, {"pid": 1, "action": "SIGTERM",
                                      "footprint_mb": 512})
        state = {"metric": {"events": 20, "reused": 2, "rate": 0.1},
                 "pilot_started_at": NOW - 3 * 86400,
                 "freed_mb": 512, "would_free_mb": 100}
        text, code = reaper.pilot_report(self.cfg, state, now=NOW)
        self.assertIn("записей об убийствах", text)
        self.assertIn("512", text)
        self.assertEqual(code, 0)


# --------------------------------------------------------------------------- #
# П.1: вердикт по пилоту
# --------------------------------------------------------------------------- #

class TestPilotReport(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(reaper.DEFAULTS)

    def _state(self, events, rate, started=NOW - 86400):
        reused = round(events * rate)
        return {"metric": {"events": events, "reused": reused,
                           "rate": rate, "pending": 0},
                "pilot_started_at": started}

    def test_too_few_events_no_verdict(self):
        text, code = reaper.pilot_report(self.cfg, self._state(3, 0.0), now=NOW)
        self.assertEqual(code, 2)
        self.assertIn("ДАННЫХ МАЛО", text)

    def test_no_events_says_pilot_has_nothing_to_collect(self):
        """Ноль событий — не «мало данных», а «данных не будет».

        Если kill-правила не сработали ни разу, кандидатов не было, и пилоту
        не из чего набирать выборку: ждать её надо не временем, а появлением
        зависшего демона. Вердикт обязан это сказать прямо.
        """
        state = {"metric": {"events": 0, "reused": 0, "rate": 0.0,
                            "pending": 0, "by_kind": {}},
                 "pilot_started_at": NOW - 86400,
                 "rule_hits": {"not-idle": 10}}
        text, code = reaper.pilot_report(self.cfg, state, now=NOW)
        self.assertEqual(code, 2)
        verdict_line = _verdict_line(text)
        self.assertIn("не из чего набирать события", verdict_line)
        self.assertIn("Kill-режим включать рано", verdict_line)

    def test_no_events_reason_dropped_when_events_exist(self):
        """При набранных событиях причина «не из чего набирать» неуместна."""
        state = self._state(20, 0.1)
        state["rule_hits"] = {"kill": 20}
        text, _ = reaper.pilot_report(self.cfg, state, now=NOW)
        self.assertNotIn("не из чего набирать события", _verdict_line(text))

    def test_no_sweeps_yet_says_nothing_was_observed(self):
        """Свипов не было — это «наблюдать было нечего», а не «порог не проверен»."""
        text = reaper.format_status([], {"metric": {}, "rule_hits": {}},
                                    reaper.DEFAULTS)
        self.assertIn("свипов ещё не было", text)
        self.assertNotIn("НИ РАЗУ не сработали", text)

    def test_high_reuse_rate_blocks(self):
        text, code = reaper.pilot_report(self.cfg, self._state(20, 0.9), now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("НЕ ВКЛЮЧАТЬ", text)

    def test_low_reuse_rate_allows(self):
        text, code = reaper.pilot_report(self.cfg, self._state(20, 0.3), now=NOW)
        self.assertEqual(code, 0)
        self.assertIn("МОЖНО ВКЛЮЧАТЬ", text)

    def test_exactly_at_threshold_allows(self):
        _, code = reaper.pilot_report(self.cfg,
                                      self._state(20, reaper.PILOT_MAX_REUSE_RATE),
                                      now=NOW)
        self.assertEqual(code, 0)

    # ---- вердикт по видам ----

    def _state_by_kind(self, by_kind, started=NOW - 86400):
        """Состояние с новой метрикой: разбивка по видам."""
        counted = sum(d["counted"] for d in by_kind.values())
        reused = sum(d["reused"] for d in by_kind.values())
        return {"metric": {"events": counted, "counted": counted,
                           "reused": reused, "pending": 0, "by_kind": by_kind},
                "pilot_started_at": started}

    def test_verdict_is_per_kind(self):
        """Один плохой вид блокирует выкатку, даже если общая цифра хорошая."""
        state = self._state_by_kind({
            "gradle": {"counted": 20, "reused": 2, "rate": 0.1,
                       "rate_weighted": 0.1, "cost_reused_seconds": 80.0},
            "esbuild": {"counted": 10, "reused": 9, "rate": 0.9,
                        "rate_weighted": 0.9, "cost_reused_seconds": 18.0},
        })
        text, code = reaper.pilot_report(self.cfg, state, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("esbuild", text)

    def test_kind_with_few_events_does_not_block(self):
        """Вид с малой выборкой своего вердикта не получает и не блокирует."""
        state = self._state_by_kind({
            "gradle": {"counted": 20, "reused": 2, "rate": 0.1,
                       "rate_weighted": 0.1, "cost_reused_seconds": 80.0},
            "esbuild": {"counted": 2, "reused": 2, "rate": 1.0,
                        "rate_weighted": 1.0, "cost_reused_seconds": 4.0},
        })
        text, code = reaper.pilot_report(self.cfg, state, now=NOW)
        self.assertEqual(code, 0)
        self.assertIn("мало данных", text)

    def test_no_kind_with_enough_events_is_no_verdict(self):
        state = self._state_by_kind({
            "esbuild": {"counted": 3, "reused": 1, "rate": 0.33,
                        "rate_weighted": 0.33, "cost_reused_seconds": 2.0},
        })
        _, code = reaper.pilot_report(self.cfg, state, now=NOW)
        self.assertEqual(code, 2)

    def test_report_shows_weighted_rate_and_cost(self):
        state = self._state_by_kind({
            "gradle": {"counted": 20, "reused": 4, "rate": 0.2,
                       "rate_weighted": 0.35, "cost_reused_seconds": 160.0},
        })
        text, _ = reaper.pilot_report(self.cfg, state, now=NOW)
        self.assertIn("взвешенно", text)
        self.assertIn("160", text)

    def test_boundary_min_events(self):
        """Ровно PILOT_MIN_EVENTS событий — вердикт выносится."""
        _, code = reaper.pilot_report(self.cfg,
                                      self._state(reaper.PILOT_MIN_EVENTS, 0.0),
                                      now=NOW)
        self.assertEqual(code, 0)

    def test_report_shows_duration(self):
        text, _ = reaper.pilot_report(self.cfg, self._state(20, 0.1), now=NOW)
        self.assertIn("сут", text)

    def test_report_without_start_marker(self):
        text, code = reaper.pilot_report(self.cfg, {"metric": {}}, now=NOW)
        self.assertEqual(code, 2)
        self.assertIn("неизвестно", text)


# --------------------------------------------------------------------------- #
# П.2: аудит
# --------------------------------------------------------------------------- #

class TestAuditReport(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(reaper.DEFAULTS)

    def test_no_stale_daemons_passes(self):
        dec = make_decision(idle_seconds=HOUR)
        text, code = reaper.audit_report(self.cfg, [dec], {})
        self.assertEqual(code, 0)
        self.assertIn("критерий приёмки выполнен", text)

    def test_stale_daemon_fails_and_is_listed(self):
        dec = make_decision(idle_seconds=10 * HOUR)
        text, code = reaper.audit_report(self.cfg, [dec], {})
        self.assertEqual(code, 1)
        self.assertIn("1000", text)
        self.assertIn("НЕ выполнен", text)

    def test_unknown_idle_is_not_stale(self):
        dec = reaper.Decision(make_daemon(), False, "x", idle_seconds=None)
        _, code = reaper.audit_report(self.cfg, [dec], {})
        self.assertEqual(code, 0)

    def test_audit_counts_total_footprint(self):
        d1 = make_decision(idle_seconds=10 * HOUR)
        d2 = make_decision(pid=2000, idle_seconds=20 * HOUR)
        d2.daemon.rss_mb = 1000
        text, code = reaper.audit_report(self.cfg, [d1, d2], {})
        self.assertEqual(code, 1)
        self.assertIn("2 демонов", text)


# --------------------------------------------------------------------------- #
# П.12: --explain
# --------------------------------------------------------------------------- #

class TestExplain(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(reaper.DEFAULTS)

    def test_every_rule_has_doc_and_condition(self):
        for rule in reaper.default_policy(self.cfg):
            self.assertTrue(rule.doc.strip(), f"у правила {rule.name} нет описания")
            self.assertTrue(rule.condition.strip(),
                            f"у правила {rule.name} нет условия")

    def test_rule_names_unique(self):
        names = [r.name for r in reaper.default_policy(self.cfg)]
        self.assertEqual(len(names), len(set(names)))

    def test_explain_lists_all_rules(self):
        text = reaper.format_explain(self.cfg)
        for rule in reaper.default_policy(self.cfg):
            self.assertIn(rule.name, text)
            self.assertIn(rule.doc, text)

    def test_explain_shows_thresholds(self):
        text = reaper.format_explain(self.cfg)
        self.assertIn("Порог простоя", text)
        self.assertIn("180", text)  # 3 ч в минутах


# --------------------------------------------------------------------------- #
# Общие помощники отчётов: демоны по видам, порог простоя, сводка
# --------------------------------------------------------------------------- #

class TestReportHelpers(unittest.TestCase):
    """Помощники, на которые переписаны отчёты.

    Если они разойдутся между собой, разойдутся и отчёты: таблица, JSON
    и аудит обязаны показывать одни и те же числа.
    """

    def setUp(self):
        self.cfg = dict(reaper.DEFAULTS)
        self.targets = reaper.builtin_targets()

    def test_daemons_grouped_by_kind(self):
        g = reaper.Decision(make_daemon(pid=1, kind="gradle"), True, "простой")
        k = reaper.Decision(make_daemon(pid=2, kind="kotlin"), False, "ждёт")
        grouped = report.daemons_by_kind([g, k], self.targets)
        self.assertEqual([d.daemon.pid for d in grouped["gradle"]], [1])
        self.assertEqual([d.daemon.pid for d in grouped["kotlin"]], [2])

    def test_unknown_kind_is_not_in_summary(self):
        other = reaper.Decision(make_daemon(pid=3, kind="неизвестный"), False, "x")
        grouped = report.daemons_by_kind([other], self.targets)
        self.assertNotIn("неизвестный", grouped)
        self.assertEqual(sum(len(v) for v in grouped.values()), 0)

    def test_idle_limits_match_kind_thresholds(self):
        limits = report.idle_limits(self.cfg, self.targets)
        for kind in self.targets.names():
            self.assertEqual(limits[kind],
                             report.idle_limit_for_kind(self.cfg, kind, self.targets))

    def test_kind_aggregates_equal_kind_summary(self):
        g = reaper.Decision(make_daemon(pid=1, kind="gradle"), True, "простой")
        k = reaper.Decision(make_daemon(pid=2, kind="kotlin"), False, "ждёт")
        decisions = [g, k]
        self.assertEqual(report.kind_aggregates(decisions, self.cfg, self.targets),
                         reaper.kind_summary(decisions, self.cfg, self.targets))

    def test_kind_label_falls_back_to_name(self):
        self.assertEqual(report.kind_label({}, "gradle"), "gradle")
        self.assertEqual(report.kind_label({"gradle": "Gradle"}, "gradle"), "Gradle")


# --------------------------------------------------------------------------- #
# health на всех ветках
# --------------------------------------------------------------------------- #

class TestHealthBranches(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(reaper.DEFAULTS)

    def test_never_swept_is_problem(self):
        _, code = reaper.health(self.cfg, {})
        self.assertEqual(code, 1)

    def test_fresh_sweep_is_ok(self):
        import time as _t
        _, code = reaper.health(self.cfg, {"last_sweep_at": _t.time()})
        self.assertEqual(code, 0)

    def test_stale_sweep_is_problem(self):
        import time as _t
        old = _t.time() - 10 * self.cfg["SWEEP_SECONDS"]
        text, code = reaper.health(self.cfg, {"last_sweep_at": old})
        self.assertEqual(code, 1)
        self.assertIn("таймер молчит", text)

    def test_health_mentions_freed_memory(self):
        import time as _t
        text, _ = reaper.health(self.cfg, {"last_sweep_at": _t.time(),
                                           "freed_mb": 1024})
        self.assertIn("1024", text)


class TestPathPrivacy(unittest.TestCase):
    """В машинном выводе домашнего пути пользователя быть не должно.

    Путь к логу нужен (видно, где лежит), но домашний каталог раскрывает имя
    пользователя и раскладку его рабочих каталогов — в публичном отчёте этому
    не место.
    """

    def test_home_prefix_is_cut(self):
        home = os.path.expanduser("~")
        self.assertEqual(reaper.redact_home(f"{home}/.gradle/x.log"),
                         "~/.gradle/x.log")

    def test_path_outside_home_is_untouched(self):
        self.assertEqual(reaper.redact_home("/tmp/kotlin-daemon.log"),
                         "/tmp/kotlin-daemon.log")

    def test_empty_path_stays_empty(self):
        self.assertIsNone(reaper.redact_home(None))
        self.assertEqual(reaper.redact_home(""), "")

    def test_json_daemon_record_has_no_home_path(self):
        cfg = dict(reaper.DEFAULTS)
        home = os.path.expanduser("~")
        d = reaper.Daemon(pid=1, kind="gradle", rss_mb=100, swap_mb=0,
                          cpu_ticks=1, starttime=1,
                          log_path=f"{home}/.gradle/daemon/9.7.1/daemon-1.out.log")
        dec = reaper.Decision(d, False, "тест")
        payload = json.loads(reaper.format_json([dec], {"metric": {}}, cfg))
        record = payload["daemons"][0]
        self.assertTrue(record["log_path"].startswith("~/"),
                        f"домашний путь утёк: {record['log_path']}")
        self.assertNotIn(home, record["log_path"])


if __name__ == "__main__":
    unittest.main()
