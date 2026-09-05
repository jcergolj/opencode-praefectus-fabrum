"""Command-line composition for the OpenCode watcher."""

import argparse
import json
import time
from typing import Callable, Optional, Sequence, Tuple

from .config import FOCUS_CYCLE_STATE_FILE, STATUS_RECORD_PATH
from .cycling import (
    ALL_SESSIONS_BUCKET,
    FOCUS_STATE_ALIASES,
    FocusCycleStore,
    snapshot_signature,
)
from .domain import SnapshotSource
from .focus import FocusService, HyprlandClient, TmuxClient
from .snapshots import SessionCollector, SnapshotService
from .sources import AttentionStateReader, ProcProcessSource


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
            snapshot = self.snapshot_source.snapshot()
            current_signature = snapshot_signature(snapshot)
            if current_signature != last_signature:
                self.emit(json.dumps(snapshot, separators=(",", ":")))
                last_signature = current_signature
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
        AttentionStateReader(STATUS_RECORD_PATH),
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
    focus_direction = parser.add_mutually_exclusive_group()
    focus_direction.add_argument("--focus-next", action="store_true")
    focus_direction.add_argument("--focus-previous", action="store_true")
    arguments = parser.parse_args(argv)

    if snapshot_source is None or focus_service is None:
        default_snapshot_source, default_focus_service = build_runtime()
        if snapshot_source is None:
            snapshot_source = default_snapshot_source
        if focus_service is None:
            focus_service = default_focus_service

    if arguments.focus is not None:
        return 0 if focus_service.focus(arguments.focus) else 1
    if arguments.focus_state is not None:
        return 0 if FocusCycleStore(FOCUS_CYCLE_STATE_FILE).focus(
            snapshot_source.snapshot(), arguments.focus_state, focus_service.focus
        ) else 1
    if arguments.focus_next or arguments.focus_previous:
        direction = -1 if arguments.focus_previous else 1
        return 0 if FocusCycleStore(FOCUS_CYCLE_STATE_FILE).focus(
            snapshot_source.snapshot(),
            ALL_SESSIONS_BUCKET,
            focus_service.focus,
            direction=direction,
        ) else 1

    return SnapshotStreamer(
        snapshot_source,
        interval=arguments.interval,
        sleep=sleep,
        emit=emit,
    ).run(once=arguments.once)
