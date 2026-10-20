"""Цели (targets.yaml): разбор, сопоставление, разрешение порогов.

Отдельный файл тестов: цели — это конфигурация, и её ошибки (битый YAML,
неизвестное поле, конфликт с плоским конфигом) обязаны быть видны, а не
молча менять поведение рейпера.
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import _api as reaper  # noqa: E402
from reaper import targets as tg  # noqa: E402


YAML_FULL = """
version: 1
targets:
  - name: gradle
    label: Gradle
    match_cmdline: "org.gradle.launcher.daemon.bootstrap.GradleDaemon"
    log_name_regex: '^daemon-\\d+\\.out\\.log$'
    activity: markers
    build_start: "Received command: Build"
    build_end: "Marking the daemon as idle"
    cancel: "client disconnection detected"
    idle_seconds: 21600
    recreate_cost_seconds: 40
    self_timeout_seconds: 10800
    protects: [kotlin]
    rebuildable: true
  - name: kotlin
    label: Kotlin
    match_cmdline: "org.jetbrains.kotlin.daemon.KotlinCompileDaemon"
    log_name_regex: '^kotlin-daemon\\..*\\.log$'
    activity: mtime
    idle_seconds: 7200
    recreate_cost_seconds: 5
    rebuildable: true
"""


class TestBuiltinTargets(unittest.TestCase):
    """Встроенные цели — то, что работало до появления файла целей."""

    def test_builtin_kinds(self):
        targets = tg.builtin_targets()
        self.assertEqual(sorted(targets.names()), ["gradle", "kotlin"])

    def test_gradle_protects_kotlin(self):
        targets = tg.builtin_targets()
        self.assertEqual(targets.protectors_of("kotlin"), ["gradle"])
        self.assertEqual(targets.protectors_of("gradle"), [])

    def test_match_by_cmdline(self):
        targets = tg.builtin_targets()
        spec = targets.match("java -cp x org.gradle.launcher.daemon.bootstrap.GradleDaemon")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "gradle")
        spec = targets.match("java org.jetbrains.kotlin.daemon.KotlinCompileDaemon")
        self.assertEqual(spec.name, "kotlin")

    def test_unknown_process_is_not_matched(self):
        self.assertIsNone(tg.builtin_targets().match("java -jar app.jar"))

    def test_builtin_activity_models(self):
        targets = tg.builtin_targets()
        self.assertEqual(targets.get("gradle").activity, "markers")
        self.assertEqual(targets.get("kotlin").activity, "mtime")


class TestYamlLoading(unittest.TestCase):
    """Разбор targets.yaml."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "targets.yaml")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_file_gives_builtin_targets(self):
        warnings = []
        targets = tg.load_targets(os.path.join(self.tmp.name, "нет.yaml"),
                                  warnings)
        self.assertEqual(sorted(targets.names()), ["gradle", "kotlin"])
        self.assertEqual(warnings, [])

    def test_full_file_parsed(self):
        self._write(YAML_FULL)
        warnings = []
        targets = tg.load_targets(self.path, warnings)
        self.assertEqual(warnings, [])
        self.assertEqual(sorted(targets.names()), ["gradle", "kotlin"])
        gradle = targets.get("gradle")
        self.assertEqual(gradle.idle_seconds, 21600)
        self.assertEqual(gradle.recreate_cost_seconds, 40)
        self.assertEqual(gradle.self_timeout_seconds, 10800)
        self.assertEqual(gradle.protects, ("kotlin",))
        self.assertTrue(gradle.rebuildable)
        self.assertEqual(targets.get("kotlin").activity, "mtime")

    def test_new_target_added(self):
        self._write(YAML_FULL + """
  - name: esbuild
    label: esbuild
    match_cmdline: "esbuild"
    log_name_regex: '\\.log$'
    activity: mtime
    idle_seconds: 3600
    recreate_cost_seconds: 2
    rebuildable: true
""")
        targets = tg.load_targets(self.path, [])
        self.assertIn("esbuild", targets)
        self.assertEqual(targets.get("esbuild").idle_seconds, 3600)
        self.assertEqual(targets.get("esbuild").label, "esbuild")

    def test_broken_yaml_falls_back_to_builtin(self):
        self._write("targets: [это: не список")
        warnings = []
        targets = tg.load_targets(self.path, warnings)
        self.assertEqual(sorted(targets.names()), ["gradle", "kotlin"])
        self.assertTrue(warnings, "битый YAML должен давать предупреждение")

    def test_unknown_activity_warns(self):
        self._write("""
targets:
  - name: weird
    match_cmdline: "weird"
    activity: "непонятно"
""")
        warnings = []
        targets = tg.load_targets(self.path, warnings)
        self.assertEqual(targets.get("weird").activity, "mtime")
        self.assertTrue(any("activity" in w for w in warnings))

    def test_target_without_match_warns(self):
        self._write("""
targets:
  - name: пустой
    label: Пустой
""")
        warnings = []
        tg.load_targets(self.path, warnings)
        self.assertTrue(any("match_cmdline" in w for w in warnings))

    def test_bad_regex_warns_and_drops(self):
        self._write("""
targets:
  - name: broken
    match_cmdline: "broken"
    log_name_regex: "([unclosed"
""")
        warnings = []
        targets = tg.load_targets(self.path, warnings)
        self.assertEqual(targets.get("broken").log_name_regex, "")
        self.assertTrue(any("регулярка" in w for w in warnings))


