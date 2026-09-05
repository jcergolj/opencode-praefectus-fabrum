#!/usr/bin/env python3
"""Collect and focus the OpenCode sessions visible on this desktop."""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
)


RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", os.path.expanduser("~/.cache"))
# Status metadata is optional; live process counting never depends on it.
STATUS_STATE_FILE = os.environ.get(
    "OPENCODE_STATUS_FILE",
    os.path.join(RUNTIME_DIR, "opencode-praefectus-fabrum"),
)
FOCUS_CYCLE_STATE_FILE = os.path.join(RUNTIME_DIR, "opencode-focus-cycle.json")
# OpenCode can initialize the plugin a few seconds after the process starts.
PROCESS_START_TOLERANCE = 5.0
EMPTY_COUNTS = {
    "sessions": 0,
    "attention": 0,
    "response": 0,
    "permission": 0,
    "idle": 0,
    "working": 0,
}
FOCUS_STATE_ALIASES = {
    "working": "WORKING",
    "response": "WAITING",
    "permission": "NEEDS_APPROVAL",
    "idle": "IDLE",
}
DEFAULT_PREVIEWS = {
    "WORKING": "working",
    "WAITING": "waiting for response",
    "NEEDS_APPROVAL": "waiting for permission",
    "IDLE": "idle",
}


class ProcessSource(Protocol):
    def opencode_pids(self) -> Set[int]: ...

    def inspect(self, pid: int) -> Optional["ProcessInfo"]: ...

    def ancestors(self, pid: int) -> List[int]: ...


class AttentionSource(Protocol):
    def read(self) -> Mapping[int, Mapping[str, Any]]: ...


class TerminalSource(Protocol):
    def panes(self) -> List["TmuxPane"]: ...


class FocusTarget(Protocol):
    def focus(self, ancestors: Iterable[int]) -> bool: ...


class SnapshotSource(Protocol):
    def snapshot(self) -> Mapping[str, Any]: ...


class SessionSource(Protocol):
    def collect(self) -> List["Session"]: ...


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    directory: str
    started_at: float


@dataclass(frozen=True)
class TmuxPane:
    id: str
    pid: int


@dataclass(frozen=True)
class Session:
    session_id: Any
    project: str
    state: Any
    tmux_pane: Optional[str]
    tmux_socket: Any
    source_pid: int
    directory: str
    notification_id: Any
    attention: bool
    attention_since: Any
    last_transition_ts: Any
    preview: Any

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "state": self.state,
            "tmux_pane": self.tmux_pane,
            "tmux_socket": self.tmux_socket,
            "source_pid": self.source_pid,
            "directory": self.directory,
            "notification_id": self.notification_id,
            "attention": self.attention,
            "attention_since": self.attention_since,
            "last_transition_ts": self.last_transition_ts,
            "preview": self.preview,
        }


class SessionStatus(str, Enum):
    """Statuses supported by the OpenCode session lifecycle."""

    IDLE = "IDLE"
    WORKING = "WORKING"
    WAITING = "WAITING"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"

    @classmethod
    def from_value(cls, value: Any) -> Optional["SessionStatus"]:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            return None
        try:
            return cls(value.strip().upper())
        except ValueError:
            return None


class InvalidTransition(ValueError):
    """Raised when a state receives a status it cannot transition to."""

    def __init__(self, current: SessionStatus, target: SessionStatus):
        super().__init__(f"cannot transition from {current.value} to {target.value}")
        self.current = current
        self.target = target


class StateFactory(Protocol):
    def create(self, status: SessionStatus) -> "SessionState": ...


class SessionState:
    """State behavior for one OpenCode session."""

    status = SessionStatus.IDLE
    allowed_transitions: FrozenSet[SessionStatus] = frozenset()

    def can_transition_to(self, target: SessionStatus) -> bool:
        return target == self.status or target in self.allowed_transitions

    def transition_to(
        self,
        target: SessionStatus,
        state_factory: StateFactory,
    ) -> "SessionState":
        if not self.can_transition_to(target):
            raise InvalidTransition(self.status, target)
        if target == self.status:
            return self
        return state_factory.create(target)


class IdleSessionState(SessionState):
    status = SessionStatus.IDLE
    allowed_transitions = frozenset(
        {
            SessionStatus.WORKING,
            SessionStatus.WAITING,
            SessionStatus.NEEDS_APPROVAL,
        }
    )


