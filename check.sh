#!/usr/bin/env bash
# Единая точка проверки рейпера: одна команда — и видно, что всё в порядке.
#
# Зачем она нужна: у проекта есть тесты, приборы и инструменты, и каждый раз
# проверять их по отдельности — это время и chance забыть. Здесь собраны все
# проверки в один запуск с понятным итогом.
#
# Что проверяется:
#   1. синтаксис — все файлы компилируются;
#   2. тесты — полный набор;
#   3. живой свип — рейпер в режиме dry-run на этой машине;
#   4. инструменты — каждый отрабатывает без ошибки.
#
# Ничего разрушительного: режим dry-run по умолчанию, kill-режим не включается.
#
# Использование:
#   ./check.sh              — всё
#   ./check.sh --fast       — без живого свипа и инструментов (только тесты)
#   ./check.sh --no-tests   — без тестов

set -uo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
FAST=0
RUN_TESTS=1
FAILED=0
STEP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --fast) FAST=1 ;;
        --no-tests) RUN_TESTS=0 ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *)
            echo "неизвестный аргумент: $1" >&2
            exit 2
            ;;
    esac
    shift
done

step() {
    STEP=$((STEP + 1))
    printf '\n[%d] %s\n' "$STEP" "$1"
}

ok() {
    printf '    OK: %s\n' "$1"
}

fail() {
    printf '    СБОЙ: %s\n' "$1" >&2
    FAILED=$((FAILED + 1))
}

# --------------------------------------------------------------------------- #
step "Синтаксис: все файлы компилируются"
# --------------------------------------------------------------------------- #
if "$PY" -m compileall -q reaper tools tests >/dev/null 2>&1; then
    ok "reaper/, tools/, tests/ компилируются"
else
    "$PY" -m compileall -q reaper tools tests
    fail "компиляция не прошла"
fi

# --------------------------------------------------------------------------- #
step "Тесты"
# --------------------------------------------------------------------------- #
if [ "$RUN_TESTS" -eq 1 ]; then
    if "$PY" -m unittest discover -s tests >/tmp/java-reaper-check-tests.log 2>&1; then
        ok "набор тестов пройден ($(grep -o 'Ran [0-9]* tests' /tmp/java-reaper-check-tests.log | tail -1))"
    else
        tail -25 /tmp/java-reaper-check-tests.log | sed 's/^/      /'
        fail "тесты упали"
    fi
else
    ok "пропущено (--no-tests)"
fi

if [ "$FAST" -eq 1 ]; then
    printf '\nИтог: %s\n' "$([ "$FAILED" -eq 0 ] && echo 'все проверки пройдены' || echo "провалено проверок: $FAILED")"
    exit "$FAILED"
fi

# --------------------------------------------------------------------------- #
step "Живой свип (dry-run)"
# --------------------------------------------------------------------------- #
# dry-run по умолчанию: рейпер показывает, что он сделал бы, и ничего не убивает.
if "$PY" -m reaper.cli --mode=dry-run >/tmp/java-reaper-check-sweep.log 2>&1; then
    ok "свип выполнен, вывод: /tmp/java-reaper-check-sweep.log"
    tail -3 /tmp/java-reaper-check-sweep.log | sed 's/^/      /'
else
    tail -5 /tmp/java-reaper-check-sweep.log | sed 's/^/      /'
    fail "живой свип не прошёл"
fi

# --------------------------------------------------------------------------- #
step "Отчёты и приборы"
# --------------------------------------------------------------------------- #
run_tool() {
    local name="$1"
    shift
    if "$@" >/tmp/java-reaper-check-tool.log 2>&1; then
        ok "$name"
    else
        tail -5 /tmp/java-reaper-check-tool.log | sed 's/^/      /'
        fail "$name"
    fi
}

# Сторож возвращает 2, если рейпер отключён флагом. Это законное состояние
# оператора, а не поломка, поэтому оно не считается провалом.
run_watchdog() {
    if "$PY" tools/watchdog.py >/tmp/java-reaper-check-tool.log 2>&1; then
        ok "tools/watchdog.py"
    else
        local rc=$?
        if [ "$rc" -eq 2 ]; then
            ok "tools/watchdog.py (рейпер отключён флагом — это не поломка)"
        else
            tail -5 /tmp/java-reaper-check-tool.log | sed 's/^/      /'
            fail "tools/watchdog.py"
        fi
    fi
}

run_tool "--status" "$PY" -m reaper.cli --status
run_tool "--explain" "$PY" -m reaper.cli --explain
run_tool "--status --json" "$PY" -m reaper.cli --status --json
run_tool "tools/measure-thresholds.py" "$PY" tools/measure-thresholds.py
run_tool "tools/simulate.py" "$PY" tools/simulate.py
run_tool "tools/monthly-report.py" "$PY" tools/monthly-report.py
run_tool "tools/analyze-idle.py --json" "$PY" tools/analyze-idle.py --json
run_tool "tools/prune-logs.py --archive-only" "$PY" tools/prune-logs.py --archive-only --min-age-hours 0
run_watchdog

# Пилот возвращает 2, когда данных для вердикта не хватает. Это его ответ,
# а не поломка: код 2 означает «вердикт не выносим», а не «инструмент упал».
run_pilot() {
    if "$PY" -m reaper.cli --pilot >/tmp/java-reaper-check-tool.log 2>&1; then
        ok "--pilot"
    else
        local rc=$?
        if [ "$rc" -eq 2 ]; then
            ok "--pilot (данных для вердикта пока мало — это его ответ, не сбой)"
        else
            tail -5 /tmp/java-reaper-check-tool.log | sed 's/^/      /'
            fail "--pilot"
        fi
    fi
}
run_pilot

# --------------------------------------------------------------------------- #
step "Приватность: домашнего пути в машинном выводе нет"
# --------------------------------------------------------------------------- #
if "$PY" -m reaper.cli --status --json 2>/dev/null | grep -q "$HOME"; then
    fail "в JSON найден домашний путь — его там быть не должно"
else
    ok "домашнего пути в JSON нет"
fi

# --------------------------------------------------------------------------- #
printf '\nИтог: '
if [ "$FAILED" -eq 0 ]; then
    echo "все проверки пройдены"
    exit 0
fi
echo "провалено проверок: $FAILED"
exit "$FAILED"