class TestThresholdResolution(unittest.TestCase):
    """Кто главнее: YAML или плоский ключ конфига."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(reaper.DEFAULTS)
        self.warnings = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_yaml_value_used_when_flat_key_absent(self):
        cfg = dict(self.cfg)
        targets = tg.TargetSet({"x": tg.TargetSpec(name="x", idle_seconds=1234)})
        resolved = reaper.resolve_targets(targets, cfg, set(), self.warnings)
        self.assertEqual(resolved.get("x").idle_seconds, 1234)

    def test_flat_key_used_for_kind_without_yaml_value(self):
        cfg = dict(self.cfg)
        targets = tg.TargetSet({"kotlin": tg.TargetSpec(name="kotlin")})
        resolved = reaper.resolve_targets(targets, cfg, {"KOTLIN_IDLE_SECONDS"},
                                          self.warnings)
        self.assertEqual(resolved.get("kotlin").idle_seconds, 7200)
        self.assertEqual(self.warnings, [])

    def test_per_kind_flat_key_wins_over_yaml(self):
        """Видовой ключ конфига (KOTLIN_IDLE_SECONDS) — намерение оператора."""
        cfg = dict(self.cfg)
        cfg["KOTLIN_IDLE_SECONDS"] = 3600
        targets = tg.TargetSet({"kotlin": tg.TargetSpec(name="kotlin",
                                                        idle_seconds=7200)})
        resolved = reaper.resolve_targets(targets, cfg, {"KOTLIN_IDLE_SECONDS"},
                                          self.warnings)
        self.assertEqual(resolved.get("kotlin").idle_seconds, 3600)

    def test_yaml_is_more_specific_than_general_key(self):
        """Общий ключ конфига — менее специфичен, чем значение для вида."""
        cfg = dict(self.cfg)
        cfg["IDLE_SECONDS"] = 3600
        targets = tg.TargetSet({"gradle": tg.TargetSpec(name="gradle",
                                                        idle_seconds=21600)})
        resolved = reaper.resolve_targets(targets, cfg, {"IDLE_SECONDS"},
                                          self.warnings)
        self.assertEqual(resolved.get("gradle").idle_seconds, 21600)
        self.assertTrue(any("IDLE_SECONDS" in w for w in self.warnings))

    def test_mismatch_warns(self):
        """Значения расходятся — молчать об этом нельзя."""
        cfg = dict(self.cfg)
        cfg["IDLE_SECONDS"] = 3600
        targets = tg.TargetSet({"gradle": tg.TargetSpec(name="gradle",
                                                        idle_seconds=21600)})
        reaper.resolve_targets(targets, cfg, {"IDLE_SECONDS"}, self.warnings)
        self.assertTrue(any("IDLE_SECONDS" in w for w in self.warnings))

    def test_builtin_values_unchanged(self):
        """Главное: значения встроенных целей не поехали."""
        targets = reaper.resolve_targets(reaper.builtin_targets(),
                                         dict(self.cfg), set(), [])
        gradle = targets.get("gradle")
        kotlin = targets.get("kotlin")
        self.assertEqual(gradle.idle_seconds, 21600)
        self.assertEqual(gradle.recreate_cost_seconds, 40)
        self.assertEqual(gradle.build_active_seconds, 1800)
        self.assertEqual(gradle.stuck_after_seconds, 3600)
        self.assertEqual(kotlin.idle_seconds, 7200)
        self.assertEqual(kotlin.recreate_cost_seconds, 5)

    def test_new_target_gets_its_own_thresholds(self):
        """У новой цели — свои пороги из YAML."""
        targets = tg.TargetSet({
            "esbuild": tg.TargetSpec(name="esbuild", idle_seconds=3600,
                                     recreate_cost_seconds=2,
                                     min_footprint_mb=64,
                                     large_daemon_mb=2048),
        })
        resolved = reaper.resolve_targets(targets, dict(self.cfg), set(),
                                          self.warnings)
        spec = resolved.get("esbuild")
        self.assertEqual(spec.idle_seconds, 3600)
        self.assertEqual(spec.recreate_cost_seconds, 2)
        self.assertEqual(spec.min_footprint_mb, 64)
        self.assertEqual(spec.large_daemon_mb, 2048)

    def test_agreeing_values_do_not_warn(self):
        cfg = dict(self.cfg)
        targets = tg.TargetSet({"gradle": tg.TargetSpec(name="gradle",
                                                        idle_seconds=21600)})
        reaper.resolve_targets(targets, cfg, {"IDLE_SECONDS"}, self.warnings)
        self.assertEqual(self.warnings, [])

    def test_new_kind_gets_generic_cost_default(self):
        targets = tg.TargetSet({"esbuild": tg.TargetSpec(name="esbuild")})
        resolved = reaper.resolve_targets(targets, dict(self.cfg), set(),
                                          self.warnings)
        self.assertEqual(resolved.get("esbuild").recreate_cost_seconds,
                         reaper.DEFAULTS["RECREATE_COST_SECONDS"])


class TestMarkersFromSpec(unittest.TestCase):
    """Маркеры лога берутся из спецификации цели, а не из кода."""

    def test_markers_from_spec(self):
        spec = tg.TargetSpec(name="x", build_start="НАЧАЛО",
                              build_end="КОНЕЦ", cancel="ОТМЕНА")
        markers = reaper.Markers.from_spec(spec)
        self.assertEqual(markers.build_start, "НАЧАЛО".encode())
        self.assertEqual(markers.build_end, "КОНЕЦ".encode())
        self.assertEqual(markers.cancel, "ОТМЕНА".encode())

    def test_empty_markers_not_usable(self):
        self.assertFalse(reaper.Markers().usable)

    def test_custom_markers_are_parsed(self):
        """Чужой формат лога разбирается теми же маркерами."""
        spec = tg.TargetSpec(name="x", build_start="НАЧАЛО", build_end="КОНЕЦ")
        markers = reaper.Markers.from_spec(spec)
        lines = ["2026-10-20T08:00:00.000+0300 INFO НАЧАЛО работы",
                 "2026-10-20T08:10:00.000+0300 INFO КОНЕЦ работы"]
        lines = [line.encode("utf-8") for line in lines]
        scan = reaper.scan_lines(lines, markers=markers)
        self.assertEqual(len(scan.builds), 1)
        self.assertIsNotNone(scan.builds[0].end)

    def test_gradle_markers_are_default(self):
        """По умолчанию — маркеры Gradle: ничего не изменилось."""
        scan = reaper.scan_lines([b"2026-10-20T08:00:00.000+0300 Received command: Build"])
        self.assertEqual(len(scan.builds), 1)


class TestBehaviorUnchanged(unittest.TestCase):
    """Регрессия: поведение Gradle/Kotlin не изменилось.

    Ключевое требование рефакторинга — решение по тем же демонам осталось
    тем же. Проверяем на наборе демонов с разными признаками.
    """

    def setUp(self):
        self.cfg = dict(reaper.DEFAULTS)
        self.cfg["MEMORY_PRESSURE_MB"] = 0  # детерминизм: память не влияет
        self.now = 1_800_000_000.0
        self.hour = 3600.0

    def _daemons(self):
        state = {"daemons": {}}
        out = []
        # Gradle: простаивает 10 ч (порог 6 ч) → кандидат
        g = reaper.Daemon(pid=1, kind="gradle", rss_mb=1000, swap_mb=0,
                          cpu_ticks=100, starttime=1,
                          last_build_end=self.now - 10 * self.hour)
        # Kotlin: простаивает 5 ч (порог 2 ч) → кандидат
        k = reaper.Daemon(pid=2, kind="kotlin", rss_mb=800, swap_mb=0,
                          cpu_ticks=100, starttime=1,
                          log_mtime=self.now - 5 * self.hour)
        # Kotlin: только что работал → не кандидат
        k2 = reaper.Daemon(pid=3, kind="kotlin", rss_mb=800, swap_mb=0,
                           cpu_ticks=100, starttime=1,
                           log_mtime=self.now - 60)
        for d in (g, k, k2):
            state["daemons"][str(d.pid)] = {
                "first_seen": self.now - 20 * self.hour,
                "cpu_ticks": 100,
            }
            out.append(d)
        return out, state

    def _decisions(self, targets=None):
        daemons, state = self._daemons()
        return {d.daemon.pid: d
                for d in reaper.classify(daemons, state, self.now, self.cfg,
                                         targets=targets)}

    def test_builtin_targets_decisions(self):
        decisions = self._decisions()
        self.assertTrue(decisions[1].kill, "простаивающий Gradle — кандидат")
        self.assertTrue(decisions[2].kill, "простаивающий Kotlin — кандидат")
        self.assertFalse(decisions[3].kill, "работающий Kotlin — не кандидат")
        self.assertEqual(decisions[1].recreate_cost_seconds, 40)
        self.assertEqual(decisions[2].recreate_cost_seconds, 5)

    def test_yaml_targets_decide_exactly_the_same(self):
        """С файлом целей и без него решения совпадают до слова."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "targets.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(YAML_FULL)
            targets = reaper.resolve_targets(
                reaper.load_targets(path, []), self.cfg, set(), [])
        builtin = self._decisions()
        from_yaml = self._decisions(targets)
        self.assertEqual(sorted(builtin), sorted(from_yaml))
        for pid in builtin:
            self.assertEqual(builtin[pid].action, from_yaml[pid].action)
            self.assertEqual(builtin[pid].reason, from_yaml[pid].reason)
            self.assertEqual(builtin[pid].rule, from_yaml[pid].rule)

    def test_protection_still_works(self):
        """Пока идёт свежая сборка Gradle, Kotlin защищён."""
        g = reaper.Daemon(pid=1, kind="gradle", rss_mb=1000, swap_mb=0,
                          cpu_ticks=100, starttime=1,
                          last_build_start=self.now - 60, last_build_end=None)
        k = reaper.Daemon(pid=2, kind="kotlin", rss_mb=800, swap_mb=0,
                          cpu_ticks=100, starttime=1,
                          log_mtime=self.now - 5 * self.hour)
        state = {"daemons": {"1": {"first_seen": self.now - 20 * self.hour,
                                   "cpu_ticks": 100},
                             "2": {"first_seen": self.now - 20 * self.hour,
                                   "cpu_ticks": 100}}}
        decisions = {d.daemon.pid: d for d in
                     reaper.classify([g, k], state, self.now, self.cfg)}
        self.assertFalse(decisions[2].kill)
        self.assertEqual(decisions[2].rule, "protected-by-build")
        self.assertIn("Gradle", decisions[2].reason)


