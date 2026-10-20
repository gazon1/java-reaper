# RUNBOOK: java-reaper на рабочей машине

Короткая памятка: что делать, если что-то пошло не так. Основные правила
и обоснования — в [README.md](README.md).

## Как узнать, что происходит

```bash
java-reaper --status          # таблица демонов с решениями (ничего не меняет)
java-reaper --status --json   # то же машинно-читаемо: защиты, peer'ы, возраст
java-reaper --health          # «таймер молча не работает?» + метрики
java-reaper --pilot           # вердикт по пилоту: можно ли включать kill-режим
java-reaper --audit           # демоны с простоями длиннее IDLE_SECONDS
java-reaper --explain         # таблица правил политики
journalctl --user -u java-reaper.service -n 50   # что делал рейпер
tail -20 ~/.local/state/java-reaper/watchdog.log # что делал сторож (crond)
```

Отчёт за месяц:

```bash
python3 ~/dev/java-reaper/tools/monthly-report.py
```

Уборка логов мёртвых демонов (по умолчанию — только план):

```bash
python3 ~/dev/java-reaper/tools/prune-logs.py                # план
python3 ~/dev/java-reaper/tools/prune-logs.py --apply        # удалить
python3 ~/dev/java-reaper/tools/prune-logs.py --archive-only # только сводки
```

Симуляция правил по накопленной истории — «что бы мы убили и сколько ошиблись»,
за минуты вместо недель пилота:

```bash
python3 ~/dev/java-reaper/tools/simulate.py
python3 ~/dev/java-reaper/tools/simulate.py --thresholds "30,60,120,180"
python3 ~/dev/java-reaper/tools/simulate.py --stuck-thresholds "15,30,60,120"
```

Скан порога «зависшего» — отдельная ось: зависших демонов убирает рейпер,
а не Gradle, поэтому порог измеряется отдельно.

## Штатные состояния

| Сообщение | Что значит | Что делать |
|---|---|---|
| `лог не найден — защищаю` | демон не держит свой лог (запущен не через Gradle) | ничего: демон под защитой, но и не считается кандидатом |
| `история лога неизвестна — защищаю` | лог больше предела разбора и маркеры не найдены | проверить `LOG_SCAN_MAX_BYTES`; демон останется нетронутым |
| `нет baseline (первый свип) — защищаю` | демонseen впервые, не с чем сравнивать CPU | ничего: со второго свипа baseline появится |
| `peer: клиент сборки (pid N, имя)` | к демону подключён живой клиент | ничего: клиент наблюдает за демоном |
| `peer: занят (pid N, имя)` | клиент — другой демон, который сейчас работает | ничего |
| `peer: соединение с неопределённым владельцем` | локальный клиент, чей процесс не виден | ничего: «неизвестное ⇒ оставить» |
| `peer: соединение висит N ч … клиент не считается наблюдателем` | соединение старше `PEER_MAX_AGE_SECONDS` | зависший клиент; демон может быть убит |
| `есть живые дети: N` | у демона есть работающий дочерний процесс | ничего: демон занят |
| `занят: CPU X с за свип` | демон работал | ничего |
| `PID вне списка разрешённых — оставляю` | сработал `PID_ALLOWLIST` | снять PID из списка, если нужно |
| `превышен лимит N убийств за свип` | страховка `MAX_KILLS_PER_SWEEP` | обычно признак сбоя правил — смотреть `--status` |
| `идёт сборка Gradle — Kotlin-демоны не трогаю` | правило безопасности (сборка свежая, началась недавно) | ничего |
| `свободной памяти хватает (N МБ), демон небольшой — не трогаю` | память не нужна — убивать незачем | ничего; если демоны жрут память, снизить `MEMORY_PRESSURE_MB` или поднять `KILL_LARGE_DAEMON_MB` |
| `WARNING: кандидатов 0, но есть java-процессы со словом gradle/kotlin` | детектор перестал узнавать демонов | чинить: скорее всего изменился main-класс или формат логов |