class WorkingSessionState(SessionState):
    status = SessionStatus.WORKING
    allowed_transitions = frozenset(
        {
            SessionStatus.IDLE,
            SessionStatus.WAITING,
            SessionStatus.NEEDS_APPROVAL,
        }
    )


class AttentionSessionState(SessionState):
    """Base state for statuses that can receive every lifecycle status."""

    allowed_transitions = frozenset(SessionStatus)


class WaitingSessionState(AttentionSessionState):
    status = SessionStatus.WAITING


class NeedsApprovalSessionState(AttentionSessionState):
    status = SessionStatus.NEEDS_APPROVAL


STATUS_COUNT_BUCKETS = {
    SessionStatus.WAITING.value: "response",
    SessionStatus.NEEDS_APPROVAL.value: "permission",
    SessionStatus.IDLE.value: "idle",
    SessionStatus.WORKING.value: "working",
}


class SessionStateFactory:
    """Create concrete state objects without coupling the state context to them."""

    def __init__(
        self,
        state_types: Optional[Mapping[SessionStatus, Type[SessionState]]] = None,
    ):
        self._state_types = dict(
            state_types
            or {
                SessionStatus.IDLE: IdleSessionState,
                SessionStatus.WORKING: WorkingSessionState,
                SessionStatus.WAITING: WaitingSessionState,
                SessionStatus.NEEDS_APPROVAL: NeedsApprovalSessionState,
            }
        )

    def create(self, status: SessionStatus) -> SessionState:
        try:
            state_type = self._state_types[status]
        except KeyError as error:
            raise ValueError(f"unsupported session status: {status}") from error
        return state_type()


class SessionStateMachine:
    """Own the current state and apply only valid lifecycle transitions."""

    def __init__(self, state_factory: Optional[StateFactory] = None):
        self._state_factory = state_factory or SessionStateFactory()
        self._state = self._state_factory.create(SessionStatus.IDLE)

    @property
    def current_state(self) -> SessionState:
        return self._state

    @property
    def status(self) -> SessionStatus:
        return self._state.status

    def can_transition_to(self, target: Any) -> bool:
        target_status = SessionStatus.from_value(target)
        return target_status is not None and self._state.can_transition_to(target_status)

    def transition_to(self, target: Any) -> bool:
        """Apply an observed status, returning false for invalid observations."""

        target_status = SessionStatus.from_value(target)
        if target_status is None:
            return False
        try:
            self._state = self._state.transition_to(target_status, self._state_factory)
        except InvalidTransition:
            return False
        return True


class SessionStateRegistry:
    """Keep one state machine per live top-level OpenCode process."""

    def __init__(
        self,
        machine_factory: Optional[Callable[[], SessionStateMachine]] = None,
    ):
        self._machine_factory = machine_factory or SessionStateMachine
        self._machines: Dict[int, SessionStateMachine] = {}

    def observe(self, source_pid: int, observed_status: Any = None) -> SessionStatus:
        machine = self._machines.get(source_pid)
        if machine is None:
            machine = self._machine_factory()
            self._machines[source_pid] = machine
        if observed_status is not None:
            machine.transition_to(observed_status)
        return machine.status

    def remove_missing(self, active_pids: Iterable[int]) -> None:
        active = set(active_pids)
        for source_pid in set(self._machines) - active:
            del self._machines[source_pid]


class AttentionStateReader:
    """Read optional status records indexed by source PID."""

    def __init__(self, path: Union[os.PathLike, str]):
        self.path = os.fspath(path)

    @staticmethod
    def _read_file(path: str) -> Dict[int, Mapping[str, Any]]:
        try:
            with open(path, encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, ValueError, TypeError):
            return {}

        result: Dict[int, Mapping[str, Any]] = {}
        records = state.get("sessions") if isinstance(state, dict) else None
        if isinstance(records, list):
            records = [record for record in records if isinstance(record, dict)]
        elif isinstance(state, dict):
            records = [state]
        else:
            records = []

        for record in records:
            if record.get("parent_id") or record.get("parentID"):
                continue
            try:
                source_pid = int(record["source_pid"])
            except (KeyError, TypeError, ValueError):
                continue
            result[source_pid] = record
        return result

    def read(self) -> Dict[int, Mapping[str, Any]]:
        if not os.path.isdir(self.path):
            return self._read_file(self.path)

        result: Dict[int, Mapping[str, Any]] = {}
        try:
            entries = os.scandir(self.path)
        except OSError:
            return result

        with entries:
            for entry in entries:
                if not entry.name.endswith(".json") or not entry.is_file():
                    continue
                result.update(self._read_file(entry.path))
        return result


