import sys
from pathlib import Path


BIN_DIR = Path(__file__).parents[1] / "bin"
sys.path.insert(0, str(BIN_DIR))
import opencode_watch as watch


class FakeProcessSource:
    def __init__(self, process_info_by_pid, process_ancestors):
        self.process_info_by_pid = process_info_by_pid
        self.process_ancestors = process_ancestors

    def opencode_pids(self):
        return set(self.process_info_by_pid)

    def inspect(self, pid):
        return self.process_info_by_pid.get(pid)

    def ancestors(self, pid):
        return self.process_ancestors.get(pid, [pid])


class FakeAttentionSource:
    def __init__(self, status_records):
        self.status_records = status_records

    def read(self):
        return self.status_records


class FakeTerminal:
    def __init__(self, pane=None):
        self.pane = pane
        self.pane_call_count = 0

    def panes(self):
        self.pane_call_count += 1
        return [self.pane] if self.pane else []


class FakeSnapshotSource:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.last_snapshot = None

    def snapshot(self):
        try:
            self.last_snapshot = next(self.snapshots)
        except StopIteration:
            pass
        return self.last_snapshot


class FakeFocusTarget:
    def __init__(self, focus_succeeded):
        self.focus_succeeded = focus_succeeded
        self.focus_calls = []

    def focus(self, ancestors):
        self.focus_calls.append(ancestors)
        return self.focus_succeeded


class FakeFocusService:
    def __init__(self, focus_succeeded):
        self.focus_succeeded = focus_succeeded
        self.focus_calls = []

    def focus(self, target):
        self.focus_calls.append(target)
        return self.focus_succeeded
