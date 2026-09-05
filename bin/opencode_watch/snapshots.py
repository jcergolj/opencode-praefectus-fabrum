"""Build the stable session snapshot consumed by the QML widget."""

import os
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Union

from .config import PROCESS_START_TOLERANCE
from .domain import (
    AttentionSource,
    DEFAULT_PREVIEWS,
    EMPTY_SESSION_COUNTS,
    ProcessInfo,
    ProcessSource,
    Session,
    SessionSource,
    SessionStateRegistry,
    SessionStatus,
    STATUS_COUNT_BUCKETS,
    TerminalSource,
    TmuxPane,
)


class SessionFactory:
    """Translate process and optional attention data into a UI session."""

    def create(
        self,
        process: ProcessInfo,
        pane: Optional[TmuxPane],
        status_record: Mapping[str, Any],
        observed_state: Optional[Union[SessionStatus, str]] = None,
    ) -> Session:
        current_status = SessionStatus.from_value(observed_state)
        if current_status is None:
            current_status = SessionStatus.from_value(status_record.get("state"))
        current_status = current_status or SessionStatus.IDLE
        return Session(
            session_id=status_record.get("session_id", f"pid:{process.pid}"),
            project=os.path.basename(process.directory) or "OpenCode",
            state=current_status.value,
            tmux_pane=pane.id if pane else None,
            tmux_socket=status_record.get("tmux_socket"),
            source_pid=process.pid,
            directory=process.directory,
            notification_id=status_record.get("notification_id"),
            attention=bool(status_record.get("attention")),
            attention_since=status_record.get("attention_since"),
            last_transition_ts=status_record.get(
                "last_transition_ts", process.started_at
            ),
            preview=status_record.get("preview")
            or DEFAULT_PREVIEWS.get(current_status.value, "idle"),
            context_tokens=status_record.get("context_tokens"),
            context_limit=status_record.get("context_limit"),
            context_percentage=status_record.get("context_percentage"),
        )


class SessionCollector:
    """Collect top-level OpenCode processes and enrich them with UI metadata."""

    def __init__(
        self,
        process_source: ProcessSource,
        attention_source: AttentionSource,
        terminal_source: TerminalSource,
        session_factory: Optional[SessionFactory] = None,
        state_registry: Optional[SessionStateRegistry] = None,
    ):
        self.process_source = process_source
        self.attention_source = attention_source
        self.terminal_source = terminal_source
        self.session_factory = session_factory or SessionFactory()
        self.state_registry = state_registry or SessionStateRegistry()

    def collect(self) -> List[Session]:
        status_records = self.attention_source.read()
        opencode_pids = self.process_source.opencode_pids()
        panes = self.terminal_source.panes()
        sessions: List[Session] = []
        active_pids: Set[int] = set()

        for process_pid in sorted(opencode_pids):
            session = self._collect_process(
                process_pid,
                opencode_pids,
                status_records,
                panes,
            )
            if session is None:
                continue
            active_pids.add(process_pid)
            sessions.append(session)

        self.state_registry.remove_missing(active_pids)
        return sorted(sessions, key=lambda session: session.source_pid)

    def _collect_process(
        self,
        process_pid: int,
        opencode_pids: Set[int],
        status_records: Mapping[int, Mapping[str, Any]],
        panes: List[TmuxPane],
    ) -> Optional[Session]:
        process_ancestors = self.process_source.ancestors(process_pid)
        if any(parent_pid in opencode_pids for parent_pid in process_ancestors[1:]):
            return None

        process = self.process_source.inspect(process_pid)
        if process is None:
            return None

        status_record = status_records.get(process_pid, {})
        if not self._matches_process(process, status_record):
            status_record = {}
        pane = next(
            (pane for pane in panes if pane.pid in process_ancestors),
            None,
        )
        observed_status = (
            status_record.get("state") if "state" in status_record else None
        )
        current_status = self.state_registry.observe(process_pid, observed_status)
        return self.session_factory.create(
            process,
            pane,
            status_record,
            observed_state=current_status,
        )

    @staticmethod
    def _matches_process(
        process: ProcessInfo,
        status_record: Mapping[str, Any],
    ) -> bool:
        recorded_process_started_at = status_record.get("process_started_at")
        if recorded_process_started_at is None:
            return True
        try:
            return (
                abs(float(recorded_process_started_at) - process.started_at)
                <= PROCESS_START_TOLERANCE
            )
        except (TypeError, ValueError):
            return False


class SnapshotService:
    """Build the stable JSON document consumed by the QML widget."""

    def __init__(
        self,
        collector: SessionSource,
        clock: Callable[[], float] = time.time,
    ):
        self.collector = collector
        self.clock = clock

    def snapshot(self) -> Dict[str, Any]:
        sessions = [session.as_dict() for session in self.collector.collect()]
        session_counts = dict(EMPTY_SESSION_COUNTS)
        session_counts["sessions"] = len(sessions)
        session_counts["attention"] = sum(
            session["attention"] for session in sessions
        )
        for session in sessions:
            count_bucket = STATUS_COUNT_BUCKETS.get(session["state"])
            if count_bucket:
                session_counts[count_bucket] += 1
        return {
            "generated_ts": self.clock(),
            "counts": session_counts,
            "sessions": sessions,
        }