class TestLogLookup(unittest.TestCase):
    """Лог ищется по регулярке цели — и только по ней.

    Дефект, который закрывает этот тест: цель без своей регулярки
    получала ЧУЖОЙ лог (Gradle-овский) по старой эвристике, и простой
    этой цели измерялся бы по чужому файлу.
    """

    def test_empty_regex_means_no_log(self):
        self.assertIsNone(reaper.find_log_path(os.getpid(), ""))

    def test_regex_limits_the_search(self):
        """Регулярка цели — единственный источник лога."""
        path = reaper.find_log_path(os.getpid(), r"^нет-такого-файла\.log$")
        self.assertIsNone(path)

    def test_none_keeps_legacy_mode(self):
        """Без регулярки — старый режим (Gradle/Kotlin-имена)."""
        # Текущий процесс не держит логов Gradle/Kotlin → None.
        self.assertIsNone(reaper.find_log_path(os.getpid(), None))


class TestSafetyPromises(unittest.TestCase):
    """Обещания, которые код обязан выполнять, а не только объявлять.

    Каждое из них — место, где легко оставить поле «для красоты»: тогда
    оператор напишет в конфиге «не убивать» и ничего не изменится.
    """

    def setUp(self):
        self.cfg = dict(reaper.DEFAULTS)
        self.cfg["MEMORY_PRESSURE_MB"] = 0
        self.now = 1_800_000_000.0
        self.hour = 3600.0

    def _targets(self, **kwargs):
        kwargs.setdefault("rebuildable", True)
        return tg.TargetSet({"gradle": tg.TargetSpec(name="gradle", **kwargs)})

    def _daemon(self, kind="gradle"):
        return reaper.Daemon(pid=1, kind=kind, rss_mb=1000, swap_mb=0,
                             cpu_ticks=100, starttime=1,
                             last_build_end=self.now - 10 * self.hour)

    def _decide(self, targets):
        state = {"daemons": {"1": {"first_seen": self.now - 20 * self.hour,
                                   "cpu_ticks": 100}}}
        return reaper.classify([self._daemon()], state, self.now, self.cfg,
                               targets=targets)[0]

    def test_not_rebuildable_is_never_killed(self):
        """Цель без rebuildable — не кэш: убивать её нельзя никогда."""
        targets = self._targets(rebuildable=False)
        dec = self._decide(targets)
        self.assertFalse(dec.kill)
        self.assertEqual(dec.rule, "not-rebuildable")

    def test_rebuildable_can_be_killed(self):
        dec = self._decide(self._targets())
        self.assertTrue(dec.kill)

    def test_unknown_kind_is_not_killed(self):
        """Вида нет в наборе целей — «не знаем ⇒ не трогаем»."""
        dec = self._decide(tg.builtin_targets())
        self.assertTrue(dec.kill, "встроенные цели пересоздаваемы")

    def test_rule_is_visible_in_explain(self):
        text = reaper.format_explain(self.cfg, self._targets())
        self.assertIn("not-rebuildable", text)

    def test_sweep_record_lists_all_kinds(self):
        """Виды в журнале свипов — те, что встретились, а не два имени."""
        d1 = self._daemon("gradle")
        d2 = reaper.Daemon(pid=2, kind="esbuild", rss_mb=500, swap_mb=0,
                           cpu_ticks=100, starttime=1)
        record = reaper.sweep_record(
            [reaper.Decision(d1, True, "простой"),
             reaper.Decision(d2, True, "простой")],
            self.now, self.cfg)
        self.assertIn("esbuild", record["by_kind"])
        self.assertIn("gradle", record["by_kind"])
        self.assertEqual(record["by_kind"]["esbuild"]["count"], 1)

    def test_log_trim_is_a_property_of_target(self):
        """Усекать лог можно только там, где это не врёт про простой."""
        targets = tg.TargetSet({
            "gradle": tg.TargetSpec(name="gradle", log_trim=True),
            "kotlin": tg.TargetSpec(name="kotlin", log_trim=False),
        })
        with tempfile.TemporaryDirectory() as tmp:
            for kind, expected in (("gradle", True), ("kotlin", False)):
                path = os.path.join(tmp, f"{kind}.log")
                with open(path, "wb") as fh:
                    fh.write(b"x" * (2 * 1024 * 1024))
                d = reaper.Daemon(pid=1, kind=kind, rss_mb=1000, swap_mb=0,
                                  cpu_ticks=100, starttime=1, log_path=path)
                dec = reaper.Decision(d, True, "простой")
                dec.idle_seconds = 10 * self.hour
                report = reaper.trim_idle_logs(
                    [dec], dict(self.cfg, TRIM_IDLE_LOGS=1), self.now, targets)
                self.assertEqual(bool(report), expected,
                                 f"вид {kind}: усечение "
                                 f"{'должно' if expected else 'не должно'} "
                                 f"происходить")


