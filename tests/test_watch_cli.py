import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from watch_test_support import FakeFocusService, FakeSnapshotSource, watch


class SnapshotStreamerTests(unittest.TestCase):
    def test_stream_emits_when_preview_changes_without_state_change(self):
        session = {
            "session_id": "pid:1",
            "state": "WORKING",
            "preview": "first",
        }
        initial_snapshot = {"counts": {"sessions": 1}, "sessions": [session]}
        changed_snapshot = {
            "counts": {"sessions": 1},
            "sessions": [{**session, "preview": "second"}],
        }
        snapshot_source = FakeSnapshotSource([initial_snapshot, changed_snapshot])
        emitted_payloads = []
        sleep_intervals = []

        def stop_after_two_snapshots(interval):
            sleep_intervals.append(interval)
            if len(sleep_intervals) == 2:
                raise StopIteration

        streamer = watch.SnapshotStreamer(
            snapshot_source,
            interval=1,
            sleep=stop_after_two_snapshots,
            emit=emitted_payloads.append,
        )

        with self.assertRaises(StopIteration):
            streamer.run()

        self.assertEqual(len(emitted_payloads), 2)
        self.assertEqual(
            json.loads(emitted_payloads[1])["sessions"][0]["preview"], "second"
        )


class CommandLineTests(unittest.TestCase):
    def test_focus_option_is_handled_even_for_an_empty_argument(self):
        focus = FakeFocusService(True)

        focus_succeeded = watch.main(
            ["--focus", ""],
            snapshot_source=SimpleNamespace(snapshot=lambda: {}),
            focus_service=focus,
        )

        self.assertEqual(focus_succeeded, 0)
        self.assertEqual(focus.focus_calls, [""])

    def test_focus_state_selects_oldest_attention_session(self):
        focus = FakeFocusService(True)
        snapshot = {
            "sessions": [
                {
                    "source_pid": 202,
                    "state": "NEEDS_APPROVAL",
                    "attention": True,
                    "attention_since": 200,
                },
                {
                    "source_pid": 101,
                    "state": "NEEDS_APPROVAL",
                    "attention": True,
                    "attention_since": 100,
                },
                {
                    "source_pid": 303,
                    "state": "IDLE",
                    "attention": True,
                    "attention_since": 50,
                },
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                watch,
                "FOCUS_CYCLE_STATE_FILE",
                str(Path(directory) / "focus.json"),
            ):
                focus_succeeded = watch.main(
                    ["--focus-state", "permission"],
                    snapshot_source=SimpleNamespace(snapshot=lambda: snapshot),
                    focus_service=focus,
                )

        self.assertEqual(focus_succeeded, 0)
        self.assertEqual(focus.focus_calls, [101])

    def test_focus_state_returns_failure_when_no_session_matches(self):
        focus = FakeFocusService(True)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                watch,
                "FOCUS_CYCLE_STATE_FILE",
                str(Path(directory) / "focus.json"),
            ):
                focus_succeeded = watch.main(
                    ["--focus-state", "response"],
                    snapshot_source=SimpleNamespace(snapshot=lambda: {"sessions": []}),
                    focus_service=focus,
                )

        self.assertEqual(focus_succeeded, 1)
        self.assertEqual(focus.focus_calls, [])

    def test_focus_state_selects_idle_session_without_attention(self):
        focus = FakeFocusService(True)
        snapshot = {
            "sessions": [
                {
                    "session_id": "idle-session",
                    "source_pid": 101,
                    "state": "IDLE",
                    "attention": False,
                    "last_transition_ts": 100,
                }
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                watch,
                "FOCUS_CYCLE_STATE_FILE",
                str(Path(directory) / "focus.json"),
            ):
                focus_succeeded = watch.main(
                    ["--focus-state", "idle"],
                    snapshot_source=SimpleNamespace(snapshot=lambda: snapshot),
                    focus_service=focus,
                )

        self.assertEqual(focus_succeeded, 0)
        self.assertEqual(focus.focus_calls, [101])

    def test_focus_state_cycles_across_repeated_commands(self):
        focus = FakeFocusService(True)
        snapshot = {
            "sessions": [
                {
                    "session_id": "first",
                    "source_pid": 101,
                    "state": "WORKING",
                    "attention": False,
                    "last_transition_ts": 100,
                },
                {
                    "session_id": "second",
                    "source_pid": 202,
                    "state": "WORKING",
                    "attention": False,
                    "last_transition_ts": 200,
                },
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                watch,
                "FOCUS_CYCLE_STATE_FILE",
                str(Path(directory) / "focus.json"),
            ):
                self.assertEqual(
                    watch.main(
                        ["--focus-state", "working"],
                        snapshot_source=SimpleNamespace(snapshot=lambda: snapshot),
                        focus_service=focus,
                    ),
                    0,
                )
                self.assertEqual(
                    watch.main(
                        ["--focus-state", "working"],
                        snapshot_source=SimpleNamespace(snapshot=lambda: snapshot),
                        focus_service=focus,
                    ),
                    0,
                )

        self.assertEqual(focus.focus_calls, [101, 202])

    def test_focus_next_and_previous_cycle_all_session_states(self):
        focus = FakeFocusService(True)
        snapshot = {
            "sessions": [
                {
                    "session_id": "working",
                    "source_pid": 101,
                    "state": "WORKING",
                    "attention": False,
                },
                {
                    "session_id": "response",
                    "source_pid": 202,
                    "state": "WAITING",
                    "attention": False,
                },
                {
                    "session_id": "permission",
                    "source_pid": 303,
                    "state": "NEEDS_APPROVAL",
                    "attention": False,
                },
                {
                    "session_id": "idle",
                    "source_pid": 404,
                    "state": "IDLE",
                    "attention": False,
                },
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                watch,
                "FOCUS_CYCLE_STATE_FILE",
                str(Path(directory) / "focus.json"),
            ):
                for _ in range(4):
                    self.assertEqual(
                        watch.main(
                            ["--focus-next"],
                            snapshot_source=SimpleNamespace(snapshot=lambda: snapshot),
                            focus_service=focus,
                        ),
                        0,
                    )
                for _ in range(2):
                    self.assertEqual(
                        watch.main(
                            ["--focus-previous"],
                            snapshot_source=SimpleNamespace(snapshot=lambda: snapshot),
                            focus_service=focus,
                        ),
                        0,
                    )

        self.assertEqual(focus.focus_calls, [101, 202, 303, 404, 303, 202])


if __name__ == "__main__":
    unittest.main()
