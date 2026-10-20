"""История демонов: компактная сводка вместо лога.

Логи мёртвых демонов — это мусор (1.2 ГБ в `~/.gradle/daemon`, 108 МБ
Kotlin-логов в `/tmp`). Но выбрасывать их просто так жалко: по ним можно
ретроспективно сказать, что решили бы нынешние правила. Поэтому перед
удалением лога из него извлекается **сводка** — компактная запись о жизни
демона:

  * когда демон жил (первая активность → последняя запись в лог);
  * когда шли сборки (для Gradle) или когда была активность (для Kotlin);
  * была ли отмена сборки (клиент отвалился — демон «завис»);
  * размер и время изменения лога.

Сводка занимает килобайты вместо мегабайт и остаётся навсегда: по ней
работает симулятор (`tools/simulate.py`), который отвечает на вопрос
«что бы наши правила убили за всю историю» за минуты, а не за дни пилота.

Формат — JSONL: одна строка JSON на демон.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .corpus import read_jsonl
from .logfiles import (instance_from_log_name, instance_of, pid_for,
                       pid_from_log_name)
from .logparse import Markers, parse_line_ts, scan_file
from .targets import ACTIVITY_MARKERS, TargetSet, builtin_targets

# Ширина «корзины» активности для Kotlin: 5 минут. Заметно мельче порога
# простоя (2 ч), поэтому простой и последующее оживение видны точно.
KOTLIN_BUCKET_SECONDS = 300

# Ширина «корзины» активности по умолчанию (у конкретной цели — в targets.yaml,
# поле bucket_seconds). Прежнее имя оставлено для совместимости.
DEFAULT_BUCKET_SECONDS = KOTLIN_BUCKET_SECONDS


def parse_activity_ts(line: bytes, ts_format: str = "local") -> float | None:
    """Таймстемп строки лога → epoch (секунды).

    Формат задаёт цель (targets.yaml → ts_format): у Gradle это iso со
    смещением, у Kotlin — local (местное время, без смещения). Раньше разбор
    был зашит под Kotlin, и любая другая программа с логом без смещения
    разбиралась бы неверно.

    Сам разбор живёт в `logparse` (общий модуль форматов), здесь — тонкая
    обёртка под имя цели.
    """
    return parse_line_ts(line, ts_format)


def activity_buckets(path: str, bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
                     ts_format: str = "local"
                     ) -> tuple[float | None, float | None, list[list[float]]]:
    """Активность демона по логу — корзинами.

    Возвращает (первая активность, последняя активность, корзины активности).
    Корзина — [время начала интервала, сколько секунд активности в нём].
    Корзины идут подряд, без пустых, — это и есть интервалы жизни.

    Имя больше не «kotlin»: приём общий для всех целей, у которых признак
    активности — время записи в лог (activity=mtime).
    """
    first: float | None = None
    last: float | None = None
    buckets: dict[int, float] = {}
    try:
        with open(path, "rb") as fh:
            for line in fh:
                ts = parse_activity_ts(line, ts_format)
                if ts is None:
                    continue
                if first is None:
                    first = ts
                last = ts
                key = int(ts // bucket_seconds) * bucket_seconds
                buckets[key] = buckets.get(key, 0.0) + 1.0
    except OSError:
        return None, None, []
    spans = [[float(k), float(v)] for k, v in sorted(buckets.items())]
    return first, last, spans


def parse_kotlin_ts(line: bytes) -> float | None:
    """Совместимое имя: разбор таймстемпа в формате Kotlin (local)."""
    return parse_activity_ts(line, "local")


def kotlin_activity(path: str, bucket_seconds: int = KOTLIN_BUCKET_SECONDS
                   ) -> tuple[float | None, float | None, list[list[float]]]:
    """Совместимое имя: активность по логу в формате Kotlin (local)."""
    return activity_buckets(path, bucket_seconds, "local")


def summarize_builds(path: str, spec: Any, pid: int | None = None,
                    version: str | None = None) -> dict[str, Any]:
    """Сводка по логу демона, у которого есть маркеры сборок.

    Маркеры берутся из спецификации цели: тот же код обслуживает Gradle и
    любую новую программу с похожим форматом лога.
    """
    scan = scan_file(path, markers=Markers.from_spec(spec),
                     ts_format=getattr(spec, "ts_format", "iso"))
    builds = [[b.start, b.end] for b in scan.builds]
    starts = [b.start for b in scan.builds]
    ends = [b.end for b in scan.builds if b.end is not None]
    first = min(starts) if starts else None
    last = max(ends) if ends else (max(starts) if starts else None)
    record: dict[str, Any] = {
        "kind": spec.name,
        "pid": pid,
        "version": version,
        "path": path,
        "size_bytes": os.path.getsize(path),
        "mtime": scan.mtime,
        "first_activity": first,
        "last_activity": last,
        "builds": builds,
        "unclosed_builds": scan.unclosed_builds,
        "canceled_at": scan.canceled_at,
        # Как восстанавливать промежутки жизни этой сводки: по сборкам или
        # по корзинам активности. Раньше это определялось по наличию ключа
        # в словаре, и смена формы сводки тихо ломала бы разбор истории.
        "activity_kind": "builds",
        # Пороги, по которым жил этот демон. Без них история не самодостаточна:
        # «умер ли демон сам» и «простой ли он дольше порога» пришлось бы
        # угадывать по нынешним настройкам, а те со временем меняются.
        "self_timeout_seconds": getattr(spec, "self_timeout_seconds", None),
        "idle_seconds": getattr(spec, "idle_seconds", None),
        "summary_at": time.time(),
    }
    return record


def summarize_activity(path: str, spec: Any, instance: str | None = None
                       ) -> dict[str, Any]:
    """Сводка по логу демона без маркеров сборок.

    Признак активности — время последней записи в лог; промежутки жизни
    восстанавливаются корзинами активности.
    """
    bucket_seconds = int(getattr(spec, "bucket_seconds", 0)
                         or DEFAULT_BUCKET_SECONDS)
    ts_format = str(getattr(spec, "ts_format", "local") or "local")
    first, last, spans = activity_buckets(path, bucket_seconds, ts_format)
    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        size, mtime = 0, 0.0
    return {
        "kind": spec.name,
        "pid": None,
        "instance": instance,
        "path": path,
        "size_bytes": size,
        "mtime": mtime,
        "first_activity": first,
        "last_activity": last,
        "activity": spans,
        "bucket_seconds": bucket_seconds,
        # См. пояснение в summarize_builds: вид активности и пороги цели.
        "activity_kind": "activity",
        "self_timeout_seconds": getattr(spec, "self_timeout_seconds", None),
        "idle_seconds": getattr(spec, "idle_seconds", None),
        "summary_at": time.time(),
    }


def summarize_gradle(path: str, pid: int | None = None,
                     version: str | None = None) -> dict[str, Any]:
    """Сводка по логу Gradle-демона."""
    return summarize_builds(path, builtin_targets().get("gradle"), pid=pid,
                            version=version)


def summarize_kotlin(path: str, instance: str | None = None) -> dict[str, Any]:
    """Сводка по логу Kotlin-демона."""
    return summarize_activity(path, builtin_targets().get("kotlin"),
                              instance=instance)


def effective_canceled(record: dict[str, Any]) -> float | None:
    """Момент отмены сборки — если демон после неё действительно «завис».

    Та же логика, что в рейпере (reaper.collect.canceled_state): если после
    отмены началась новая сборка, клиент вернулся, и демон не завис —
    отмена обесценивается.
    """
    canceled = record.get("canceled_at")
    if canceled is None:
        return None
    for build in record.get("builds") or []:
        if float(build[0]) > float(canceled):
            return None
    return float(canceled)


def intervals(record: dict[str, Any]) -> list[tuple[float, float]]:
    """Интервалы активности демона из сводки — единая модель для всех видов.

    Два вида моделей:
      * «builds» — интервалы сборок (демон пишет маркеры начала/конца);
      * «activity» — корзины активности (признак активности — mtime лога).

    Какой — решает поле `activity_kind` в сводке. Раньше модель выбиралась по
    наличию ключа в словаре, то есть по содержанию: смена формы сводки
    тихо ломала бы разбор истории, и заметить это можно было бы только
    по испортившимся цифрам. У старых записей поля нет — там разбираемся
    по содержанию, как раньше (история уже накоплена, выбрасывать её нельзя).
    """
    out: list[tuple[float, float]] = []
    model = record.get("activity_kind")
    if model is None:
        # Старая запись: определяем модель по содержанию.
        model = "builds" if record.get("builds") else "activity"
    if model == "builds":
        for build in record.get("builds") or []:
            start = float(build[0])
            end = build[1]
            if end is None:
                # Сборка без конца: клиент отвалился. Демон «завис» — ждёт
                # (неизвестно, сколько) и никогда не станет idle.
                out.append((start, float("inf")))
            else:
                out.append((start, float(end)))
        return out
    bucket = float(record.get("bucket_seconds") or DEFAULT_BUCKET_SECONDS)
    for span in record.get("activity") or []:
        start = float(span[0])
        out.append((start, start + bucket))
    return out


def append_history(path: str, record: dict[str, Any]) -> None:
    """Дописывает сводку в JSONL-историю."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_history(path: str) -> list[dict[str, Any]]:
    """Читает историю сводок.

    Разбор журнала — общий (`corpus.read_jsonl`): иначе прибор измерения и
    симулятор читали бы один и тот же файл по-разному, и их цифры
    разошлись бы из-за мелочи вроде битой строки.
    """
    return read_jsonl(path)