class TestKindSignatures(unittest.TestCase):
    """Смена правил одной цели не должна обнулять метрику по остальным.

    Метрика пилота измеряет долю ложных срабатываний для конкретных правил.
    Правила изменились у одной цели — события по ней измеряли другое, и их
    надо выбросить; по остальным видам события остаются честными.
    """

    def _targets(self, mock_idle: float = 3600.0):
        return tg.TargetSet({
            "gradle": tg.TargetSpec(name="gradle", idle_seconds=21600.0,
                                    rebuildable=True),
            "mock": tg.TargetSpec(name="mock", idle_seconds=mock_idle,
                                  rebuildable=True),
        })

    def _state(self):
        return {
            "would_kill": [
                {"pid": 1, "kind": "gradle", "at": 100.0, "final": True,
                 "outcome": "reused"},
                {"pid": 2, "kind": "mock", "at": 100.0, "final": True,
                 "outcome": "reused"},
            ],
            "kind_signatures": {},
        }

    def test_first_run_records_signatures_without_dropping_events(self):
        state = self._state()
        changed = reaper.sync_kind_signatures(state, self._targets(), dict(reaper.DEFAULTS))
        self.assertEqual(changed, [])
        self.assertEqual(len(state["would_kill"]), 2)
        self.assertIn("gradle", state["kind_signatures"])
        self.assertIn("mock", state["kind_signatures"])

    def test_change_of_one_target_drops_only_its_events(self):
        state = self._state()
        cfg = dict(reaper.DEFAULTS)
        reaper.sync_kind_signatures(state, self._targets(), cfg)
        # Порог простоя у «mock» подняли — его события измеряли другое.
        changed = reaper.sync_kind_signatures(state, self._targets(mock_idle=7200.0), cfg)
        self.assertEqual(changed, ["mock"])
        kinds = [e["kind"] for e in state["would_kill"]]
        self.assertEqual(kinds, ["gradle"],
                         "события по остальным видам должны сохраниться")

    def test_global_change_drops_events_of_all_kinds(self):
        """Общий ключ конфига действует на все виды — сбрасываются все."""
        state = self._state()
        reaper.sync_kind_signatures(state, self._targets(), dict(reaper.DEFAULTS))
        cfg = dict(reaper.DEFAULTS, MEMORY_PRESSURE_MB=1024)
        changed = reaper.sync_kind_signatures(state, self._targets(), cfg)
        self.assertEqual(sorted(changed), ["gradle", "mock"])
        self.assertEqual(state["would_kill"], [])

    def test_unchanged_rules_keep_events(self):
        state = self._state()
        cfg = dict(reaper.DEFAULTS)
        reaper.sync_kind_signatures(state, self._targets(), cfg)
        changed = reaper.sync_kind_signatures(state, self._targets(), cfg)
        self.assertEqual(changed, [])
        self.assertEqual(len(state["would_kill"]), 2)


