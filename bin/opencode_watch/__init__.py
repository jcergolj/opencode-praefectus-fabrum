"""Public watcher API.

The package is split by responsibility, while this module keeps the original
single-module import surface used by the executable and tests.
"""

import time
from typing import Callable, Optional, Sequence

from . import cli as _cli
from .cli import SnapshotStreamer
from .config import (
    FOCUS_CYCLE_STATE_FILE,
    PROCESS_START_TOLERANCE,
    RUNTIME_DIR,
    STATUS_RECORD_PATH,
)
from .cycling import (
    ALL_SESSIONS_BUCKET,
    FOCUS_STATE_ALIASES,
    FocusCycleStore,
    SessionSelector,
    focus_candidates_for_bucket,
    focus_candidates_for_state,
    focus_session_for_bucket,
    focus_session_for_state,
    focus_target_for_bucket,
    focus_target_for_state,
    session_identity,
    snapshot_signature,
)
from .domain import (
    AttentionSource,
    AttentionSessionState,
    DEFAULT_PREVIEWS,
    EMPTY_SESSION_COUNTS,
    FocusTarget,
    IdleSessionState,
    InvalidTransition,
    NeedsApprovalSessionState,
    ProcessInfo,
    ProcessSource,
    Session,
    SessionSource,
    SessionState,
    SessionStateFactory,
    SessionStateMachine,
    SessionStateRegistry,
    SessionStatus,
    SnapshotSource,
    StateFactory,
    STATUS_COUNT_BUCKETS,
    TerminalSource,
    TmuxPane,
    WaitingSessionState,
    WorkingSessionState,
)
from .focus import (
    FocusService,
    HyprlandClient,
    TmuxClient,
    command_succeeded,
    parse_pid,
)
from .snapshots import SessionCollector, SessionFactory, SnapshotService
from .sources import (
    AttentionStateReader,
    ProcProcessSource,
    process_stat,
    read_boot_time,
)


def build_runtime():
    """Build the default runtime while preserving the config override seam."""

    original_status_record_path = _cli.STATUS_RECORD_PATH
    _cli.STATUS_RECORD_PATH = STATUS_RECORD_PATH
    try:
        return _cli.build_runtime()
    finally:
        _cli.STATUS_RECORD_PATH = original_status_record_path


def main(
    argv: Optional[Sequence[str]] = None,
    snapshot_source: Optional[SnapshotSource] = None,
    focus_service: Optional[FocusService] = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Optional[Callable[[str], None]] = None,
) -> int:
    """Run the CLI while preserving the original module-level config seam."""

    original_status_record_path = _cli.STATUS_RECORD_PATH
    original_cycle_state_file = _cli.FOCUS_CYCLE_STATE_FILE
    _cli.STATUS_RECORD_PATH = STATUS_RECORD_PATH
    _cli.FOCUS_CYCLE_STATE_FILE = FOCUS_CYCLE_STATE_FILE
    try:
        return _cli.main(
            argv=argv,
            snapshot_source=snapshot_source,
            focus_service=focus_service,
            sleep=sleep,
            emit=emit,
        )
    finally:
        _cli.STATUS_RECORD_PATH = original_status_record_path
        _cli.FOCUS_CYCLE_STATE_FILE = original_cycle_state_file


__all__ = [
    "ALL_SESSIONS_BUCKET",
    "AttentionSource",
    "AttentionSessionState",
    "AttentionStateReader",
    "DEFAULT_PREVIEWS",
    "EMPTY_SESSION_COUNTS",
    "FOCUS_CYCLE_STATE_FILE",
    "FOCUS_STATE_ALIASES",
    "FocusCycleStore",
    "FocusService",
    "FocusTarget",
    "HyprlandClient",
    "IdleSessionState",
    "InvalidTransition",
    "NeedsApprovalSessionState",
    "PROCESS_START_TOLERANCE",
    "ProcProcessSource",
    "ProcessInfo",
    "ProcessSource",
    "RUNTIME_DIR",
    "STATUS_COUNT_BUCKETS",
    "STATUS_RECORD_PATH",
    "Session",
    "SessionCollector",
    "SessionFactory",
    "SessionSelector",
    "SessionSource",
    "SessionState",
    "SessionStateFactory",
    "SessionStateMachine",
    "SessionStateRegistry",
    "SessionStatus",
    "SnapshotService",
    "SnapshotSource",
    "SnapshotStreamer",
    "StateFactory",
    "TerminalSource",
    "TmuxClient",
    "TmuxPane",
    "WaitingSessionState",
    "WorkingSessionState",
    "build_runtime",
    "command_succeeded",
    "focus_candidates_for_bucket",
    "focus_candidates_for_state",
    "focus_session_for_bucket",
    "focus_session_for_state",
    "focus_target_for_bucket",
    "focus_target_for_state",
    "main",
    "parse_pid",
    "process_stat",
    "read_boot_time",
    "session_identity",
    "snapshot_signature",
]