def process_stat(
    pid: int,
    proc_root: Union[os.PathLike, str] = "/proc",
) -> Tuple[Optional[int], int]:
    try:
        path = os.path.join(os.fspath(proc_root), str(pid), "stat")
        with open(path, encoding="utf-8") as stat_file:
            fields = stat_file.read().rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[19])
    except (OSError, IndexError, ValueError):
        return None, 0


def read_boot_time(proc_root: Union[os.PathLike, str] = "/proc") -> float:
    try:
        path = os.path.join(os.fspath(proc_root), "stat")
        with open(path, encoding="utf-8") as stat_file:
            for line in stat_file:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return time.time()


class ProcProcessSource:
    """Adapt Linux /proc process information to the process source interface."""

    def __init__(
        self,
        proc_root: Union[os.PathLike, str] = "/proc",
        boot_time_reader: Optional[Callable[[], float]] = None,
        clock_ticks: Optional[int] = None,
    ):
        self.proc_root = os.fspath(proc_root)
        read_boot = boot_time_reader or (lambda: read_boot_time(self.proc_root))
        self.boot_time = read_boot()
        self.clock_ticks = (
            clock_ticks
            if clock_ticks is not None
            else os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        )

    def _comm(self, pid: int) -> Optional[str]:
        try:
            path = os.path.join(self.proc_root, str(pid), "comm")
            with open(path, encoding="utf-8") as comm_file:
                return comm_file.read().strip()
        except OSError:
            return None

    def opencode_pids(self) -> Set[int]:
        result: Set[int] = set()
        try:
            entries = os.scandir(self.proc_root)
        except OSError:
            return result

        with entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    pid = int(entry.name)
                except ValueError:
                    continue
                if self._comm(pid) == "opencode":
                    result.add(pid)
        return result

    def ancestors(self, pid: int) -> List[int]:
        result: List[int] = []
        current = pid
        seen: Set[int] = set()
        while current > 1 and current not in seen:
            result.append(current)
            seen.add(current)
            parent, _ = process_stat(current, self.proc_root)
            if not parent:
                break
            current = parent
        return result

    def inspect(self, pid: int) -> Optional[ProcessInfo]:
        if self._comm(pid) != "opencode":
            return None
        try:
            cwd_path = os.path.join(self.proc_root, str(pid), "cwd")
            directory = os.path.realpath(os.readlink(cwd_path))
        except (OSError, ValueError):
            return None

        _, start_ticks = process_stat(pid, self.proc_root)
        started_at = self.boot_time + start_ticks / self.clock_ticks
        return ProcessInfo(pid, directory, started_at)


