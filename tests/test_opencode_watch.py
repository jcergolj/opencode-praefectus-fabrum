import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "bin" / "opencode_watch.py"
LOADER = importlib.machinery.SourceFileLoader("opencode_watch", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader("opencode_watch", LOADER)
watch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)


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


class SessionStateMachineTests(unittest.TestCase):
    def test_new_session_starts_idle(self):
        machine = watch.SessionStateMachine()

        self.assertEqual(machine.status, watch.SessionStatus.IDLE)

    def test_allowed_transitions_follow_the_session_lifecycle(self):
        machine = watch.SessionStateMachine()

        self.assertTrue(machine.transition_to("WORKING"))
        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("WORKING"))
        self.assertTrue(machine.transition_to("NEEDS_APPROVAL"))
        self.assertTrue(machine.transition_to("WORKING"))
        self.assertTrue(machine.transition_to("IDLE"))
        self.assertEqual(machine.status, watch.SessionStatus.IDLE)

    def test_unknown_transitions_are_rejected_without_changing_state(self):
        machine = watch.SessionStateMachine()

        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("IDLE"))
        self.assertTrue(machine.transition_to("NEEDS_APPROVAL"))
        self.assertEqual(machine.status, watch.SessionStatus.NEEDS_APPROVAL)

        self.assertTrue(machine.transition_to("WORKING"))
        self.assertFalse(machine.transition_to("COMPLETED"))
        self.assertEqual(machine.status, watch.SessionStatus.WORKING)

        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("NEEDS_APPROVAL"))
        self.assertTrue(machine.transition_to("IDLE"))
        self.assertEqual(machine.status, watch.SessionStatus.IDLE)

    def test_attention_states_can_transition_to_every_state(self):
        machine = watch.SessionStateMachine()

        self.assertTrue(machine.transition_to("WORKING"))
        self.assertTrue(machine.transition_to("NEEDS_APPROVAL"))
        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("IDLE"))

        self.assertTrue(machine.transition_to("WORKING"))
        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("NEEDS_APPROVAL"))
        self.assertTrue(machine.transition_to("IDLE"))


class AttentionStateReaderTests(unittest.TestCase):
    def test_read_ignores_malformed_and_nested_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "sessions": [
                            {"source_pid": 41, "session_id": "root"},
                            {"source_pid": 42, "parent_id": "root"},
                            {"source_pid": 43, "parentID": "root"},
                            {"session_id": "missing pid"},
                            "not a session",
                        ]
                    }
                )
            )

            records_by_pid = watch.AttentionStateReader(state_file).read()

        self.assertEqual(
            records_by_pid, {41: {"source_pid": 41, "session_id": "root"}}
        )

    def test_read_returns_empty_mapping_for_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text("not json")

            self.assertEqual(watch.AttentionStateReader(state_file).read(), {})

    def test_read_accepts_per_process_records_in_a_status_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            status_file = Path(directory) / "101.json"
            status_file.write_text(
                json.dumps(
                    {
                        "source_pid": 101,
                        "state": "WORKING",
                        "session_id": "alpha",
                    }
                )
            )

            self.assertEqual(
                watch.AttentionStateReader(directory).read(),
                {
                    101: {
                        "source_pid": 101,
                        "state": "WORKING",
                        "session_id": "alpha",
                    }
                },
            )


class ProcessSourceTests(unittest.TestCase):
    @staticmethod
    def write_stat(path, parent, start_ticks):
        tail = " ".join(["S", str(parent), *(["0"] * 17), str(start_ticks)])
        path.write_text(f"123 (opencode){tail}")

    def test_proc_source_discovers_and_inspects_opencode_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            proc_root = Path(directory)
            project = proc_root / "project"
            project.mkdir()
            process_dir = proc_root / "101"
            process_dir.mkdir()
            (process_dir / "comm").write_text("opencode\n")
            self.write_stat(process_dir / "stat", 1, 200)
            (process_dir / "cwd").symlink_to(project, target_is_directory=True)

            ignored_dir = proc_root / "102"
            ignored_dir.mkdir()
            (ignored_dir / "comm").write_text("bash\n")

            source = watch.ProcProcessSource(
                proc_root=proc_root,
                boot_time_reader=lambda: 1000,
                clock_ticks=100,
            )

            self.assertEqual(source.opencode_pids(), {101})
            self.assertEqual(
                source.inspect(101),
                watch.ProcessInfo(101, str(project), 1002.0),
            )
            self.assertEqual(source.ancestors(101), [101])


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
        self.assertEqual(state["counts"], {
            "sessions": 2,
            "attention": 1,
            "response": 1,
            "permission": 0,
            "idle": 1,
            "working": 0,
        })
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