## Аварийный тормоз

Рейпер ничего не делает, если создан файл-флаг:

```bash
touch ~/.config/java-reaper/disabled
```

Снять: `rm ~/.config/java-reaper/disabled`.

Если нужно остановить и таймер:

```bash
systemctl --user stop java-reaper.timer
systemctl --user enable --now java-reaper.timer   # вернуть
```

## «Демон убит посреди сборки»

Симптомы: сборка падает с `Could not connect to Gradle daemon` /
`Connection refused` / «daemon disappeared unexpectedly».

Что произошло: рейпер решил, что демон простаивает дольше `IDLE_SECONDS`
(по умолчанию 6 ч), и отправил ему SIGTERM. Демон завершился штатно
(в его логе появится `Daemon vm is shutting down...`).

Что делать:

1. **Ничего не чинить не нужно.** Следующая сборка поднимет новый демона.
   Цена — холодный старт: замерено на этой машине **+39 с** к первой сборке
   после убийства (41.6 с против 2.4 с тёплой). Дальше демон снова тёплый.
2. Проверить, что это был штатный рейпер, а не падение:
   ```bash
   journalctl --user -u java-reaper.service --since "30 min ago" | grep -E "SIGTERM|SIGKILL"
   ```
   Если строк нет — демон упал сам, и причина другая (OOM, краш JVM).
3. Проверить, что демон действительно простаивал:
   ```bash
   java-reaper --status --json | python3 -m json.tool | grep -A5 "<pid>"
   ```
4. Если убийств быть не должно — поднять порог или снять демона с прицела:
   ```bash
   # ~/.config/java-reaper/config
   IDLE_SECONDS=21600      # 6 ч (значение по умолчанию)
   PID_ALLOWLIST=12345     # или: только этот PID можно убивать
   ```
   Далее `systemctl --user daemon-reload` не нужен — конфиг читается на каждом
   свипе.

## «Рейпер ничего не убивает, а памяти мало»

С 2026-10-20 уборка зависит от того, нужна ли машине память: при достаточной
свободной памяти небольшие демоны не трогаются (правило `memory-not-needed`).

```bash
java-reaper --status --json | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('свободно памяти:', d['memory']['available_mb'], 'МБ')
print('порог давления:', d['memory']['pressure_mb'], 'МБ')
print('крупный демон:', d['memory']['large_daemon_mb'], 'МБ')"
```

Если демоны занимают много, а убирать их надо всегда:

```bash
# ~/.config/java-reaper/config
MEMORY_PRESSURE_MB=0      # «всегда убирать», независимо от свободной памяти
```

Обратная настройка — `MEMORY_PRESSURE_MB=4096` (по умолчанию): убивать, только
когда свободно меньше 4 ГБ. Крупные демоны (≥ `KILL_LARGE_DAEMON_MB`, 4 ГБ)
убираются в любом случае.

## Kotlin-демон остался один

Kotlin-демон — дочерний процесс Gradle-демона. Штатный порядок: Gradle-демон
получает SIGTERM → его shutdown-hook удаляет клиентский маркер → Kotlin-демон
видит «клиентов нет» и выходит сам через ~1 с (проверено на изолированной паре).

Если Kotlin-демон остался — значит клиент умер нечитно (маркер уцелел) и
демон считает себя живым. Рейпер убьёт его сам через `IDLE_SECONDS` простоя.
Срочно — вручную:

```bash
java-reaper --mode=kill --config <конфиг с PID_ALLOWLIST этого pid>
```

Маркеры клиентов (`/tmp/kotlin-compiler-client-*-is-running`) **не удалять
никогда**: у живого клиента `bootRun` маркер держится сутками, и удаление
оборвало бы сессию компиляции.

## Таймер молчит

```bash
java-reaper --health
```

- `последний свип: никогда` — таймер не запущен:
  `systemctl --user enable --now java-reaper.timer`.
