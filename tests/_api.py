"""Единая точка доступа к API пакета reaper для тестов.

Тесты пишутся как `reaper.classify(...)`, `reaper.Daemon(...)` — этот модуль
собирает плоское пространство имён из пакетных модулей.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from reaper.classify import classify, evaluate  # noqa: E402,F401
from reaper.collect import (  # noqa: E402,F401
    canceled_state,
    collect_daemons,
    peer_key,
)
from reaper.config import (  # noqa: E402,F401
    DEFAULTS,
    RULES_VERSION,
    allowlist,
    disabled_flag,
    expand,
    idle_limit,
    load_config,
)
from reaper.execute import (  # noqa: E402,F401
    execute,
    open_pidfd,
    send_signal,
    still_same,
)
from reaper.housekeeping import trim_idle_logs  # noqa: E402,F401
from reaper.logparse import (  # noqa: E402,F401
    Build,
    FileScan,
    MARK_BUILD_BYTES,
    MARK_BUILD_END,
    MARK_BUILD_END_ALT,
    MARK_BUILD_START,
    MARK_IDLE_BYTES,
    parse_timestamp,
    scan_file,
)
from reaper.logs import (  # noqa: E402,F401
    parse_gradle_log,
    scan_gradle_log,
    scan_markers,
)
from reaper.model import (  # noqa: E402,F401
    Action,
    CHILD_IGNORE_EXECUTABLES,
    Daemon,
    Decision,
    PeerConn,
    Protector,
    observed_activity,
)
from reaper.policy import Rule, Signals, default_policy  # noqa: E402,F401
from reaper.proc import (  # noqa: E402,F401
    children_map,
    daemon_kind,
    find_log_path,
    find_socket_owners,
    inode_owner_map,
    is_ignored_child,
    is_loopback,
    parse_ts,
    proc_comm,
    proc_cmdline,
    read_memory,
    read_proc_stat,
    socket_inodes,
    tcp_table,
)
from reaper.report import (  # noqa: E402,F401
    JSON_SCHEMA_VERSION,
    PILOT_MAX_REUSE_RATE,
    PILOT_MIN_EVENTS,
    audit_report,
    canary,
    decision_to_dict,
    format_explain,
    format_json,
    format_status,
    health,
    kind_summary,
    pilot_report,
)
from reaper.state import (  # noqa: E402,F401
    DAEMON_FIELDS,
    GLOBAL_FIELDS,
    STATE_VERSION,
    UNMEASURED_RULES,
    append_kill,
    degradation_warnings,
    kills_path,
    load_state,
    measure_coverage,
    note_rule_hits,
    note_stale_peers,
    read_kills,
    record_freed,
    save_state,
    state_path,
    sync_rules_version,
    update_metric,
)

# Внутреннее имя из ранних версий.
_is_loopback = is_loopback