class TestOverrideInheritsBuiltin(unittest.TestCase):
    """Переопределение встроенной цели меняет только написанное.

    Иначе частичное описание цели молча ломает остальное: цель «kotlin»
    без ts_format получила бы iso вместо local (и перестала бы понимать
    свои таймстемпы), а цель без log_name_regex — «своего лога нет», и
    демон навсегда попадал бы под защиту no-log.
    """

    def _yaml(self, body: str) -> str:
        return "targets:\n" + body

    def test_unspecified_fields_are_inherited(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "targets.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._yaml(
                    "  - name: kotlin\n"
                    "    label: Kotlin (мой)\n"))
            targets = tg.load_targets(path)
        spec = targets.get("kotlin")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.label, "Kotlin (мой)")
        # Не написано — значит осталось как было у встроенной цели.
        self.assertEqual(spec.ts_format, tg.TS_LOCAL)
        self.assertEqual(spec.log_name_regex, r"^kotlin-daemon\..*\.log$")
        self.assertTrue(spec.rebuildable)

    def test_explicit_value_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "targets.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._yaml(
                    "  - name: kotlin\n"
                    "    ts_format: iso\n"
                    "    idle_seconds: 3600\n"))
            targets = tg.load_targets(path)
        spec = targets.get("kotlin")
        self.assertEqual(spec.ts_format, tg.TS_ISO)
        self.assertEqual(spec.idle_seconds, 3600)

    def test_explicit_empty_log_regex_means_no_own_log(self):
        """Пустая регулярка — это «своего лога нет», а не «наследовать»."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "targets.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._yaml(
                    "  - name: gradle\n"
                    "    log_name_regex: ''\n"))
            targets = tg.load_targets(path)
        self.assertEqual(targets.get("gradle").log_name_regex, "")

    def test_new_target_does_not_inherit_anything(self):
        """Новая цель — дефолты, и rebuildable по умолчанию False."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "targets.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._yaml(
                    "  - name: brandnew\n"
                    "    match_cmdline: 'brandnew'\n"))
            targets = tg.load_targets(path)
        spec = targets.get("brandnew")
        self.assertFalse(spec.rebuildable)
        self.assertEqual(spec.log_name_regex, "")
        self.assertEqual(spec.ts_format, tg.TS_ISO)