- `свип старше двух интервалов` — таймер не срабатывает. Проверить:
  ```bash
  systemctl --user list-timers java-reaper.timer
  systemctl --user list-jobs        # зависшие job'ы означают зависший менеджер
  ```
  Известный сбой: user-менеджер уходит в busy-loop (1 поток, ~96 % одного
  ядра), job'ы висят в `waiting`, `systemctl --user start` не возвращается.
  Лечится без остановки юнитов:
  ```bash
  systemctl --user daemon-reexec
  ```
- После логаута таймер останавливается: нужен `loginctl enable-linger max`
  (требует пароль администратора).

### Сторож: страховка вне user-менеджера

Если user-менеджер завис (см. выше), умирают все user-юниты, включая таймер.
Сторож работает через `crond` — системный сервис — и потому переживает такую
смерть. Он раз в 5 минут проверяет, идут ли свипы, и при необходимости
выполняет свип сам.

```bash
crontab -l | grep java-reaper          # сторож установлен?
tail -20 ~/.local/state/java-reaper/watchdog.log
```

| сообщение | Что значит |
|---|---|
| `СТАТУС: ок, последний свип N мин назад` | всё в порядке |
| `СТАТУС: ПОЛОМАНО — …` | рейпер замолчал; дальше либо `починил`, либо `починить не удалось` |
| `СТАТУС: ОТКЛЮЧЁН флагом …` | сработал аварийный тормоз; сторож намеренно молчит |

Если сторож не установлен:

```bash
crontab -l > /tmp/reaper-cron.txt
cat >> /tmp/reaper-cron.txt <<'EOF'
*/5 * * * * /usr/bin/python3 /home/max/dev/java-reaper/tools/watchdog.py >> /home/max/.local/state/java-reaper/watchdog.log 2>&1
EOF
crontab /tmp/reaper-cron.txt
```

## Что рейпер убил

Каждое фактическое убийство записано в журнал:

```bash
cat ~/.local/state/java-reaper/kills.jsonl          # одна JSON-запись на убийство
python3 ~/dev/java-reaper/tools/monthly-report.py  # сводка за месяц
```

Запись: `at` (время), `pid`, `action` (SIGTERM/SIGKILL), `reason` (почему),
`footprint_mb` (сколько памяти освободили), `kind` (gradle/kotlin).

Сводка «сколько ложных срабатываний» — в `--pilot`: доля reuse из метрики
пилота против критерия ≤ 60 % при ≥ 8 событиях.

## История демонов (сводки умерших)

Рейпер сам сохраняет сводку по демону, которого больше нет, — пока его лог
ещё жив (логи Kotlin лежат в `/tmp` и исчезают при перезагрузке):

```bash
wc -l ~/.local/state/java-reaper/logs-history.jsonl   # сколько демонов в истории
python3 ~/dev/java-reaper/tools/simulate.py            # что бы мы убили по этой истории
```

Момент пропажи демона записывается как его смерть (`death_observed_at`) —
иначе простой виден только по последней записи в логе, а у Kotlin её почти
нет.

## Логи демонов растут

Простаивающий Gradle-демон пишет в лог DEBUG-спам (~124 Б/с, ≈10 МБ в сутки).
Уровень логирования на это **не влияет** — проверено: прирост одинаков при
`default`, `info` и `lifecycle` (22 356 Б за 180 с). Лог открыт в режиме
дописывания, поэтому его можно усекать:

```bash
# ~/.config/java-reaper/config
TRIM_IDLE_LOGS=1
TRIM_AFTER_SECONDS=3600
```

Рейпер усечёт лог демона, простаивающего дольше часа (только Gradle: у Kotlin
mtime лога — сам признак простоя, его трогать нельзя). Историю сборок рейпер
держит в своём состоянии, поэтому усечение не мешает его решениям.

## Мёртвые логи (1.2 ГБ в ~/.gradle/daemon, 108 МБ в /tmp)

