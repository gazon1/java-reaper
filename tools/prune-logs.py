#!/usr/bin/env python3
"""Уборка логов мёртвых демонов.

Gradle и Kotlin не убирают свои логи: `~/.gradle/daemon` разросся до
1.2 ГБ (3831 файл), `/tmp/kotlin-daemon*.log` — до 108 МБ (207 файлов).
Это не «полезный след», это мусор от процессов, которых давно нет.

Инструмент удаляет ТОЛЬКО логи процессов, которых уже не существует.

Как определяется «лог мёртвого демона»:

  * ЛОГ ЖИВ, если его держит открытым какой-либо процесс. Это главная и
    достаточная проверка: пока демон жив, он держит свой лог открытым.
    Проверка делается один раз: собираем все файлы, открытые процессами
    машины, и сверяем с кандидатами.
  * Для Gradle дополнительно сверяется PID из имени (`daemon-<pid>.out.log`
    — там PID настоящий) с живым процессом-демоном.
  * Для Kotlin PID в имени НЕ настоящий: имя вида
    `kotlin-daemon.2026-10-20.08-29-00-827.00.log` содержит номер
    инстанса, а не PID (проверено: живой демон с PID 35498 пишет в лог
    `…-827.00.log`). Поэтому для Kotlin решает только проверка «файл открыт».

По умолчанию инструмент НИЧЕГО НЕ УДАЛЯЕТ, а только показывает план
(режим dry-run). Удаление включает флаг `--apply`.

Чего инструмент не трогает (никогда):

  * маркеры Kotlin (`kotlin-compiler-client-*-is-running`,
    `kotlin-compiler-in-*.alive`, `kotlin-compilation-session-*-is-running`) —
    их в жизни создаёт и удаляет сам Kotlin, убийство демона их не
    оправдывает, а ломать может;
  * логи живых демонов (проверка «файл открыт»);
  * что-либо, чьё имя не совпадает с известными шаблонами.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any

# Главные классы демонов: по ним видно, что процесс — тот самый демон.
GRADLE_MAIN = "org.gradle.launcher.daemon.bootstrap.GradleDaemon"
KOTLIN_MAIN = "org.jetbrains.kotlin.daemon.KotlinCompileDaemon"

# Шаблоны имён логов.
GRADLE_LOG_RE = re.compile(r"^daemon-(?P<pid>\d+)\.out\.log$")
# В имени Kotlin-лога PID нет: последнее число — инстанс, а не процесс.
# Один демон пишет в несколько файлов (.00, .01, .02 — ротация), поэтому
# шаблон выделяет ГРУППУ файлов одного демона: префикс до номера файла.
KOTLIN_GROUP_RE = re.compile(
    r"^(?P<group>kotlin-daemon\.\d{4}-\d{2}-\d{2}\.\d{2}-\d{2}-\d{2}-\d+)"
    r"\.\d+\.log(?:\.lck)?$")

# Маркеры Kotlin: трогать нельзя никогда.
FORBIDDEN_PREFIXES = (
    "kotlin-compiler-client-",
    "kotlin-compiler-in-",
    "kotlin-compilation-session-",
)


def is_forbidden(name: str) -> bool:
    """Файл из «неприкосновенного» списка Kotlin."""
    return any(name.startswith(p) for p in FORBIDDEN_PREFIXES)


def proc_cmdline(pid: int) -> str:
    """Командная строка процесса (пустая, если процесса нет)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().decode("utf-8", "replace").replace("\0", " ")
    except OSError:
        return ""


def is_live_daemon(pid: int, kind: str = "gradle") -> bool:
    """Жив ли демон этого класса с таким PID.

    PID мог быть переиспользован другим процессом — тогда демон мёртв,
    даже если PID существует.
    """
    cmdline = proc_cmdline(pid)
    if not cmdline:
        return False
    main = GRADLE_MAIN if kind == "gradle" else KOTLIN_MAIN
    return main in cmdline


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


def iter_gradle_logs(gradle_dir: str):
    """Все логи Gradle: (путь, pid)."""
    if not os.path.isdir(gradle_dir):
        return
    for version in sorted(os.listdir(gradle_dir)):
        sub = os.path.join(gradle_dir, version)
        if not os.path.isdir(sub):
            continue
        for name in sorted(os.listdir(sub)):
            pid = parse_gradle_name(name)
            if pid is None:
                continue
            yield os.path.join(sub, name), pid


