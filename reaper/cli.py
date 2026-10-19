"""CLI рейпера: разбор аргументов, главный цикл, запись состояния."""

from __future__ import annotations

import argparse
import fcntl
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
from .report import canary, format_json, format_status, health
from .state import load_state, record_freed, save_state, update_metric


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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = args.mode or ("dry-run" if args.dry_run else "kill")

    cfg = load_config(args.config)
    state = load_state(cfg)

    if args.health:
        text, code = health(cfg, state)
        print(text, flush=True)
        return code

    # Аварийный тормоз: файл-флаг выключает рейпер до любых действий.
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
    daemons = collect_daemons(cfg, state, now)
    decisions = classify(daemons, state, now, cfg)

    if args.status:
        # --status — только чтение: состояние не трогаем.
        print(format_json(decisions, state, cfg, now=now) if args.json
              else format_status(decisions, state), flush=True)
        warn = canary(daemons, decisions)
        if warn:
            print("WARNING: " + warn, flush=True)
        return 0

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
    freed = record_freed(state, report)
    state["last_sweep_at"] = now
    state["last_sweep_ok"] = True
    save_state(cfg, state)

    for item in report:
        print(f"java-reaper: pid={item['pid']} {item['action']} — {item['reason']}",
              flush=True)
    killed = [i for i in report if i["action"] in ("SIGTERM", "SIGKILL")]
    print(f"java-reaper: режим {mode}, демонов {len(decisions)}, "
          f"сигналов отправлено {len(killed)}, "
          f"кандидатов на убийство {sum(1 for d in decisions if d.kill)}"
          + (f", освобождено {freed} МБ" if freed else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