class TestModuleStructure(unittest.TestCase):
    """Цели разнесены по модулям — но публичный контракт не изменился.

    Проверяем не «как именно разделено», а две вещи, которые важны
    посторонним: `reaper.targets` остаётся точкой входа с теми же именами,
    и новые модули не замыкаются кругом на фасад.
    """

    def test_facade_reexports_the_same_objects(self):
        """Фасад отдаёт те же объекты, а не копии."""
        from reaper import target_registry, target_spec, target_yaml

        self.assertIs(tg.TargetSpec, target_spec.TargetSpec)
        self.assertIs(tg.TargetSet, target_registry.TargetSet)
        self.assertIs(tg.builtin_targets, target_registry.builtin_targets)
        self.assertIs(tg.BUILTIN_TARGETS, target_registry.BUILTIN_TARGETS)
        self.assertIs(tg.load_targets, target_yaml.load_targets)
        self.assertIs(tg.parse_targets, target_yaml.parse_targets)
        self.assertIs(tg.TS_ISO, target_spec.TS_ISO)
        self.assertIs(tg.TS_LOCAL, target_spec.TS_LOCAL)
        self.assertIs(tg.DEFAULT_TARGETS_PATH, target_yaml.DEFAULT_TARGETS_PATH)

    def test_facade_keeps_public_names(self):
        """Все имена, которыми пользовались раньше, доступны и теперь."""
        for name in ("TargetSpec", "TargetSet", "ACTIVITY_MARKERS",
                     "ACTIVITY_MTIME", "GRADLE_MAIN", "KOTLIN_MAIN",
                     "TS_ISO", "TS_LOCAL", "BUILTIN_TARGETS",
                     "DEFAULT_TARGETS_PATH", "builtin_targets", "load_targets",
                     "parse_targets"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(tg, name))

    def test_no_module_imports_the_facade_back(self):
        """Цикла импорта нет: новые модули не тянут `reaper.targets`.

        Если бы кто-то из них импортировал фасад, то фасад (он импортирует
        их) замкнул бы круг — и обычный `import reaper.targets` сломался бы
        в зависимости от порядка импорта.
        """
        import ast

        package = os.path.dirname(os.path.abspath(tg.__file__))
        for module in ("target_spec.py", "target_registry.py", "target_yaml.py"):
            with self.subTest(module=module):
                with open(os.path.join(package, module), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                imported = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        if node.level and node.module:
                            imported.add(node.module)
                        for alias in node.names:
                            imported.add(alias.name)
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            imported.add(alias.name)
                self.assertNotIn("targets", imported)
                self.assertNotIn("reaper.targets", imported)


if __name__ == "__main__":
    unittest.main()
