import tempfile
import unittest
from pathlib import Path

from watch_test_support import watch


class FocusCycleStoreTests(unittest.TestCase):
    def test_session_selector_orders_attention_by_age_and_identity(self):
        snapshot = {
            "sessions": [
                {
                    "session_id": "later",
                    "state": "NEEDS_APPROVAL",
                    "attention": True,
                    "attention_since": 200,
                },
                {
                    "session_id": "same-time-b",
                    "state": "NEEDS_APPROVAL",
                    "attention": True,
                    "attention_since": 100,
                },
                {
                    "session_id": "same-time-a",
                    "state": "NEEDS_APPROVAL",
                    "attention": True,
                    "attention_since": 100,
                },
            ]
        }

        selector = watch.SessionSelector()

        self.assertEqual(
            [
                session["session_id"]
                for session in selector.candidates_for_bucket(snapshot, "permission")
            ],
            ["same-time-a", "same-time-b", "later"],
        )

    def test_working_sessions_are_candidates_without_attention(self):
        snapshot = {
            "sessions": [
                {"source_pid": 101, "state": "WORKING", "attention": False},
                {"source_pid": 202, "state": "WAITING", "attention": True},
            ]
        }

        self.assertEqual(
            watch.focus_target_for_state(snapshot, "working"),
            101,
        )

    def test_repeated_focus_cycles_per_status(self):
        snapshot = {
            "sessions": [
                {
                    "session_id": "first",
                    "source_pid": 101,
                    "state": "WORKING",
                    "last_transition_ts": 100,
                },
                {
                    "session_id": "second",
                    "source_pid": 202,
                    "state": "WORKING",
                    "last_transition_ts": 200,
                },
            ]
        }
        focused_targets = []

        with tempfile.TemporaryDirectory() as directory:
            store = watch.FocusCycleStore(Path(directory) / "focus.json")
            focus_session = (
                lambda session_target: focused_targets.append(session_target) or True
            )

            self.assertTrue(store.focus(snapshot, "working", focus_session))
            self.assertTrue(store.focus(snapshot, "working", focus_session))
            self.assertTrue(store.focus(snapshot, "working", focus_session))

        self.assertEqual(focused_targets, [101, 202, 101])

    def test_all_sessions_are_candidates_regardless_of_state(self):
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

        self.assertEqual(
            [
                session["source_pid"]
                for session in watch.focus_candidates_for_bucket(
                    snapshot, watch.ALL_SESSIONS_BUCKET
                )
            ],
            [101, 202, 303, 404],
        )

    def test_previous_direction_starts_at_the_last_session_and_wraps(self):
        snapshot = {
            "sessions": [
                {"session_id": "first", "source_pid": 101},
                {"session_id": "second", "source_pid": 202},
            ]
        }
        focused_targets = []

        with tempfile.TemporaryDirectory() as directory:
            store = watch.FocusCycleStore(Path(directory) / "focus.json")
            focus_session = (
                lambda session_target: focused_targets.append(session_target) or True
            )

            self.assertTrue(
                store.focus(
                    snapshot,
                    watch.ALL_SESSIONS_BUCKET,
                    focus_session,
                    direction=-1,
                )
            )
            self.assertTrue(
                store.focus(
                    snapshot,
                    watch.ALL_SESSIONS_BUCKET,
                    focus_session,
                    direction=-1,
                )
            )

        self.assertEqual(focused_targets, [202, 101])


if __name__ == "__main__":
    unittest.main()
