"""CLI рейпера: разбор аргументов, главный цикл, запись состояния."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from typing import Any

from .classify import classify
from .collect import collect_daemons
from .config import (RULES_VERSION, disabled_flag,
                    idle_limit_for_kind, load_config, resolve_targets)
from .execute import execute
from .history import archive_daemon_log
from .housekeeping import trim_idle_logs
from .model import Decision
from .report import (
    JSON_SCHEMA_VERSION,
    PILOT_MAX_REUSE_RATE,
    PILOT_MIN_EVENTS,
    audit_report,
    canary,
    format_explain,
    format_json,
    format_status,
    health,
    idle_limits,
    pilot_report,
)
from .targets import TargetSet, load_targets
from .state import (
    append_kill,
    append_sweep,
    degradation_warnings,
    load_state,
    measure_coverage,
    note_rule_hits,
    note_stale_peers,
    record_freed,
    save_state,
    sweep_record,
    sync_kind_signatures,
    sync_rules_version,
    update_metric,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Рейпер застаревших Gradle/Kotlin-демонов")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать решения, ничего не убивать")
    parser.add_argument("--mode", choices=("kill", "dry-run"),
                        help="режим работы: kill (по умолчанию) или dry-run")
    parser.add_argument("--status", action="store_true",
                        help="таблица демонов с решениями (ничего не убивает)")
    parser.add_argument("--json", action="store_true",
                        help="машиночитаемый вывод вместо таблицы")
    parser.add_argument("--health", action="store_true",
                        help="проверка «таймер молча не работает»")
    parser.add_argument("--pilot", action="store_true",
                        help="вердикт по пилоту: можно ли включать kill-режим")
    parser.add_argument("--audit", action="store_true",
                        help="аудит: демоны с простоями длиннее IDLE_SECONDS")
    parser.add_argument("--explain", action="store_true",
                        help="напечатать таблицу правил политики")
    parser.add_argument("--config", help="путь к конфигу")
    parser.add_argument("--targets", help="путь к файлу целей (YAML); "
                                          "по умолчанию — "
                                          "~/.config/java-reaper/targets.yaml")
    return parser


def write_state(cfg: dict[str, Any], state: dict[str, Any],
                decisions: list[Decision], now: float,
                trimmed_pids: set[int] | None = None) -> None:
    """Переносит наблюдения в состояние: baseline, отметки активности,
    счётчики SIGTERM, возраст peer-соединений."""
    prev = state.setdefault("daemons", {})
    for dec in decisions:
        d = dec.daemon
        rec = prev.setdefault(str(d.pid), {})
        rec.setdefault("first_seen", now)
        rec["kind"] = d.kind
        rec["cpu_ticks"] = d.cpu_ticks
        rec["starttime"] = d.starttime
        rec["last_cpu_at"] = now
        rec["log_offset"] = d.log_offset
        rec["log_path"] = d.log_path
        if d.kind == "gradle":
            rec["last_build_start"] = d.last_build_start
            rec["last_build_end"] = d.last_build_end
            # Момент отмены сборки держим в состоянии: лог разбирается
            # инкрементально, и без записи маркер отмены был бы виден
            # ровно один свип — а «зависший» демон нужно видеть спустя часы.
            rec["canceled_at"] = d.canceled_at
        if d.peer_first_seen:
            rec["peer_first_seen"] = d.peer_first_seen
        if trimmed_pids and d.pid in trimmed_pids:
            # Лог усечён — читать его заново с нуля.
            rec["log_offset"] = 0
        if dec.busy:
            # Демон реально работал (CPU/дети/свежий лог) — отсчёт простоя
            # начинается заново. Защита (peer, нет baseline) — не активность:
            # иначе демон, за которым кто-то подключён, никогда не состарится.
            rec["last_activity"] = now
        elif dec.last_activity is not None:
            rec["last_activity"] = max(rec.get("last_activity", 0.0) or 0.0,
                                       dec.last_activity)
        if dec.busy or dec.protected:
            # Демон снова в работе или под наблюдением — счётчик попыток
            # SIGTERM обнуляется: иначе эскалация до SIGKILL накопится за
            # несвязанные эпизоды и убьёт живой демон.
            rec["sigterm_attempts"] = 0
    # Забытые PID убираем.
    live_pids = {str(dec.daemon.pid) for dec in decisions}
    for key in list(prev.keys()):
        if key not in live_pids:
            prev.pop(key, None)


def archive_vanished(cfg: dict[str, Any], state: dict[str, Any],
                     daemons: list[Any], now: float | None = None,
                     targets: TargetSet | None = None) -> int:
    """Архивирует сводки по демонам, которых больше нет.

    Логи Kotlin-демонов живут в /tmp и умирают вместе с перезагрузкой.
    Если не сохранить сводку сейчас, демон исчезнет из истории навсегда —
    и по нему никогда нельзя будет понять, сколько памяти он занимал.

    Момент, когда мы заметили пропажу демона, — это и есть его смерть
    (с точностью до интервала свипа): лог-то молчит с последней записи,
    и без этой отметки простой демона не виден вовсе.

    Вместе с верхней границей (`death_observed_at`) запоминаем и нижнюю —
    `last_seen_at`, момент последнего свипа, на котором демон ещё был жив.
    Между ними демон умер, но когда именно — неизвестно; знать нижнюю
    границу важно, чтобы оценивать простой честно: считать, что демон жил
    до момента обнаружения, — это завышать его простой.
    """
    history_path = os.path.join(os.path.expanduser(str(cfg["STATE_DIR"])),
                                "logs-history.jsonl")
    live = {d.pid for d in daemons}
    prev = state.get("daemons", {})
    seen_at = time.time() if now is None else now
    # Момент предыдущего свипа: на нём демон ещё был виден (он был в
    # состоянии, из которого мы его и вычитываем).
    last_seen_at = state.get("last_sweep_at")
    archived = 0
    for pid, rec in list(prev.items()):
        if int(pid) in live:
            continue
        path = rec.get("log_path")
        if not path or not os.path.exists(path):
            continue
        try:
            if archive_daemon_log(str(path), rec.get("kind"), history_path,
                                  death_observed_at=seen_at, targets=targets,
                                  last_seen_at=last_seen_at):
                archived += 1
        except OSError:
            continue
    return archived


def _sweep(cfg: dict[str, Any], state: dict[str, Any], now: float,
           targets: TargetSet | None = None):
    """Один свип: собрать → классифицировать. Состояние здесь не меняется."""
    daemons = collect_daemons(cfg, state, now, targets)
    decisions = classify(daemons, state, now, cfg, targets=targets)
    return daemons, decisions


def _say(args: argparse.Namespace, text: str) -> None:
    """Человекочитаемое сообщение — в stdout, но с --json уходит в stderr.

    С --json stdout должен оставаться чистым JSON: иначе его нельзя
    разобрать (`json.loads(proc.stdout)`). Служебные сообщения (сброс
    метрики, предупреждения, список действий) в JSON-режиме идут в stderr.
    """
    stream = sys.stderr if getattr(args, "json", False) else sys.stdout
    print(text, file=stream, flush=True)


def _load_context(args: argparse.Namespace
                  ) -> tuple[dict[str, Any], TargetSet, dict[str, Any], list[str]]:
    """Конфиг, цели, состояние и предупреждения — всё, что нужно до решения.

    Порядок важен: цели резолвятся по конфигу, а состояние читается уже
    по готовому конфигу — иначе демоны собрались бы не по тем правилам.
    """
    config_warnings: list[str] = []
    explicit_keys: set[str] = set()
    cfg = load_config(args.config, config_warnings, explicit_keys)
    # Цели (targets.yaml): кто считается демоном, где его лог, как его читать.
    # Файл необязателен — без него работают встроенные цели Gradle и Kotlin.
    targets = resolve_targets(load_targets(args.targets, config_warnings), cfg,
                              explicit_keys, config_warnings)
    state = load_state(cfg)
    return cfg, targets, state, config_warnings


def _pilot_json(cfg: dict[str, Any], state: dict[str, Any], code: int) -> str:
    """Машиночитаемый вердикт пилота — те же числа, что и в тексте.

    Отдельный JSON, а не разбор текста: потребитель (агент, скрипт) должен
    видеть ровно те величины, на которых вынесен вердикт.
    """
    metric = state.get("metric", {}) or {}
    return json.dumps({
        "schema_version": JSON_SCHEMA_VERSION,
        "metric": metric,
        "events": int(metric.get("events", 0)),
        "reused": int(metric.get("reused", 0)),
        "reuse_rate": float(metric.get("rate", 0.0)),
        "by_kind": metric.get("by_kind", {}),
        "pilot_started_at": state.get("pilot_started_at", 0.0),
        "freed_mb": state.get("freed_mb", 0),
        "would_free_mb": state.get("would_free_mb", 0),
        "stale_peer_events": state.get("stale_peer_events", 0),
        "stale_peer_daemons": state.get("stale_peer_daemons", {}),
        "criterion": {
            "max_reuse_rate": float(cfg.get("PILOT_MAX_REUSE_RATE",
                                            PILOT_MAX_REUSE_RATE)),
            "min_events": int(cfg.get("PILOT_MIN_EVENTS",
                                      PILOT_MIN_EVENTS))},
        "verdict_code": code,
    }, ensure_ascii=False, indent=1, sort_keys=True)


def _run_readonly(args: argparse.Namespace, cfg: dict[str, Any],
                  targets: TargetSet, state: dict[str, Any]) -> int | None:
    """Команды, которые только читают состояние.

    Их выполняем до блокировки и свипа: посмотреть, что рейпер наделал,
    должно быть можно и тогда, когда свип уже идёт, а молчащий таймер не
    должен мешать диагностике. None — такой команды не было.
    """
    if args.explain:
        print(format_explain(cfg, targets), flush=True)
        return 0

    if args.pilot:
        text, code = pilot_report(cfg, state)
        if args.json:
            print(_pilot_json(cfg, state, code), flush=True)
        else:
            print(text, flush=True)
        return code

    if args.health:
        text, code = health(cfg, state)
        print(text, flush=True)
        return code
    return None


def _emergency_brake(args: argparse.Namespace, cfg: dict[str, Any]) -> bool:
    """Аварийный тормоз: файл-флаг отключения рейпера.

    Проверяем раньше всего остального: выключенный рейпер не должен ни
    блокировку брать, ни состояние трогать.
    """
    flag = disabled_flag(cfg)
    if not os.path.exists(flag):
        return False
    _say(args, f"java-reaper: отключён флагом {flag} — ничего не делаю")
    return True


def _acquire_lock(cfg: dict[str, Any]):
    """Единственный владелец состояния: flock исключает наложение прогонов.

    Возвращает открытый дескриптор — его надо держать живым до конца
    прогона, иначе блокировка снимется сама. None — прогон уже идёт.
    """
    os.makedirs(os.path.expanduser(str(cfg["STATE_DIR"])), exist_ok=True)
    lock_path = os.path.abspath(os.path.expanduser(str(cfg["LOCK_PATH"])))
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fh.close()
        return None
    return lock_fh


def _reset_metric_if_rules_changed(args: argparse.Namespace,
                                   cfg: dict[str, Any], state: dict[str, Any],
                                   targets: TargetSet) -> None:
    """Сброс метрики пилота, если правила изменились.

    Метрика, набранная по старым порогам, измеряет другое — её надо
    обнулить. Но по возможности по одному виду: по остальным она
    остаётся честной.
    """
    if sync_rules_version(state):
        # Правила изменились: метрика пилота, набранная по старым порогам,
        # измеряет другое — сбрасываем её ДО свипа, чтобы события этого
        # свипа уже записывались по новым правилам.
        _say(args, f"java-reaper: правила изменились (версия {RULES_VERSION}) — "
                   f"метрика пилота сброшена")
        return
    # Версия кода та же, но правила конкретной цели могли измениться
    # (порог в targets.yaml). Тогда сбрасывается метрика ТОЛЬКО по этому
    # виду: по остальным она остаётся честной.
    changed = sync_kind_signatures(state, targets, cfg)
    if changed:
        _say(args, "java-reaper: правила изменились для вида "
                   + ", ".join(changed)
                   + " — метрика по нему перезапущена, по остальным сохранена")


def _print_status(args: argparse.Namespace, decisions: list[Decision],
                  state: dict[str, Any], cfg: dict[str, Any], now: float,
                  targets: TargetSet) -> None:
    """Таблица демонов с решениями (или тот же список в JSON)."""
    print(format_json(decisions, state, cfg, now=now, targets=targets)
          if args.json
          else format_status(decisions, state, cfg, targets), flush=True)


def _audit_json(cfg: dict[str, Any], decisions: list[Decision],
                targets: TargetSet, code: int) -> str:
    """Машиночитаемый аудит — те же демоны, что и в текстовом отчёте."""
    limits = idle_limits(cfg, targets)
    stale = [d for d in decisions
             if d.idle_seconds is not None
             and d.idle_seconds > limits.get(
                 d.daemon.kind,
                 idle_limit_for_kind(cfg, d.daemon.kind, targets))]
    return json.dumps({
        "schema_version": JSON_SCHEMA_VERSION,
        "idle_limit_seconds": limits,
        "stale_daemons": [
            {"pid": d.daemon.pid, "kind": d.daemon.kind,
             "footprint_mb": d.daemon.footprint_mb,
             "idle_seconds": d.idle_seconds, "reason": d.reason}
            for d in stale],
        "total_footprint_mb": sum(d.daemon.footprint_mb for d in stale),
        "verdict_code": code,
    }, ensure_ascii=False, indent=1, sort_keys=True)


def _print_audit(args: argparse.Namespace, decisions: list[Decision],
                 state: dict[str, Any], cfg: dict[str, Any],
                 targets: TargetSet) -> None:
    """Аудит приёмки: демоны с простоями длиннее порога."""
    text, code = audit_report(cfg, decisions, state, targets)
    if args.json:
        print(_audit_json(cfg, decisions, targets, code), flush=True)
    else:
        print(text, flush=True)


def _print_sweep_result(args: argparse.Namespace, decisions: list[Decision],
                        state: dict[str, Any], cfg: dict[str, Any],
                        report: list[dict[str, Any]], now: float,
                        targets: TargetSet, daemons: list[Any]) -> None:
    """Вывод результата свипа и предупреждение о слепом детекторе."""
    print(format_json(decisions, state, cfg, report, now, targets=targets)
          if args.json
          else format_status(decisions, state, cfg, targets), flush=True)
    warn = canary(daemons, decisions, targets)
    if warn:
        _say(args, "WARNING: " + warn)


def _save_sweep_state(cfg: dict[str, Any], state: dict[str, Any],
                      decisions: list[Decision], now: float,
                      report: list[dict[str, Any]],
                      trimmed_pids: set[int]) -> int:
    """Запись состояния по итогам свипа.

    Сначала наблюдения, потом метрика и журнал: метрика считается по
    тому, что действительно сделали, а не по предположению.
    """
    write_state(cfg, state, decisions, now, trimmed_pids)
    update_metric(state, decisions, now, cfg, report)
    note_stale_peers(state, decisions)
    append_sweep(cfg, sweep_record(decisions, now, cfg,
                                   mem_available_mb=state.get("mem_available_mb")))
    freed = record_freed(state, report)
    state["last_sweep_at"] = now
    state["last_sweep_ok"] = True
    if not state.get("pilot_started_at"):
        state["pilot_started_at"] = now
    save_state(cfg, state)
    return freed


def _log_actions(args: argparse.Namespace, cfg: dict[str, Any],
                 report: list[dict[str, Any]],
                 decisions: list[Decision]) -> None:
    """Журнал действий: что сделали с каждым демоном.

    Отдельные строки, а не часть отчёта: с --json stdout должен остаться
    чистым JSON, поэтому действия уходят в stderr.
    """
    for item in report:
        _say(args, f"java-reaper: pid={item['pid']} {item['action']} — "
                   f"{item['reason']}")
        if item.get("action") in ("SIGTERM", "SIGKILL"):
            append_kill(cfg, {
                "pid": item["pid"],
                "action": item["action"],
                "reason": item["reason"],
                "footprint_mb": item.get("footprint_mb", 0),
                "kind": next((d.daemon.kind for d in decisions
                              if d.daemon.pid == item["pid"]), "?"),
            })


def _run_sweep(args: argparse.Namespace, cfg: dict[str, Any],
               state: dict[str, Any], daemons: list[Any],
               decisions: list[Decision], now: float, mode: str,
               targets: TargetSet) -> int:
    """Рабочий свип: убрать застаревшие демоны и записать результат.

    Порядок важен: сначала решение, потом вывод, потом состояние — по
    прерванному прогону должно быть видно, что уже решено, а не только
    что записано.
    """
    # Демоны, которых больше нет, архивируем в историю: их логи в /tmp
    # исчезают при перезагрузке, и без сводки они исчезнут навсегда.
    archive_vanished(cfg, state, daemons, now, targets)

    report = execute(decisions, state, now, cfg, dry_run=(mode == "dry-run"))
    trim_report = trim_idle_logs(decisions, cfg, now, targets)
    report = report + trim_report
    trimmed_pids = {int(i["pid"]) for i in trim_report if i.get("action") == "trim"}

    # Наблюдаемость: какие правила сработали и не деградировала ли детекция.
    # Считаем ДО вывода — отчёт должен показывать свежие числа.
    note_rule_hits(state, decisions)
    if decisions and decisions[0].context is not None:
        state["mem_available_mb"] = decisions[0].context.mem_available_mb
    measured, total = measure_coverage(state, decisions, cfg)
    warnings = degradation_warnings(state, cfg, measured, total)
    state["degradation"] = warnings

    _print_sweep_result(args, decisions, state, cfg, report, now, targets,
                         daemons)
    for w in warnings:
        _say(args, f"WARNING: {w}")

    freed = _save_sweep_state(cfg, state, decisions, now, report, trimmed_pids)
    _log_actions(args, cfg, report, decisions)
    killed = [i for i in report if i["action"] in ("SIGTERM", "SIGKILL")]
    _say(args, f"java-reaper: режим {mode}, демонов {len(decisions)}, "
               f"сигналов отправлено {len(killed)}, "
               f"кандидатов на убийство {sum(1 for d in decisions if d.kill)}"
               + (f", освобождено {freed} МБ" if freed else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Точка входа: разбор аргументов и выбор режима.

    В main остаётся только маршрутизация — что именно делает команда,
    живёт в отдельных функциях. Иначе правка любого отчёта задевала бы
    разбор аргументов, и diff переставал бы что-то объяснять.
    """
    args = build_parser().parse_args(argv)
    mode = args.mode or ("dry-run" if args.dry_run else "kill")
    cfg, targets, state, config_warnings = _load_context(args)
    for w in config_warnings:
        _say(args, f"WARNING: {w}")

    code = _run_readonly(args, cfg, targets, state)
    if code is not None:
        return code

    if _emergency_brake(args, cfg):
        return 0

    # Дескриптор держим открытым до конца прогона: пока он жив, жив
    # и flock, а второй прогон не начнётся.
    lock_fh = _acquire_lock(cfg)
    if lock_fh is None:
        _say(args, "java-reaper: другой прогон уже идёт — выхожу")
        return 0

    now = time.time()
    _reset_metric_if_rules_changed(args, cfg, state, targets)
    daemons, decisions = _sweep(cfg, state, now, targets)

    if args.status or args.audit:
        # --status/--audit — только чтение: состояние не трогаем.
        if args.audit:
            _print_audit(args, decisions, state, cfg, targets)
        else:
            _print_status(args, decisions, state, cfg, now, targets)
        warn = canary(daemons, decisions, targets)
        if warn:
            _say(args, "WARNING: " + warn)
        return 0

    return _run_sweep(args, cfg, state, daemons, decisions, now, mode, targets)


if __name__ == "__main__":
    sys.exit(main())
