"""Описание одной цели: как узнать демона, где его лог, по каким маркерам судить о работе.

Это «лист характеристик» вида демона: он ничего не знает про остальные цели
и ничего не делает — только хранит признаки и отвечает на два вопроса
(«похож ли этот процесс на цель» и «какие виды демонов цель защищает»).

Разделение по смыслу: описание отдельно от реестра целей
(`reaper.target_registry`) и от разбора файла целей
(`reaper.target_yaml`) — чтобы добавление нового поля в цель не задевало
ни чтение YAML, ни набор целей.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import ACTIVITY_MTIME

# Формат таймстемпа в начале строки лога.
TS_ISO = "iso"        # «2026-10-19T19:36:17.864+0300» — с смещением
TS_LOCAL = "local"    # «2026-10-20 08:55:16.690» — местное время, без смещения


@dataclass(frozen=True)
class TargetSpec:
    """Описание одного вида демонов.

    name           — ключ вида (`kind` в демоне, в состоянии и в отчётах);
    label          — человекочитаемое имя для отчётов;
    match_cmdline  — подстрока в cmdline, по которой узнаём процесс;
    match_comm     — имя исполняемого файла (команды) процесса;
    log_name_regex — регулярка на имя файла лога (ищем среди открытых fd);
    activity       — «mtime» или «markers» (см. константы выше);
    ts_format      — формат таймстемпа в строке лога («iso»/«local»);
    build_start    — маркер начала работы (для activity=markers);
    build_end      — маркер конца работы;
    build_end_alt  — запасной маркер конца работы;
    cancel         — маркер «клиент отвалился» (даёт признак «завис»);
    idle_seconds   — порог простоя (None → берётся из плоского конфига);
    recreate_cost_seconds — цена холодного старта (None → плоский конфиг);
    build_active_seconds — окно «свежей» сборки: пока идёт сборка, начавшаяся
                     не позже, защищать защищаемые виды (None → плоский конфиг);
    stuck_after_seconds — через сколько после отмены сборки демон «завис»;
    min_footprint_mb — ниже этого демон не стоит пересоздания (None → конфиг);
    large_daemon_mb  — крупнее этого демона убираем в любом случае;
    self_timeout_seconds  — через сколько демон умирает сам (Gradle: 180 мин);
    protects       — какие виды демонов этот вид защищает, пока работает;
    rebuildable    — «это пересоздаваемый кэш, его можно убить»;
    log_trim       — «лог этого демона можно усекать» (усечение сдвигает
                     mtime, а у целей, где mtime — признак простоя, это
                     врёт: демон выглядел бы «только что активным»);
    log_globs      — где на диске лежат логи цели (шаблоны пути);
    log_group_regex— как файлы одного демона группируются (именованная
                     группа «group»): у Kotlin это инстанс, у него ротация
                     .00/.01, и открытый файл защищает всю группу;
    log_pid_regex  — PID в имени файла (именованная группа «pid»): у Gradle
                     он настоящий, у Kotlin — нет;
    forbidden_prefixes — имена, которые трогать нельзя никогда (маркеры
                     Kotlin: их создаёт и удаляет сам Kotlin);
    bucket_seconds — ширина «корзины» активности для целей с activity=mtime
                     (0 → значение по умолчанию).
    """

    name: str
    label: str = ""
    match_cmdline: str = ""
    match_comm: str = ""
    log_name_regex: str = ""
    activity: str = ACTIVITY_MTIME
    ts_format: str = TS_ISO
    build_start: str = ""
    build_end: str = ""
    build_end_alt: str = ""
    cancel: str = ""
    idle_seconds: float | None = None
    recreate_cost_seconds: float | None = None
    build_active_seconds: float | None = None
    stuck_after_seconds: float | None = None
    min_footprint_mb: int | None = None
    large_daemon_mb: int | None = None
    self_timeout_seconds: float | None = None
    protects: tuple[str, ...] = ()
    rebuildable: bool = False
    log_trim: bool = False
    log_globs: tuple[str, ...] = ()
    log_group_regex: str = ""
    log_pid_regex: str = ""
    forbidden_prefixes: tuple[str, ...] = ()
    bucket_seconds: int = 0

    def matches(self, cmdline: str, comm: str = "") -> bool:
        """Похоже ли этот процесс на нашу цель."""
        if self.match_cmdline and self.match_cmdline in cmdline:
            return True
        if self.match_comm and comm == self.match_comm:
            return True
        return False

    def protects_kind(self, kind: str) -> bool:
        """Защищает ли наша цель данный вид демонов."""
        return kind in self.protects
