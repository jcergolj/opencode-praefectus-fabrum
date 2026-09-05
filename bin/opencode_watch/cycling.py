"""Session selection and persistent focus-cycle coordination."""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union


FOCUS_STATE_ALIASES = {
    "working": "WORKING",
    "response": "WAITING",
    "permission": "NEEDS_APPROVAL",
    "idle": "IDLE",
}
ALL_SESSIONS_BUCKET = "all"


def snapshot_signature(snapshot: Mapping[str, Any]) -> str:
    """Exclude the changing generation time but include every visible value."""

    return json.dumps(
        {
            "counts": snapshot.get("counts", {}),
            "sessions": snapshot.get("sessions", []),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def session_identity(session_record: Mapping[str, Any]) -> str:
    """Return a stable identity for a session in the cycle state file."""

    session_id = session_record.get("session_id")
    if session_id is not None and str(session_id):
        return "session:" + str(session_id)
    source_pid = session_record.get("source_pid")
    if source_pid is not None:
        return "pid:" + str(source_pid)
    return ""


class SessionSelector:
    """Select and order sessions for state-specific focus shortcuts."""

    def candidates_for_bucket(
        self,
        snapshot: Mapping[str, Any],
        requested_bucket: str,
    ) -> List[Mapping[str, Any]]:
        session_records = snapshot.get("sessions", [])
        if not isinstance(session_records, list):
            return []

        if requested_bucket == ALL_SESSIONS_BUCKET:
            return [
                session_record
                for session_record in session_records
                if isinstance(session_record, Mapping) and session_identity(session_record)
            ]

        target_status = FOCUS_STATE_ALIASES[requested_bucket]
        candidates = [
            session_record
            for session_record in session_records
            if isinstance(session_record, Mapping)
            and session_record.get("state") == target_status
            and (
                target_status in ("WORKING", "IDLE")
                or session_record.get("attention")
            )
        ]
        candidates.sort(key=self._attention_sort_key)
        return candidates

    @staticmethod
    def _attention_sort_key(session_record: Mapping[str, Any]) -> Tuple[float, str]:
        timestamp_value = session_record.get("attention_since") or session_record.get(
            "last_transition_ts"
        )
        try:
            attention_timestamp = float(timestamp_value)
        except (TypeError, ValueError):
            attention_timestamp = 0.0
        # The identity tie-breaker prevents the cycle order changing between
        # snapshots when two sessions transition at the same time.
        return attention_timestamp, session_identity(session_record)

    def session_for_bucket(
        self,
        snapshot: Mapping[str, Any],
        requested_bucket: str,
        previous_identity: Optional[str] = None,
        direction: int = 1,
    ) -> Optional[Mapping[str, Any]]:
        candidates = self.candidates_for_bucket(snapshot, requested_bucket)
        if not candidates:
            return None

        selected_index = len(candidates) - 1 if direction < 0 else 0
        if previous_identity is not None:
            for index, candidate in enumerate(candidates):
                if session_identity(candidate) == previous_identity:
                    selected_index = (index + direction) % len(candidates)
                    break
        return candidates[selected_index]

    def target_for_bucket(
        self,
        snapshot: Mapping[str, Any],
        requested_bucket: str,
        previous_identity: Optional[str] = None,
        direction: int = 1,
    ) -> Optional[Any]:
        selected_session = self.session_for_bucket(
            snapshot, requested_bucket, previous_identity, direction
        )
        if selected_session is None:
            return None
        return selected_session.get("source_pid") or selected_session.get("session_id")


def focus_candidates_for_bucket(
    snapshot: Mapping[str, Any], requested_bucket: str
) -> List[Mapping[str, Any]]:
    return SessionSelector().candidates_for_bucket(snapshot, requested_bucket)


def focus_candidates_for_state(
    snapshot: Mapping[str, Any], requested_bucket: str
) -> List[Mapping[str, Any]]:
    return focus_candidates_for_bucket(snapshot, requested_bucket)


def focus_session_for_bucket(
    snapshot: Mapping[str, Any],
    requested_bucket: str,
    previous_identity: Optional[str] = None,
    direction: int = 1,
) -> Optional[Mapping[str, Any]]:
    return SessionSelector().session_for_bucket(
        snapshot, requested_bucket, previous_identity, direction
    )


def focus_session_for_state(
    snapshot: Mapping[str, Any],
    requested_bucket: str,
    previous_identity: Optional[str] = None,
    direction: int = 1,
) -> Optional[Mapping[str, Any]]:
    return focus_session_for_bucket(
        snapshot, requested_bucket, previous_identity, direction
    )


def focus_target_for_bucket(
    snapshot: Mapping[str, Any],
    requested_bucket: str,
    previous_identity: Optional[str] = None,
    direction: int = 1,
) -> Optional[Any]:
    return SessionSelector().target_for_bucket(
        snapshot, requested_bucket, previous_identity, direction
    )


def focus_target_for_state(
    snapshot: Mapping[str, Any],
    requested_bucket: str,
    previous_identity: Optional[str] = None,
    direction: int = 1,
) -> Optional[Any]:
    return focus_target_for_bucket(
        snapshot, requested_bucket, previous_identity, direction
    )


class FocusCycleStore:
    """Serialize repeated status shortcuts and remember the last target."""

    def __init__(
        self,
        path: Union[os.PathLike, str],
        selector: Optional[SessionSelector] = None,
    ):
        self.path = os.fspath(path)
        self.lock_path = self.path + ".lock"
        self.selector = selector or SessionSelector()

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
            with open(self.path, encoding="utf-8") as cycle_state_file:
                cycle_state_document = json.load(cycle_state_file)
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(cycle_state_document, dict):
            return {}
        return {
            str(status_bucket): str(identity)
            for status_bucket, identity in cycle_state_document.items()
            if identity is not None
        }

    def _write(self, cycle_state: Mapping[str, str]) -> None:
        directory = os.path.dirname(self.path) or "."
        temporary_path = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".opencode-focus-", dir=directory, text=True
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as cycle_state_file:
                json.dump(cycle_state, cycle_state_file, separators=(",", ":"))
                cycle_state_file.write("\n")
                cycle_state_file.flush()
                os.fsync(cycle_state_file.fileno())
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
        snapshot: Mapping[str, Any],
        requested_bucket: str,
        focus_session: Callable[[Any], bool],
        direction: int,
    ) -> bool:
        cycle_state = self._read()
        selected_session = self.selector.session_for_bucket(
            snapshot,
            requested_bucket,
            cycle_state.get(requested_bucket),
            direction,
        )
        if selected_session is None:
            cycle_state.pop(requested_bucket, None)
            try:
                self._write(cycle_state)
            except OSError:
                pass
            return False

        session_target = selected_session.get("source_pid") or selected_session.get(
            "session_id"
        )
        if session_target is None:
            return False
        try:
            focus_succeeded = focus_session(session_target)
        except OSError:
            focus_succeeded = False
        if not focus_succeeded:
            return False

        cycle_state[requested_bucket] = session_identity(selected_session)
        try:
            self._write(cycle_state)
        except OSError:
            # Focusing the session is still successful if persistence is not
            # available; the next press will simply start at the first match.
            pass
        return True

    def focus(
        self,
        snapshot: Mapping[str, Any],
        requested_bucket: str,
        focus_session: Callable[[Any], bool],
        direction: int = 1,
    ) -> bool:
        try:
            with self._lock():
                return self._focus_locked(
                    snapshot, requested_bucket, focus_session, direction
                )
        except OSError:
            # The runtime directory may be unavailable during shutdown. Keep
            # the shortcut useful by focusing the first match without cycling.
            session_target = self.selector.target_for_bucket(
                snapshot, requested_bucket, direction=direction
            )
            if session_target is None:
                return False
            try:
                return focus_session(session_target)
            except OSError:
                return False