Gradle и Kotlin свои логи не чистят — растёт бесконечно.

```bash
python3 ~/dev/java-reaper/tools/prune-logs.py           # план: что удалим
python3 ~/dev/java-reaper/tools/prune-logs.py --apply   # удалить
```

Что удаляется: логи процессов, которых больше нет. Проверка «файл открыт
каким-либо процессом» — главная (пока демон жив, он держит лог открытым);
для Gradle дополнительно сверяется PID из имени файла.

**Чего инструмент не делает:** не трогает маркеры Kotlin
(`kotlin-compiler-client-*-is-running`, `kotlin-compiler-in-*.alive`,
`kotlin-compilation-session-*-is-running`) — их создаёт и удаляет сам Kotlin;
не трогает логи живых демонов (у Kotlin живой демон пишет в несколько
файлов `.00/.01/.02`, открыт только последний — открытый файл защищает всю
группу).

Если план выглядит неправдоподобно (например, в списке лог живого демона) —
не применять, разбираться вручную.

## Рейпер молчит, но выглядит здоровым

Если `--status` показывает «оставить» у всех, а выглядит это нормально —
проверьте наблюдаемость:

```bash
java-reaper --status --json | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('правила за свип:', d['rule_hits_last'])
print('измерено демонов:', d['measured_count'], 'из', len(d['daemons']))
print('деградация:', d['degradation'])"
```

- `rule_hits_last` пустой или весь в `unknown-history` / `no-baseline` —
  разбор логов сломался (сменился формат Gradle), и рейпер ничего не может
  оценить;
- `degradation` непуст — детекция деградировала (см. предупреждения в
  `--health` и в журнале свипа);
- в `metric` много `unknown` — свипов не было (машина выключена), и события
  закрылись без наблюдения; они не входят в знаменатель.

## Откат

Всё обратимо: рейпер не меняет ни репозитории, ни `gradle.properties`,
ни клиентские маркеры Kotlin.

```bash
systemctl --user disable --now java-reaper.timer
rm -f ~/.config/java-reaper/disabled
rm -rf ~/.local/state/java-reaper        # состояние и метрики
rm ~/.local/bin/java-reaper              # симлинк
rm -rf ~/dev/java-reaper                 # сам инструмент
```

## Полезные однострочники

```bash
# что занимает память сейчас
java-reaper --status --json | python3 -c "
import json,sys
d=json.load(sys.stdin)
for x in sorted(d['daemons'], key=lambda x:-x['footprint_mb']):
    print(f\"{x['pid']:>8} {x['kind']:>7} {x['footprint_mb']:>6} МБ  простой {int(x['idle_seconds']/60):>5} мин  {x['reason'][:50]}\")"

# сводка по классам демонов (сколько памяти держит Gradle, сколько Kotlin)
java-reaper --status --json | python3 -c "
import json,sys
d=json.load(sys.stdin)
for k,v in d['kinds'].items():
    print(f\"{k:>7}: {v['count']} шт, {v['footprint_mb']} МБ, под удар {v['kill_footprint_mb']} МБ, порог {v['idle_limit_seconds']/3600:.0f} ч\")"

# ретроспектива: какой порог простоя оптимален (по самим логам)
./tools/analyze-idle.py --thresholds "30,60,120,180,360,720"

# то же по накопленной истории сводок (быстрее: логи уже разобраны)
./tools/simulate.py --thresholds "30,60,120,180,360,720"

# ретроспектива: какой порог «зависшего» оптимален
./tools/simulate.py --stuck-thresholds "15,30,60,120,240"

# сколько свободной памяти сейчас (то же, что видит рейпер)
grep MemAvailable /proc/meminfo

# что под ударом по журналу свипов (эффект рейпера в непрерывном измерении)
python3 ~/dev/java-reaper/tools/monthly-report.py | sed -n '/Картина/,/^$/p'
```
