#!/usr/bin/env python3
"""Уборка логов мёртвых демонов.

Gradle и Kotlin не убирают свои логи: `~/.gradle/daemon` разросся до
1.2 ГБ (3831 файл), `/tmp/kotlin-daemon*.log` — до 108 МБ (207 файлов).
Это не «полезный след», это мусор от процессов, которых давно нет.

Инструмент удаляет ТОЛЬКО логи процессов, которых уже не существует.

Как определяется «лог мёртвого демона» (для каждой цели — по её описанию
в targets.yaml):

  * ЛОГ ЖИВ, если его держит открытым какой-либо процесс. Это главная и
    достаточная проверка: пока демон жив, он держит свой лог открытым.
    Проверка делается один раз: собираем все файлы, открытые процессами
    машины, и сверяем с кандидатами.
  * Если у цели в имени файла есть настоящий PID (у Gradle — `daemon-<pid>.
    out.log`, там PID настоящий), дополнительно сверяем его с живым
    процессом-демоном этой же цели.
  * Если у цели файлы группируются по инстансу (у Kotlin — ротация
    `.00`, `.01`, `.02`), открытый файл защищает ВСЮ группу: живой демон
    держит открытым только последний файл, а прежние остаются на диске.

По умолчанию инструмент НИЧЕГО НЕ УДАЛЯЕТ, а только показывает план
(режим dry-run). Удаление включает флаг `--apply`.

Перед удалением из лога извлекается КОМПАКТНАЯ СВОДКА и складывается в
`logs-history.jsonl` (рядом с состоянием рейпера): кто, когда жил, когда
шли сборки, была ли отмена. Лог — это десятки мегабайт, сводка — килобайты;
история при этом остаётся навсегда и по ней работает симулятор
(`tools/simulate.py`). Отключается флагом `--no-history`.

Чего инструмент не трогает (никогда):

  * маркеры Kotlin (`kotlin-compiler-client-*-is-running`,
    `kotlin-compiler-in-*.alive`, `kotlin-compilation-session-*-is-running`) —
    их в жизни создаёт и удаляет сам Kotlin, убийство демона их не
    оправдывает, а ломать может (список — в описании цели,
    `forbidden_prefixes`);
  * логи живых демонов (проверка «файл открыт»);
  * что-либо, чьё имя не совпадает с шаблонами цели.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

# Пакет reaper лежит в родительском каталоге — нужен ради сводок истории.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reaper.config import load_config, resolve_targets  # noqa: E402
from reaper.history import (  # noqa: E402
    append_history,
    default_history_path,
    history_keys,
    summarize_by_path,
)
from reaper.logfiles import (  # noqa: E402
    discover,
    group_of as group_of_spec,
    is_forbidden,
    is_target_process,
    pid_of,
)
from reaper.targets import TargetSet, load_targets  # noqa: E402


def is_live_daemon(pid: int, spec: Any, targets: TargetSet) -> bool:
    """Жив ли демон этой цели с таким PID.

    PID мог быть переиспользован другим процессом — тогда демон мёртв,
    даже если PID существует. Поэтому проверяем не «существует ли PID», а
    «похож ли процесс на демон этой цели» (общий модуль `logfiles`).
    """
    return is_target_process(pid, spec, targets)


def open_paths_under(dirs: list[str]) -> set[str]:
    """Все файлы, которые сейчас держит открытыми какой-либо процесс.

    Главная защита от удаления лога живого демона: пока демон жив, он
    держит свой лог открытым. Имя файла для этого не годится (у Kotlin
    в имени нет PID), а вот открытый дескриптор — надёжный признак.
    """
    prefixes = tuple(os.path.realpath(d) + os.sep for d in dirs if d)
    out: set[str] = set()
    if not prefixes:
        return out
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith(prefixes):
                out.add(target)
    return out


def group_of(spec: Any, path: str) -> str:
    """Ключ группы файлов одного демона.

    У Kotlin один демон пишет в несколько файлов (ротация .00/.01/.02),
    и живой демон держит открытым только последний. Если судить по каждому
    файлу отдельно, мы удалили бы логи живого демона. Поэтому у цели есть
    регулярка группировки; если её нет — файл сам себе группа.

    Запасной вариант («файл сам себе группа») живёт здесь, а не в общем
    модуле: там None означает «группу определить нельзя», и для истории
    это другое значение, чем для уборки.
    """
    return group_of_spec(spec, path) or path


def collect(targets: TargetSet, min_age_seconds: float, now: float,
            open_paths: set[str] | None = None,
            overrides: dict[str, list[str]] | None = None
            ) -> list[dict[str, Any]]:
    """Собирает план уборки: логи мёртвых демонов, которые не жалко удалить."""
    plan: list[dict[str, Any]] = []
    for spec in targets.specs():
        paths = discover(spec, overrides)
        if not paths:
            continue
        if open_paths is None:
            open_paths = open_paths_under(
                [os.path.dirname(p) for p in paths])

        # Группируем: открытый файл защищает всю группу.
        groups: dict[str, list[str]] = {}
        for path in paths:
            groups.setdefault(group_of(spec, path), []).append(path)

        for _group, group_paths in groups.items():
            if any(p in open_paths for p in group_paths):
                continue  # живой демон держит свой лог открытым
            for path in group_paths:
                name = os.path.basename(path)
                if is_forbidden(spec, name):
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if (now - st.st_mtime) < min_age_seconds:
                    # Свежий лог: демон умер только что, лог ещё может быть
                    # нужен для разбора (и наш рейпер его читает).
                    continue
                pid = pid_of(spec, path)
                if pid is not None and is_live_daemon(pid, spec, targets):
                    continue
                plan.append({
                    "path": path,
                    "kind": spec.name,
                    "pid": pid,
                    "size_bytes": st.st_size,
                    "age_hours": round((now - st.st_mtime) / 3600.0, 1),
                    "lock": name.endswith(".lck"),
                })
    return plan


def summarize_for_history(item: dict[str, Any],
                          targets: TargetSet | None = None) -> dict[str, Any] | None:
    """Сводка по логу, который собираемся удалить.

    Лог — это десятки мегабайт, сводка — килобайты. Сводка остаётся
    навсегда и по ней работает симулятор правил.
    """
    path = item["path"]
    try:
        return summarize_by_path(path, item.get("kind"), targets)
    except OSError:
        return None


def write_history(plan: list[dict[str, Any]], history_path: str,
                  targets: TargetSet | None = None) -> int:
    """Пишет сводки по всем кандидатам в JSONL-историю. Возвращает число.

    Повторный прогон по тем же файлам не добавляет дубликатов: ключ
    (путь, mtime) уже сохранённой сводки пропускается. Иначе история
    разрасталась бы копиями, а симулятор считал бы каждую запись.
    """
    known = history_keys(history_path)
    written = 0
    for item in plan:
        if item.get("lock"):
            continue  # замок — не демон
        record = summarize_for_history(item, targets)
        if record is None:
            continue
        key = (str(record.get("path")), float(record.get("mtime") or 0.0))
        if key in known:
            continue
        append_history(history_path, record)
        known.add(key)
        written += 1
    return written


def apply_plan(plan: list[dict[str, Any]]) -> tuple[int, int, list[str]]:
    """Удаляет файлы из плана. Возвращает (удалено, байт, ошибки)."""
    removed = 0
    freed = 0
    errors: list[str] = []
    for item in plan:
        path = item["path"]
        try:
            size = os.path.getsize(path)
            os.unlink(path)
            removed += 1
            freed += size
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return removed, freed, errors


def format_plan(plan: list[dict[str, Any]], dry_run: bool) -> str:
    """Человекочитаемый план уборки."""
    mode = "БЫЛО БЫ УДАЛЕНО (dry-run)" if dry_run else "УДАЛЕНО"
    lines = [f"Уборка логов мёртвых демонов: {mode}", "=" * 72]
    if not plan:
        lines.append("  удалять нечего: все логи — либо живых демонов, "
                     "либо свежие")
        return "\n".join(lines)
    total = 0
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in plan:
        by_kind.setdefault(item["kind"], []).append(item)
    for kind, items in sorted(by_kind.items()):
        size = sum(i["size_bytes"] for i in items)
        total += size
        lines.append(f"  {kind}: {len(items)} файлов, {size / 1024 / 1024:.1f} МБ")
        for item in sorted(items, key=lambda i: -i["size_bytes"])[:10]:
            who = (f"pid={item['pid']:>8}" if item["pid"] else "pid=  ???????")
            lines.append(f"    {who} {item['size_bytes'] / 1024:>9.0f} КБ "
                         f"  возраст {item['age_hours']:.1f} ч  {item['path']}")
        if len(items) > 10:
            lines.append(f"    … и ещё {len(items) - 10}")
    lines.append("-" * 72)
    lines.append(f"  итого: {len(plan)} файлов, {total / 1024 / 1024:.1f} МБ")
    if dry_run:
        lines.append("  это план, ничего не удалено; применить: --apply")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Уборка логов мёртвых демонов "
                    "(по умолчанию — dry-run)")
    parser.add_argument("--apply", action="store_true",
                        help="реально удалить (по умолчанию только показать план)")
    parser.add_argument("--min-age-hours", type=float, default=72.0,
                        help="удалять только логи старше N часов (по умолчанию 72)")
    parser.add_argument("--targets", default=None,
                        help="путь к файлу целей (YAML)")
    parser.add_argument("--gradle-dir", default=None,
                        help="каталог логов Gradle (перекрывает описание цели)")
    parser.add_argument("--kotlin-dir", default=None,
                        help="каталог логов Kotlin (перекрывает описание цели)")
    parser.add_argument("--json", action="store_true",
                        help="машиночитаемый вывод")
    parser.add_argument("--history", default=None,
                        help="куда писать сводки истории "
                             "(по умолчанию logs-history.jsonl в каталоге "
                             "состояния рейпера)")
    parser.add_argument("--no-history", action="store_true",
                        help="не сохранять сводки (логи просто удаляются)")
    parser.add_argument("--archive-only", action="store_true",
                        help="только сохранить сводки, ничего не удалять")
    args = parser.parse_args(argv)

    warnings: list[str] = []
    cfg = load_config(args.config if hasattr(args, "config") else None, warnings)
    targets = resolve_targets(load_targets(args.targets, warnings), cfg,
                              set(), warnings)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr, flush=True)

    # Старые ключи командной строки перекрывают описание цели: оператор
    # привык ими пользоваться.
    overrides: dict[str, list[str]] = {}
    if args.gradle_dir:
        overrides["gradle"] = [os.path.join(os.path.expanduser(args.gradle_dir),
                                            "*", "daemon-*.out.log")]
    if args.kotlin_dir:
        overrides["kotlin"] = [os.path.join(os.path.expanduser(args.kotlin_dir),
                                            "kotlin-daemon.*.log*")]

    now = time.time()
    plan = collect(targets, args.min_age_hours * 3600.0, now, None, overrides)

    # Сводки — ДО удаления: после удаления лога данные исчезнут.
    history_path = args.history or default_history_path()
    archived = 0
    if not args.no_history and plan:
        archived = write_history(plan, history_path, targets)

    if args.apply and not args.archive_only:
        removed, freed, errors = apply_plan(plan)
    else:
        removed, freed, errors = 0, 0, []

    if args.json:
        print(json.dumps({
            "dry_run": not args.apply or args.archive_only,
            "min_age_hours": args.min_age_hours,
            "files": len(plan),
            "bytes": sum(i["size_bytes"] for i in plan),
            "removed": removed,
            "removed_bytes": freed,
            "archived": archived,
            "history_path": None if args.no_history else history_path,
            "errors": errors,
            "plan": plan,
        }, ensure_ascii=False, indent=1, sort_keys=True), flush=True)
    else:
        print(format_plan(plan, dry_run=not args.apply or args.archive_only),
              flush=True)
        for err in errors:
            print(f"ОШИБКА: {err}", file=sys.stderr, flush=True)
        if archived:
            print(f"Сводок в истории: {archived} → {history_path}", flush=True)
        if args.apply and not args.archive_only:
            print(f"Удалено файлов: {removed}, освобождено "
                  f"{freed / 1024 / 1024:.1f} МБ", flush=True)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
