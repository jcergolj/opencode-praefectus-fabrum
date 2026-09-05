import json
import unittest
from types import SimpleNamespace

from watch_test_support import FakeFocusTarget, FakeProcessSource, watch


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


if __name__ == "__main__":
    unittest.main()