def history_keys(path: str) -> set[tuple[str, float]]:
    """Ключи уже сохранённых сводок: (путь, mtime файла).

    Нужны, чтобы повторный прогон архивирования не засорил историю
    дубликатами: симулятор считает каждую запись, и копии одного демона
    исказили бы и число убийств, и долю ложных срабатываний.
    """
    keys: set[tuple[str, float]] = set()
    for record in read_history(path):
        path_key = record.get("path")
        if path_key is None:
            continue
        keys.add((str(path_key), float(record.get("mtime") or 0.0)))
    return keys


def summarize_by_path(path: str, kind: str | None = None,
                      targets: TargetSet | None = None
                      ) -> dict[str, Any] | None:
    """Сводка по логу демона — вид лога берётся из набора целей.

    Если вид известен (targets.yaml или встроенные цели) — сводка строится по
    его спецификации: маркеры сборок или корзины активности. Если вид не
    известен — вид лога определяется по имени файла, как раньше.

    Имя лога Gradle — `daemon-<pid>.out.log`, лога Kotlin —
    `kotlin-daemon.<дата>.<время>-<инстанс>.<N>.log`.
    """
    target_set = targets or builtin_targets()
    spec = target_set.get(kind) if kind else None
    if spec is not None:
        try:
            if spec.activity == ACTIVITY_MARKERS:
                return summarize_builds(path, spec,
                                        pid=_pid_for(path, spec))
            return summarize_activity(path, spec,
                                      instance=_instance_for(path, spec))
        except OSError:
            return None

    name = os.path.basename(path)
    if kind == "gradle" or name.startswith("daemon-"):
        version = os.path.basename(os.path.dirname(path))
        pid = pid_from_log_name(path)
        try:
            return summarize_gradle(path, pid=pid, version=version)
        except OSError:
            return None
    if name.startswith("kotlin-daemon."):
        instance = instance_from_log_name(path)
        try:
            return summarize_kotlin(path, instance=instance)
        except OSError:
            return None
    return None