def kotlin_groups(kotlin_dir: str) -> dict[str, list[str]]:
    """Группы файлов одного демона Kotlin: префикс → список путей.

    Группировка обязательна: живой демон держит открытым только последний
    файл (.NN), а предыдущие (.00, .01) остаются на диске. Если судить
    по каждому файлу отдельно, мы удалили бы логи живого демона.
    """
    groups: dict[str, list[str]] = {}
    if not os.path.isdir(kotlin_dir):
        return groups
    for name in sorted(os.listdir(kotlin_dir)):
        if is_forbidden(name):
            continue
        m = KOTLIN_GROUP_RE.match(name)
        if not m:
            continue
        groups.setdefault(m.group("group"), []).append(
            os.path.join(kotlin_dir, name))
    return groups


def parse_gradle_name(name: str) -> int | None:
    """PID из имени лога Gradle (в имени он настоящий)."""
    m = GRADLE_LOG_RE.match(name)
    return int(m.group("pid")) if m else None


def parse_kotlin_group(name: str) -> str | None:
    """Префикс группы файлов демона Kotlin (если имя подходит под шаблон)."""
    m = KOTLIN_GROUP_RE.match(name)
    return m.group("group") if m else None


def collect(gradle_dir: str, kotlin_dir: str, min_age_seconds: float,
            now: float, open_paths: set[str] | None = None
            ) -> list[dict[str, Any]]:
    """Собирает план уборки: логи мёртвых демонов, которые не жалко удалить."""
    if open_paths is None:
        open_paths = open_paths_under([gradle_dir, kotlin_dir])
    plan: list[dict[str, Any]] = []

    # Gradle: один демон — один файл, и PID в имени настоящий.
    for path, pid in iter_gradle_logs(gradle_dir):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if (now - st.st_mtime) < min_age_seconds:
            # Свежий лог: демон умер только что, лог ещё может быть
            # нужен для разбора (и наш рейпер его читает).
            continue
        if path in open_paths:
            continue
        if is_live_daemon(pid):
            continue
        plan.append({
            "path": path,
            "kind": "gradle",
            "pid": pid,
            "size_bytes": st.st_size,
            "age_hours": round((now - st.st_mtime) / 3600.0, 1),
        })

    # Kotlin: файлы группируются по демону. Живой демон держит открытым
    # только последний файл группы — поэтому открытый файл защищает
    # ВСЮ группу, а не себя.
    for group, paths in kotlin_groups(kotlin_dir).items():
        if any(p in open_paths for p in paths):
            continue
        for path in paths:
            try:
                st = os.stat(path)
            except OSError:
                continue
            if (now - st.st_mtime) < min_age_seconds:
                continue
            plan.append({
                "path": path,
                "kind": "kotlin",
                "pid": None,
                "size_bytes": st.st_size,
                "age_hours": round((now - st.st_mtime) / 3600.0, 1),
                "lock": path.endswith(".lck"),
            })
    return plan


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
        description="Уборка логов мёртвых Gradle/Kotlin-демонов "
                    "(по умолчанию — dry-run)")
    parser.add_argument("--apply", action="store_true",
                        help="реально удалить (по умолчанию только показать план)")
    parser.add_argument("--min-age-hours", type=float, default=72.0,
                        help="удалять только логи старше N часов (по умолчанию 72)")
    parser.add_argument("--gradle-dir", default=os.path.expanduser("~/.gradle/daemon"),
                        help="каталог логов Gradle")
    parser.add_argument("--kotlin-dir", default="/tmp",
                        help="каталог логов Kotlin")
    parser.add_argument("--json", action="store_true",
                        help="машиночитаемый вывод")
    args = parser.parse_args(argv)

    now = time.time()
    open_paths = open_paths_under([args.gradle_dir, args.kotlin_dir])
    plan = collect(args.gradle_dir, args.kotlin_dir,
                   args.min_age_hours * 3600.0, now, open_paths)

    if args.apply:
        removed, freed, errors = apply_plan(plan)
    else:
        removed, freed, errors = 0, 0, []

    if args.json:
        print(json.dumps({
            "dry_run": not args.apply,
            "min_age_hours": args.min_age_hours,
            "files": len(plan),
            "bytes": sum(i["size_bytes"] for i in plan),
            "removed": removed,
            "removed_bytes": freed,
            "errors": errors,
            "plan": plan,
        }, ensure_ascii=False, indent=1, sort_keys=True), flush=True)
    else:
        print(format_plan(plan, dry_run=not args.apply), flush=True)
        for err in errors:
            print(f"ОШИБКА: {err}", file=sys.stderr, flush=True)
        if args.apply:
            print(f"Удалено файлов: {removed}, освобождено "
                  f"{freed / 1024 / 1024:.1f} МБ", flush=True)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
