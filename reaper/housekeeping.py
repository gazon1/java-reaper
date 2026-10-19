"""Хозяйственные операции: уборка разросшихся логов простаивающих демонов.

Зачем: простаивающий Gradle-демон пишет в лог DEBUG-спам (~124 Б/с, то есть
~10 МБ в сутки на демон). Уровень логирования на это не влияет — проверено
замером: прирост лога одинаков при default/info/lifecycle (22 356 Б за 180 с).

Почему усекаем, а не меняем настройки: `org.gradle.logging.level` управляет
консолью клиента, но не внутренним логом демона — маркеры и спам пишутся на
одном уровне. Лог открыт демоном в режиме дописывания, поэтому truncate
безопасен: демон продолжает писать в тот же файл.

Логи Kotlin-демонов не трогаем: у них mtime лога — сам признак простоя,
усечение сдвинуло бы mtime и демон выглядел бы «только что активным».
"""

from __future__ import annotations

import os
from typing import Any

from .model import Decision


def trim_idle_logs(decisions: list[Decision], cfg: dict[str, Any],
                   now: float) -> list[dict[str, Any]]:
    """Усекает логи демонов, которые простаивают дольше TRIM_AFTER_SECONDS.

    По умолчанию выключено (TRIM_IDLE_LOGS=0) — это запись в чужие файлы,
    включать её нужно осознанно.
    """
    report: list[dict[str, Any]] = []
    if not int(cfg.get("TRIM_IDLE_LOGS", 0)):
        return report
    after = float(cfg.get("TRIM_AFTER_SECONDS", 3600))
    for dec in decisions:
        d = dec.daemon
        if d.kind != "gradle" or not d.log_path:
            continue
        if dec.idle_seconds is None or dec.idle_seconds < after:
            continue
        try:
            size = os.path.getsize(d.log_path)
        except OSError:
            continue
        if size < 1024 * 1024:
            continue  # мелочь не трогаем
        try:
            fd = os.open(d.log_path, os.O_WRONLY)
            try:
                os.ftruncate(fd, 0)
            finally:
                os.close(fd)
        except OSError as exc:
            report.append({"pid": d.pid, "action": "trim failed",
                           "reason": f"{d.log_path}: {exc}"})
            continue
        report.append({"pid": d.pid, "action": "trim",
                       "reason": f"усечён лог простаивающего демона "
                                 f"({size // 1024 // 1024} МБ → 0)"})
    return report