def _pid_for(path: str, spec: Any) -> int | None:
    """PID демона — по описанию цели (log_pid_regex).

    У цели PID в имени лога может быть настоящим (Gradle), а может и нет
    (Kotlin) — тогда это «неизвестно», а не «демон без PID».
    """
    return pid_for(spec, path)


def _instance_for(path: str, spec: Any) -> str | None:
    """Инстанс демона — по описанию цели, а не по имени файла.

    У цели может быть log_group_regex с именованной группой «group»: в ней
    инстанс (у Kotlin — метка инстанса, у неё ротация файлов .00/.01).
    Если регулярки нет — берём имя файла, как раньше.
    """
    return instance_of(spec, path)


def archive_daemon_log(path: str, kind: str | None, history_path: str,
                       death_observed_at: float | None = None,
                       targets: TargetSet | None = None,
                       last_seen_at: float | None = None) -> bool:
    """Сохраняет сводку по логу демона в историю. True — сохранили.

    Нужно для демонов, которых больше нет: их логи в `/tmp` исчезают при
    перезагрузке, и без сводки демон исчезает из истории навсегда.

    `death_observed_at` — момент, когда мы заметили, что демона нет. Для
    демона, который просто простаивал и потом умер, это единственный
    источник знания о его смерти: лог молчит с момента последней записи,
    и без этой отметки простой не виден вовсе (симулятор посчитал бы
    демона умершим сразу после последней активности).

    `last_seen_at` — момент, когда демона видели В ПОСЛЕДНИЙ РАЗ (конец
    предыдущего свипа). Между ним и `death_observed_at` демон умер, но
    когда именно — неизвестно. Нижняя граница нужна, чтобы оценивать простой
    честно: считать, что демон жил до момента обнаружения, — это завышать
    его простой (и выглядеть так, будто он простаивал дольше, чем на самом
    деле). С нижней границей измерение не завышает.
    """
    record = summarize_by_path(path, kind, targets)
    if record is None:
        return False
    if death_observed_at is not None:
        record["death_observed_at"] = float(death_observed_at)
    if last_seen_at is not None:
        record["last_seen_at"] = float(last_seen_at)
    append_history(history_path, record)
    return True


def default_history_path(state_dir: str | None = None) -> str:
    """Путь к истории по умолчанию: рядом с состоянием рейпера."""
    base = state_dir or os.path.expanduser("~/.local/state/java-reaper")
    return os.path.join(base, "logs-history.jsonl")
