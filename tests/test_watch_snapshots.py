import unittest

from watch_test_support import FakeAttentionSource, FakeProcessSource, FakeTerminal, watch


class SnapshotServiceTests(unittest.TestCase):
    def test_new_process_uses_its_first_reported_attention_state(self):
        process_source = FakeProcessSource(
            {101: watch.ProcessInfo(101, "/work/alpha", 100)},
            {101: [101]},
        )
        attention_source = FakeAttentionSource({101: {"state": "NEEDS_APPROVAL"}})
        collector = watch.SessionCollector(
            process_source,
            attention_source,
            FakeTerminal(),
        )

        self.assertEqual(collector.collect()[0].state, "NEEDS_APPROVAL")

        attention_source.status_records[101]["state"] = "WORKING"
        self.assertEqual(collector.collect()[0].state, "WORKING")

        attention_source.status_records[101]["state"] = "NEEDS_APPROVAL"
        self.assertEqual(collector.collect()[0].state, "NEEDS_APPROVAL")

    def test_status_record_allows_plugin_startup_clock_skew(self):
        process_source = FakeProcessSource(
            {101: watch.ProcessInfo(101, "/work/alpha", 100)},
            {101: [101]},
        )
        collector = watch.SessionCollector(
            process_source,
            FakeAttentionSource(
                {
                    101: {
                        "state": "NEEDS_APPROVAL",
                        "process_started_at": 102.2,
                    }
                }
            ),
            FakeTerminal(),
        )

        self.assertEqual(collector.collect()[0].state, "NEEDS_APPROVAL")

    def test_recreated_process_gets_a_new_state(self):
        process_source = FakeProcessSource(
            {101: watch.ProcessInfo(101, "/work/alpha", 100)},
            {101: [101]},
        )
        attention_source = FakeAttentionSource({101: {"state": "WORKING"}})
        collector = watch.SessionCollector(
            process_source,
            attention_source,
            FakeTerminal(),
        )

        self.assertEqual(collector.collect()[0].state, "WORKING")

        process_source.process_info_by_pid.clear()
        self.assertEqual(collector.collect(), [])

        process_source.process_info_by_pid[101] = watch.ProcessInfo(
            101, "/work/alpha", 200
        )
        attention_source.status_records[101]["state"] = "WAITING"
        self.assertEqual(collector.collect()[0].state, "WAITING")

    def test_snapshot_ignores_nested_processes_and_aggregates_states(self):
        processes = {
            101: watch.ProcessInfo(101, "/work/alpha", 100),
            202: watch.ProcessInfo(202, "/work/nested", 200),
            303: watch.ProcessInfo(303, "/work/beta", 300),
        }
        process_source = FakeProcessSource(
            processes,
            {
                101: [101, 10],
                202: [202, 101, 10],
                303: [303, 20],
            },
        )
        attention_source = FakeAttentionSource(
            {
                101: {
                    "session_id": "alpha",
                    "state": "WORKING",
                    "attention": True,
                    "attention_since": 90,
                    "preview": "Answer the question",
                    "context_tokens": 1300,
                    "context_limit": 10000,
                    "context_percentage": 13,
                },
                303: {"session_id": "beta", "state": "IDLE"},
            }
        )
        terminal = FakeTerminal(watch.TmuxPane("%1", 10))
        collector = watch.SessionCollector(process_source, attention_source, terminal)
        snapshots = watch.SnapshotService(collector, clock=lambda: 1234)

        collector.collect()
        attention_source.status_records[101]["state"] = "WAITING"
        state = snapshots.snapshot()

        self.assertEqual(state["generated_ts"], 1234)
        self.assertEqual(
            state["counts"],
            {
                "sessions": 2,
                "attention": 1,
                "response": 1,
                "permission": 0,
                "idle": 1,
                "working": 0,
            },
        )
        self.assertEqual(
            [session["session_id"] for session in state["sessions"]],
            ["alpha", "beta"],
        )
        self.assertEqual(state["sessions"][0]["tmux_pane"], "%1")
        self.assertEqual(state["sessions"][1]["state"], "IDLE")
        self.assertEqual(state["sessions"][0]["context_tokens"], 1300)
        self.assertEqual(state["sessions"][0]["context_limit"], 10000)
        self.assertEqual(state["sessions"][0]["context_percentage"], 13)
        self.assertEqual(terminal.pane_call_count, 2)

    def test_untracked_process_defaults_to_idle(self):
        process_source = FakeProcessSource(
            {101: watch.ProcessInfo(101, "/work/alpha", 100)},
            {101: [101]},
        )
        collector = watch.SessionCollector(
            process_source,
            FakeAttentionSource({}),
            FakeTerminal(),
        )

        session = collector.collect()[0]

        self.assertEqual(session.state, "IDLE")
        self.assertEqual(session.session_id, "pid:101")
        self.assertFalse(session.attention)
        self.assertEqual(session.preview, "idle")

        counts = watch.SnapshotService(collector).snapshot()["counts"]

        self.assertEqual(counts["idle"], 1)
        self.assertEqual(counts["working"], 0)

    def test_permission_state_is_not_counted_as_working(self):
        process_source = FakeProcessSource(
            {101: watch.ProcessInfo(101, "/work/alpha", 100)},
            {101: [101]},
        )
        attention_source = FakeAttentionSource({101: {"state": "WORKING"}})
        collector = watch.SessionCollector(process_source, attention_source, FakeTerminal())

        collector.collect()
        attention_source.status_records[101]["state"] = "NEEDS_APPROVAL"

        counts = watch.SnapshotService(collector).snapshot()["counts"]

        self.assertEqual(counts["permission"], 1)
        self.assertEqual(counts["working"], 0)
        self.assertEqual(collector.collect()[0].preview, "waiting for permission")

    def test_working_state_is_exclusive_from_other_status_counts(self):
        process_source = FakeProcessSource(
            {101: watch.ProcessInfo(101, "/work/alpha", 100)},
            {101: [101]},
        )
        collector = watch.SessionCollector(
            process_source,
            FakeAttentionSource({101: {"state": "WORKING"}}),
            FakeTerminal(),
        )

        state = watch.SnapshotService(collector).snapshot()

        self.assertEqual(state["counts"]["sessions"], 1)
        self.assertEqual(state["counts"]["working"], 1)
        self.assertEqual(state["counts"]["idle"], 0)
        self.assertEqual(state["counts"]["response"], 0)
        self.assertEqual(state["counts"]["permission"], 0)


if __name__ == "__main__":
    unittest.main()