class FocusServiceTests(unittest.TestCase):
    def test_focus_tries_targets_in_order_and_accepts_pid_prefix(self):
        process_source = FakeProcessSource({}, {55: [55, 12]})
        tmux = FakeFocusTarget(False)
        hyprland = FakeFocusTarget(True)
        focus = watch.FocusService(process_source, [tmux, hyprland])

        focus_succeeded = focus.focus("pid:55")

        self.assertTrue(focus_succeeded)
        self.assertEqual(tmux.focus_calls, [[55, 12]])
        self.assertEqual(hyprland.focus_calls, [[55, 12]])

    def test_focus_rejects_non_numeric_targets(self):
        process_source = FakeProcessSource({}, {})
        target = FakeFocusTarget(True)

        self.assertFalse(
            watch.FocusService(process_source, [target]).focus("not-a-pid")
        )
        self.assertEqual(target.focus_calls, [])


class FocusCycleStoreTests(unittest.TestCase):
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


class TmuxClientTests(unittest.TestCase):
    def test_focus_switches_the_visible_client_to_the_matching_pane(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command[1] == "list-panes":
                return SimpleNamespace(stdout="%7\t12\n", returncode=0)
            if command[1] == "list-clients":
                return SimpleNamespace(stdout="client-1\t99\n", returncode=0)
            return SimpleNamespace(stdout="", returncode=0)

        window_focus = FakeFocusTarget(True)
        client = watch.TmuxClient(
            runner=runner,
            executable_finder=lambda _: "/usr/bin/tmux",
            client_ancestors=lambda pid: [pid, 88],
            window_focus=window_focus,
        )

        self.assertTrue(client.focus([99, 12]))
        self.assertEqual(
            calls[-1][0],
            ["tmux", "switch-client", "-c", "client-1", "-t", "%7"],
        )
        self.assertEqual(window_focus.focus_calls, [[99, 88]])

    def test_focus_reports_failure_when_pane_command_fails(self):
        def runner(command, **kwargs):
            if command[1] == "list-panes":
                return SimpleNamespace(stdout="%7\t12\n", returncode=0)
            if command[1] == "list-clients":
                return SimpleNamespace(stdout="", returncode=0)
            return SimpleNamespace(stdout="", returncode=1)

        client = watch.TmuxClient(
            runner=runner,
            executable_finder=lambda _: "/usr/bin/tmux",
        )

        self.assertFalse(client.focus([12]))


class HyprlandClientTests(unittest.TestCase):
    def test_focus_dispatches_to_a_matching_window(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command[1:3] == ["clients", "-j"]:
                return SimpleNamespace(
                    stdout=json.dumps([{"pid": 12, "address": "0xabc"}]),
                    returncode=0,
                )
            return SimpleNamespace(stdout="", returncode=0)

        client = watch.HyprlandClient(
            runner=runner,
            executable_finder=lambda _: "/usr/bin/hyprctl",
        )

        self.assertTrue(client.focus([12]))
        self.assertEqual(
            calls[-1][0],
            [
                "hyprctl",
                "dispatch",
                'hl.dsp.focus({ window = "address:0xabc" })',
            ],
        )

    def test_focus_ignores_invalid_client_payloads(self):
        def runner(command, **kwargs):
            return SimpleNamespace(stdout="{}", returncode=0)

        client = watch.HyprlandClient(
            runner=runner,
            executable_finder=lambda _: "/usr/bin/hyprctl",
        )

        self.assertFalse(client.focus([12]))


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
