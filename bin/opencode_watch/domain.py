"""Domain models and lifecycle rules for tracked OpenCode sessions."""

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
    Set,
    Type,
)


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
    start_ticks: Optional[int] = None


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
    context_tokens: Any = None
    context_limit: Any = None
    context_percentage: Any = None

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
            "context_tokens": self.context_tokens,
            "context_limit": self.context_limit,
            "context_percentage": self.context_percentage,
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

EMPTY_SESSION_COUNTS = {
    "sessions": 0,
    "attention": 0,
    "response": 0,
    "permission": 0,
    "idle": 0,
    "working": 0,
}

DEFAULT_PREVIEWS = {
    "WORKING": "working",
    "WAITING": "waiting for response",
    "NEEDS_APPROVAL": "waiting for permission",
    "IDLE": "idle",
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
        active_pid_set = set(active_pids)
        for source_pid in set(self._machines) - active_pid_set:
            del self._machines[source_pid]
