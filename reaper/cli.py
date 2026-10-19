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
from .config import disabled_flag, load_config
from .execute import execute
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
    pilot_report,
)
from .state import (
    append_kill,
    load_state,
    note_stale_peers,
    record_freed,
    save_state,
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
        if d.kind == "gradle":
            rec["last_build_start"] = d.last_build_start
            rec["last_build_end"] = d.last_build_end
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


def _sweep(cfg: dict[str, Any], state: dict[str, Any], now: float):
    """Один свип: собрать → классифицировать. Состояние здесь не меняется."""
    daemons = collect_daemons(cfg, state, now)
    decisions = classify(daemons, state, now, cfg)
    return daemons, decisions


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = args.mode or ("dry-run" if args.dry_run else "kill")

    cfg = load_config(args.config)
    state = load_state(cfg)

    # ---- команды только на чтение ----
    if args.explain:
        print(format_explain(cfg), flush=True)
        return 0

    if args.pilot:
        text, code = pilot_report(cfg, state)
        if args.json:
            metric = state.get("metric", {}) or {}
            print(json.dumps({
                "schema_version": JSON_SCHEMA_VERSION,
                "metric": metric,
                "events": int(metric.get("events", 0)),
                "reused": int(metric.get("reused", 0)),
                "reuse_rate": float(metric.get("rate", 0.0)),
                "pilot_started_at": state.get("pilot_started_at", 0.0),
                "freed_mb": state.get("freed_mb", 0),
                "would_free_mb": state.get("would_free_mb", 0),
                "stale_peer_events": state.get("stale_peer_events", 0),
                "stale_peer_daemons": state.get("stale_peer_daemons", {}),
                "criterion": {"max_reuse_rate": PILOT_MAX_REUSE_RATE,
                              "min_events": PILOT_MIN_EVENTS},
                "verdict_code": code,
            }, ensure_ascii=False, indent=1, sort_keys=True), flush=True)
        else:
            print(text, flush=True)
        return code

    if args.health:
        text, code = health(cfg, state)
        print(text, flush=True)
        return code

    # ---- аварийный тормоз ----
    flag = disabled_flag(cfg)
    if os.path.exists(flag):
        print(f"java-reaper: отключён флагом {flag} — ничего не делаю", flush=True)
        return 0

    os.makedirs(os.path.expanduser(str(cfg["STATE_DIR"])), exist_ok=True)

    # Единственный владелец состояния: flock исключает наложение прогонов.
    lock_path = os.path.abspath(os.path.expanduser(str(cfg["LOCK_PATH"])))
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("java-reaper: другой прогон уже идёт — выхожу", flush=True)
        return 0

    now = time.time()
    daemons, decisions = _sweep(cfg, state, now)

    if args.status or args.audit:
        # --status/--audit — только чтение: состояние не трогаем.
        if args.audit:
            text, code = audit_report(cfg, decisions, state)
            if args.json:
                idle_limit = float(cfg["IDLE_SECONDS"])
                stale = [d for d in decisions
                         if d.idle_seconds is not None and d.idle_seconds > idle_limit]
                print(json.dumps({
                    "schema_version": JSON_SCHEMA_VERSION,
                    "idle_limit_seconds": idle_limit,
                    "stale_daemons": [
                        {"pid": d.daemon.pid, "kind": d.daemon.kind,
                         "footprint_mb": d.daemon.footprint_mb,
                         "idle_seconds": d.idle_seconds, "reason": d.reason}
                        for d in stale],
                    "total_footprint_mb": sum(d.daemon.footprint_mb for d in stale),
                    "verdict_code": code,
                }, ensure_ascii=False, indent=1, sort_keys=True), flush=True)
            else:
                print(text, flush=True)
        else:
            print(format_json(decisions, state, cfg, now=now) if args.json
                  else format_status(decisions, state), flush=True)
        warn = canary(daemons, decisions)
        if warn:
            print("WARNING: " + warn, flush=True)
        return 0

    # ---- рабочий свип ----
    report = execute(decisions, state, now, cfg, dry_run=(mode == "dry-run"))
    trim_report = trim_idle_logs(decisions, cfg, now)
    report = report + trim_report
    trimmed_pids = {int(i["pid"]) for i in trim_report if i.get("action") == "trim"}

    print(format_json(decisions, state, cfg, report, now) if args.json
          else format_status(decisions, state), flush=True)
    warn = canary(daemons, decisions)
    if warn:
        print("WARNING: " + warn, flush=True)

    write_state(cfg, state, decisions, now, trimmed_pids)
    update_metric(state, decisions, now, cfg)
    note_stale_peers(state, decisions)
    freed = record_freed(state, report)
    state["last_sweep_at"] = now
    state["last_sweep_ok"] = True
    if not state.get("pilot_started_at"):
        state["pilot_started_at"] = now
    save_state(cfg, state)

    for item in report:
        print(f"java-reaper: pid={item['pid']} {item['action']} — {item['reason']}",
              flush=True)
        if item.get("action") in ("SIGTERM", "SIGKILL"):
            append_kill(cfg, {
                "pid": item["pid"],
                "action": item["action"],
                "reason": item["reason"],
                "footprint_mb": item.get("footprint_mb", 0),
                "kind": next((d.daemon.kind for d in decisions
                              if d.daemon.pid == item["pid"]), "?"),
            })
    killed = [i for i in report if i["action"] in ("SIGTERM", "SIGKILL")]
    print(f"java-reaper: режим {mode}, демонов {len(decisions)}, "
          f"сигналов отправлено {len(killed)}, "
          f"кандидатов на убийство {sum(1 for d in decisions if d.kill)}"
          + (f", освобождено {freed} МБ" if freed else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