class SessionFactory:
    """Translate process and optional attention data into a UI session."""

    def create(
        self,
        process: ProcessInfo,
        pane: Optional[TmuxPane],
        tracked: Mapping[str, Any],
        state: Optional[Union[SessionStatus, str]] = None,
    ) -> Session:
        current_status = SessionStatus.from_value(state)
        if current_status is None:
            current_status = SessionStatus.from_value(tracked.get("state"))
        current_status = current_status or SessionStatus.IDLE
        return Session(
            session_id=tracked.get("session_id", f"pid:{process.pid}"),
            project=os.path.basename(process.directory) or "OpenCode",
            state=current_status.value,
            tmux_pane=pane.id if pane else None,
            tmux_socket=tracked.get("tmux_socket"),
            source_pid=process.pid,
            directory=process.directory,
            notification_id=tracked.get("notification_id"),
            attention=bool(tracked.get("attention")),
            attention_since=tracked.get("attention_since"),
            last_transition_ts=tracked.get("last_transition_ts", process.started_at),
            preview=tracked.get("preview")
            or DEFAULT_PREVIEWS.get(current_status.value, "idle"),
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
        attention = self.attention_source.read()
        opencode_pids = self.process_source.opencode_pids()
        panes = self.terminal_source.panes()
        sessions: List[Session] = []
        active_pids: Set[int] = set()

        for pid in sorted(opencode_pids):
            ancestors = self.process_source.ancestors(pid)
            if any(parent in opencode_pids for parent in ancestors[1:]):
                continue

            process = self.process_source.inspect(pid)
            if process is None:
                continue
            tracked = attention.get(pid, {})
            if not self._matches_process(process, tracked):
                tracked = {}
            pane = next((pane for pane in panes if pane.pid in ancestors), None)
            observed_status = tracked.get("state") if "state" in tracked else None
            current_status = self.state_registry.observe(pid, observed_status)
            active_pids.add(pid)
            sessions.append(
                self.session_factory.create(
                    process,
                    pane,
                    tracked,
                    state=current_status,
                )
            )

        self.state_registry.remove_missing(active_pids)
        return sorted(sessions, key=lambda session: session.source_pid)

    @staticmethod
    def _matches_process(
        process: ProcessInfo,
        tracked: Mapping[str, Any],
    ) -> bool:
        started_at = tracked.get("process_started_at")
        if started_at is None:
            return True
        try:
            return abs(float(started_at) - process.started_at) <= PROCESS_START_TOLERANCE
        except (TypeError, ValueError):
            return False


class SnapshotService:
    """Build the stable JSON document consumed by the QML widget."""

    def __init__(self, collector: SessionSource, clock: Callable[[], float] = time.time):
        self.collector = collector
        self.clock = clock

    def snapshot(self) -> Dict[str, Any]:
        sessions = [session.as_dict() for session in self.collector.collect()]
        counts = dict(EMPTY_COUNTS)
        counts["sessions"] = len(sessions)
        counts["attention"] = sum(session["attention"] for session in sessions)
        for session in sessions:
            count_key = STATUS_COUNT_BUCKETS.get(session["state"])
            if count_key:
                counts[count_key] += 1
        return {
            "generated_ts": self.clock(),
            "counts": counts,
            "sessions": sessions,
        }


def command_succeeded(result: Any) -> bool:
    return result is not None and getattr(result, "returncode", 0) == 0


class TmuxClient:
    """Use tmux when it is available for pane discovery and focusing."""

    def __init__(
        self,
        runner: Callable[..., Any] = subprocess.run,
        executable_finder: Callable[[str], Optional[str]] = shutil.which,
        client_ancestors: Optional[Callable[[int], Iterable[int]]] = None,
        window_focus: Optional[FocusTarget] = None,
    ):
        self.runner = runner
        self.executable_finder = executable_finder
        self.client_ancestors = client_ancestors
        self.window_focus = window_focus

    def _run(self, args: Sequence[str]) -> Any:
        try:
            return self.runner(
                ["tmux", *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None

    def panes(self) -> List[TmuxPane]:
        if not self.executable_finder("tmux"):
            return []
        result = self._run(["list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"])
        if result is None:
            return []

        panes: List[TmuxPane] = []
        for line in (getattr(result, "stdout", "") or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            pane_id, pane_pid = parts
            try:
                panes.append(TmuxPane(pane_id, int(pane_pid)))
            except ValueError:
                continue
        return panes

    def pane_for(self, ancestors: Iterable[int]) -> Optional[TmuxPane]:
        ancestor_set = set(ancestors)
        return next((pane for pane in self.panes() if pane.pid in ancestor_set), None)

    def focus(self, ancestors: Iterable[int]) -> bool:
        pane = self.pane_for(ancestors)
        if pane is None:
            return False

        clients = self._run(["list-clients", "-F", "#{client_name}\t#{client_pid}"])
        client = None
        for line in (getattr(clients, "stdout", "") or "").splitlines():
            parts = line.split("\t")
            if not parts or not parts[0]:
                continue
            client_pid = None
            if len(parts) > 1:
                try:
                    client_pid = int(parts[1])
                except ValueError:
                    pass
            client = (parts[0], client_pid)
            break

        if client:
            client_name, client_pid = client
            result = self._run(["switch-client", "-c", client_name, "-t", pane.id])
            if not command_succeeded(result):
                return False
            # Switching panes does not raise the terminal window when the
            # widget lives on another workspace. Focus the tmux client itself,
            # whose process ancestry contains the terminal emulator.
            if (
                client_pid is not None
                and self.client_ancestors
                and self.window_focus
            ):
                self.window_focus.focus(self.client_ancestors(client_pid))
            return True
        else:
            result = self._run(["select-pane", "-t", pane.id])
            return command_succeeded(result)


class HyprlandClient:
    """Use Hyprland as the fallback focus target outside tmux."""

    def __init__(
        self,
        runner: Callable[..., Any] = subprocess.run,
        executable_finder: Callable[[str], Optional[str]] = shutil.which,
    ):
        self.runner = runner
        self.executable_finder = executable_finder

    def focus(self, ancestors: Iterable[int]) -> bool:
        if not self.executable_finder("hyprctl"):
            return False

        try:
            result = self.runner(
                ["hyprctl", "clients", "-j"],
                capture_output=True,
                text=True,
                check=True,
            )
            clients = json.loads(result.stdout)
        except (
            OSError,
            subprocess.CalledProcessError,
            json.JSONDecodeError,
            TypeError,
            AttributeError,
        ):
            return False

        if not isinstance(clients, list):
            return False
        ancestor_set = set(ancestors)
        for client in clients:
            if not isinstance(client, dict):
                continue
            if client.get("pid") not in ancestor_set or not client.get("address"):
                continue
            try:
                result = self.runner(
                    [
                        "hyprctl",
                        "dispatch",
                        f'hl.dsp.focus({{ window = "address:{client["address"]}" }})',
                    ],
                    check=False,
                )
            except OSError:
                return False
            return command_succeeded(result)
        return False


def parse_pid(target: Any) -> Optional[int]:
    try:
        value = str(target)
        return int(value.removeprefix("pid:"))
    except (TypeError, ValueError):
        return None


class FocusService:
    """Try the available desktop focus targets in their configured order."""

    def __init__(self, process_source: ProcessSource, targets: Sequence[FocusTarget]):
        self.process_source = process_source
        self.targets = targets

    def focus(self, target: Any) -> bool:
        pid = parse_pid(target)
        if pid is None:
            return False
        ancestors = self.process_source.ancestors(pid)
        return any(target.focus(ancestors) for target in self.targets)


def snapshot_signature(state: Mapping[str, Any]) -> str:
    """Exclude the changing generation time but include every visible value."""

    return json.dumps(
        {
            "counts": state.get("counts", {}),
            "sessions": state.get("sessions", []),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def session_identity(session: Mapping[str, Any]) -> str:
    """Return a stable identity for a session in the cycle state file."""

    session_id = session.get("session_id")
    if session_id is not None and str(session_id):
        return "session:" + str(session_id)
    source_pid = session.get("source_pid")
    if source_pid is not None:
        return "pid:" + str(source_pid)
    return ""


def focus_candidates_for_state(
    state: Mapping[str, Any], requested_state: str
) -> List[Mapping[str, Any]]:
    wanted_state = FOCUS_STATE_ALIASES[requested_state]
    sessions = state.get("sessions", [])
    if not isinstance(sessions, list):
        return []

    candidates = [
        session
        for session in sessions
        if isinstance(session, Mapping)
        and session.get("state") == wanted_state
        and (wanted_state in ("WORKING", "IDLE") or session.get("attention"))
    ]

    def attention_time(session: Mapping[str, Any]) -> float:
        value = session.get("attention_since") or session.get("last_transition_ts")
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    # The timestamp keeps the existing oldest-attention-first behavior. The
    # identity tie-breaker prevents the cycle order changing between snapshots
    # when two sessions transition at the same time.
    candidates.sort(key=lambda session: (attention_time(session), session_identity(session)))
    return candidates


def focus_session_for_state(
    state: Mapping[str, Any],
    requested_state: str,
    previous_identity: Optional[str] = None,
) -> Optional[Mapping[str, Any]]:
    candidates = focus_candidates_for_state(state, requested_state)
    if not candidates:
        return None

    selected_index = 0
    if previous_identity is not None:
        for index, candidate in enumerate(candidates):
            if session_identity(candidate) == previous_identity:
                selected_index = (index + 1) % len(candidates)
                break
    return candidates[selected_index]


def focus_target_for_state(
    state: Mapping[str, Any],
    requested_state: str,
    previous_identity: Optional[str] = None,
) -> Optional[Any]:
    session = focus_session_for_state(state, requested_state, previous_identity)
    if session is None:
        return None
    return session.get("source_pid") or session.get("session_id")


class FocusCycleStore:
    """Serialize repeated status shortcuts and remember the last target."""

    def __init__(self, path: Union[os.PathLike, str]):
        self.path = os.fspath(path)
        self.lock_path = self.path + ".lock"

    @contextmanager
    def _lock(self):
        directory = os.path.dirname(self.lock_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self) -> Dict[str, str]:
        try:
            with open(self.path, encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(state, dict):
            return {}
        return {
            str(status): str(identity)
            for status, identity in state.items()
            if identity is not None
        }

    def _write(self, state: Mapping[str, str]) -> None:
        directory = os.path.dirname(self.path) or "."
        temporary_path = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".opencode-focus-", dir=directory, text=True
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file, separators=(",", ":"))
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _focus_locked(
        self,
        state: Mapping[str, Any],
        requested_state: str,
        focus: Callable[[Any], bool],
    ) -> bool:
        cycle_state = self._read()
        selected = focus_session_for_state(
            state, requested_state, cycle_state.get(requested_state)
        )
        if selected is None:
            cycle_state.pop(requested_state, None)
            try:
                self._write(cycle_state)
            except OSError:
                pass
            return False

        target = selected.get("source_pid") or selected.get("session_id")
        if target is None:
            return False
        try:
            focused = focus(target)
        except OSError:
            focused = False
        if not focused:
            return False

        cycle_state[requested_state] = session_identity(selected)
        try:
            self._write(cycle_state)
        except OSError:
            # Focusing the session is still successful if persistence is not
            # available; the next press will simply start at the first match.
            pass
        return True

    def focus(
        self,
        state: Mapping[str, Any],
        requested_state: str,
        focus: Callable[[Any], bool],
    ) -> bool:
        try:
            with self._lock():
                return self._focus_locked(state, requested_state, focus)
        except OSError:
            # The runtime directory may be unavailable during shutdown. Keep
            # the shortcut useful by focusing the first match without cycling.
            target = focus_target_for_state(state, requested_state)
            if target is None:
                return False
            try:
                return focus(target)
            except OSError:
                return False


class SnapshotStreamer:
    """Poll a snapshot source and emit only changed documents."""

    def __init__(
        self,
        snapshot_source: SnapshotSource,
        interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        emit: Optional[Callable[[str], None]] = None,
    ):
        self.snapshot_source = snapshot_source
        self.interval = max(0.2, interval)
        self.sleep = sleep
        self.emit = emit or (lambda payload: print(payload, flush=True))

    def run(self, once: bool = False) -> int:
        last_signature = None
        while True:
            state = self.snapshot_source.snapshot()
            signature = snapshot_signature(state)
            if signature != last_signature:
                self.emit(json.dumps(state, separators=(",", ":")))
                last_signature = signature
            if once:
                return 0
            self.sleep(self.interval)


def build_runtime() -> Tuple[SnapshotService, FocusService]:
    process_source = ProcProcessSource()
    hyprland = HyprlandClient()
    terminal_source = TmuxClient(
        client_ancestors=process_source.ancestors,
        window_focus=hyprland,
    )
    collector = SessionCollector(
        process_source,
        AttentionStateReader(STATUS_STATE_FILE),
        terminal_source,
    )
    return SnapshotService(collector), FocusService(
        process_source,
        [terminal_source, hyprland],
    )


def main(
    argv: Optional[Sequence[str]] = None,
    snapshot_source: Optional[SnapshotSource] = None,
    focus_service: Optional[FocusService] = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Optional[Callable[[str], None]] = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--focus")
    parser.add_argument("--focus-state", choices=tuple(FOCUS_STATE_ALIASES))
    args = parser.parse_args(argv)

    if snapshot_source is None or focus_service is None:
        default_snapshot_source, default_focus_service = build_runtime()
        if snapshot_source is None:
            snapshot_source = default_snapshot_source
        if focus_service is None:
            focus_service = default_focus_service

    if args.focus is not None:
        return 0 if focus_service.focus(args.focus) else 1
    if args.focus_state is not None:
        return 0 if FocusCycleStore(FOCUS_CYCLE_STATE_FILE).focus(
            snapshot_source.snapshot(), args.focus_state, focus_service.focus
        ) else 1

    return SnapshotStreamer(
        snapshot_source,
        interval=args.interval,
        sleep=sleep,
        emit=emit,
    ).run(once=args.once)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        pass
