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
    def __init__(self, processes, process_ancestors):
        self.processes = processes
        self.process_ancestors = process_ancestors

    def opencode_pids(self):
        return set(self.processes)

    def inspect(self, pid):
        return self.processes.get(pid)

    def ancestors(self, pid):
        return self.process_ancestors.get(pid, [pid])


class FakeAttentionSource:
    def __init__(self, sessions):
        self.sessions = sessions

    def read(self):
        return self.sessions


class FakeTerminal:
    def __init__(self, pane=None):
        self.pane = pane
        self.panes_calls = 0

    def panes(self):
        self.panes_calls += 1
        return [self.pane] if self.pane else []


class FakeSnapshotSource:
    def __init__(self, states):
        self.states = iter(states)
        self.last_state = None

    def snapshot(self):
        try:
            self.last_state = next(self.states)
        except StopIteration:
            pass
        return self.last_state


class FakeFocusTarget:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def focus(self, ancestors):
        self.calls.append(ancestors)
        return self.result


class FakeFocusService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def focus(self, target):
        self.calls.append(target)
        return self.result


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

            sessions = watch.AttentionStateReader(state_file).read()

        self.assertEqual(sessions, {41: {"source_pid": 41, "session_id": "root"}})

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

        attention_source.sessions[101]["state"] = "WORKING"
        self.assertEqual(collector.collect()[0].state, "WORKING")

        attention_source.sessions[101]["state"] = "NEEDS_APPROVAL"
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

        process_source.processes.clear()
        self.assertEqual(collector.collect(), [])

        process_source.processes[101] = watch.ProcessInfo(101, "/work/alpha", 200)
        attention_source.sessions[101]["state"] = "WAITING"
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
                },
                303: {"session_id": "beta", "state": "IDLE"},
            }
        )
        terminal = FakeTerminal(watch.TmuxPane("%1", 10))
        collector = watch.SessionCollector(process_source, attention_source, terminal)
        snapshots = watch.SnapshotService(collector, clock=lambda: 1234)

        collector.collect()
        attention_source.sessions[101]["state"] = "WAITING"
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
        self.assertEqual(terminal.panes_calls, 2)

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
        attention_source.sessions[101]["state"] = "NEEDS_APPROVAL"

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

        result = focus.focus("pid:55")

        self.assertTrue(result)
        self.assertEqual(tmux.calls, [[55, 12]])
        self.assertEqual(hyprland.calls, [[55, 12]])

    def test_focus_rejects_non_numeric_targets(self):
        process_source = FakeProcessSource({}, {})
        target = FakeFocusTarget(True)

        self.assertFalse(
            watch.FocusService(process_source, [target]).focus("not-a-pid")
        )
        self.assertEqual(target.calls, [])


class FocusCycleStoreTests(unittest.TestCase):
    def test_working_sessions_are_candidates_without_attention(self):
        state = {
            "sessions": [
                {"source_pid": 101, "state": "WORKING", "attention": False},
                {"source_pid": 202, "state": "WAITING", "attention": True},
            ]
        }

        self.assertEqual(
            watch.focus_target_for_state(state, "working"),
            101,
        )

    def test_repeated_focus_cycles_per_status(self):
        state = {
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
        focused = []

        with tempfile.TemporaryDirectory() as directory:
            store = watch.FocusCycleStore(Path(directory) / "focus.json")
            focus = lambda target: focused.append(target) or True

            self.assertTrue(store.focus(state, "working", focus))
            self.assertTrue(store.focus(state, "working", focus))
            self.assertTrue(store.focus(state, "working", focus))

        self.assertEqual(focused, [101, 202, 101])


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
        self.assertEqual(window_focus.calls, [[99, 88]])

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
        first = {"counts": {"sessions": 1}, "sessions": [session]}
        changed = {
            "counts": {"sessions": 1},
            "sessions": [{**session, "preview": "second"}],
        }
        source = FakeSnapshotSource([first, changed])
        output = []
        sleeps = []

        def stop_after_two_snapshots(interval):
            sleeps.append(interval)
            if len(sleeps) == 2:
                raise StopIteration

        streamer = watch.SnapshotStreamer(
            source,
            interval=1,
            sleep=stop_after_two_snapshots,
            emit=output.append,
        )

        with self.assertRaises(StopIteration):
            streamer.run()

        self.assertEqual(len(output), 2)
        self.assertEqual(json.loads(output[1])["sessions"][0]["preview"], "second")


class CommandLineTests(unittest.TestCase):
    def test_focus_option_is_handled_even_for_an_empty_argument(self):
        focus = FakeFocusService(True)

        result = watch.main(
            ["--focus", ""],
            snapshot_source=SimpleNamespace(snapshot=lambda: {}),
            focus_service=focus,
        )

        self.assertEqual(result, 0)
        self.assertEqual(focus.calls, [""])

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
                result = watch.main(
                    ["--focus-state", "permission"],
                    snapshot_source=SimpleNamespace(snapshot=lambda: snapshot),
                    focus_service=focus,
                )

        self.assertEqual(result, 0)
        self.assertEqual(focus.calls, [101])

    def test_focus_state_returns_failure_when_no_session_matches(self):
        focus = FakeFocusService(True)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                watch,
                "FOCUS_CYCLE_STATE_FILE",
                str(Path(directory) / "focus.json"),
            ):
                result = watch.main(
                    ["--focus-state", "response"],
                    snapshot_source=SimpleNamespace(snapshot=lambda: {"sessions": []}),
                    focus_service=focus,
                )

        self.assertEqual(result, 1)
        self.assertEqual(focus.calls, [])

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

        self.assertEqual(focus.calls, [101, 202])


if __name__ == "__main__":
    unittest.main()
