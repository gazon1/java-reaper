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

    def test_flat_key_wins_over_yaml(self):
        """Оператор правил старый ключ конфига — он и главный."""
        cfg = dict(self.cfg)
        cfg["IDLE_SECONDS"] = 3600
        targets = tg.TargetSet({"gradle": tg.TargetSpec(name="gradle",
                                                        idle_seconds=21600)})
        resolved = reaper.resolve_targets(targets, cfg, {"IDLE_SECONDS"},
                                          self.warnings)
        self.assertEqual(resolved.get("gradle").idle_seconds, 3600)

    def test_mismatch_warns(self):
        """Значения расходятся — молчать об этом нельзя."""
        cfg = dict(self.cfg)
        cfg["IDLE_SECONDS"] = 3600
        targets = tg.TargetSet({"gradle": tg.TargetSpec(name="gradle",
                                                        idle_seconds=21600)})
        reaper.resolve_targets(targets, cfg, {"IDLE_SECONDS"}, self.warnings)
        self.assertTrue(any("IDLE_SECONDS" in w for w in self.warnings))

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


if __name__ == "__main__":
    unittest.main()
