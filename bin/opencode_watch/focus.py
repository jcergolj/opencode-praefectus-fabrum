"""Desktop focus adapters for tmux and Hyprland."""

import json
import shutil
import subprocess
from typing import Any, Callable, Iterable, List, Optional, Sequence

from .domain import FocusTarget, ProcessSource, TmuxPane


def command_succeeded(command_result: Any) -> bool:
    return command_result is not None and getattr(command_result, "returncode", 0) == 0


class TmuxClient:
    """Use tmux when it is available for pane discovery and focusing."""

    def __init__(
        self,
        runner: Callable[..., Any] = subprocess.run,
        executable_finder: Callable[[str], Optional[str]] = shutil.which,
        client_ancestors: Optional[Callable[[int], Iterable[int]]] = None,
        window_focus: Optional[FocusTarget] = None,
    ):
        self.runner = runner
        self.executable_finder = executable_finder
        self.client_ancestors = client_ancestors
        self.window_focus = window_focus

    def _run(self, command_args: Sequence[str]) -> Any:
        try:
            return self.runner(
                ["tmux", *command_args],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None

    def panes(self) -> List[TmuxPane]:
        if not self.executable_finder("tmux"):
            return []
        pane_list_result = self._run(
            ["list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"]
        )
        if pane_list_result is None:
            return []

        panes: List[TmuxPane] = []
        for line in (getattr(pane_list_result, "stdout", "") or "").splitlines():
            pane_fields = line.split("\t")
            if len(pane_fields) != 2:
                continue
            pane_id, pane_pid = pane_fields
            try:
                panes.append(TmuxPane(pane_id, int(pane_pid)))
            except ValueError:
                continue
        return panes

    def pane_for(self, ancestors: Iterable[int]) -> Optional[TmuxPane]:
        ancestor_set = set(ancestors)
        return next((pane for pane in self.panes() if pane.pid in ancestor_set), None)

    def focus(self, ancestors: Iterable[int]) -> bool:
        pane = self.pane_for(ancestors)
        if pane is None:
            return False

        client_list_result = self._run(
            ["list-clients", "-F", "#{client_name}\t#{client_pid}"]
        )
        selected_client = None
        for line in (
            getattr(client_list_result, "stdout", "") or ""
        ).splitlines():
            client_fields = line.split("\t")
            if not client_fields or not client_fields[0]:
                continue
            client_pid = None
            if len(client_fields) > 1:
                try:
                    client_pid = int(client_fields[1])
                except ValueError:
                    pass
            selected_client = (client_fields[0], client_pid)
            break

        if selected_client:
            client_name, client_pid = selected_client
            focus_command_result = self._run(
                ["switch-client", "-c", client_name, "-t", pane.id]
            )
            if not command_succeeded(focus_command_result):
                return False
            # Switching panes does not raise the terminal window when the
            # widget lives on another workspace. Focus the tmux client itself,
            # whose process ancestry contains the terminal emulator.
            if (
                client_pid is not None
                and self.client_ancestors
                and self.window_focus
            ):
                self.window_focus.focus(self.client_ancestors(client_pid))
            return True
        else:
            focus_command_result = self._run(["select-pane", "-t", pane.id])
            return command_succeeded(focus_command_result)


class HyprlandClient:
    """Use Hyprland as the fallback focus target outside tmux."""

    def __init__(
        self,
        runner: Callable[..., Any] = subprocess.run,
        executable_finder: Callable[[str], Optional[str]] = shutil.which,
    ):
        self.runner = runner
        self.executable_finder = executable_finder

    def focus(self, ancestors: Iterable[int]) -> bool:
        if not self.executable_finder("hyprctl"):
            return False

        try:
            client_list_result = self.runner(
                ["hyprctl", "clients", "-j"],
                capture_output=True,
                text=True,
                check=True,
            )
            client_records = json.loads(client_list_result.stdout)
        except (
            OSError,
            subprocess.CalledProcessError,
            json.JSONDecodeError,
            TypeError,
            AttributeError,
        ):
            return False

        if not isinstance(client_records, list):
            return False
        ancestor_set = set(ancestors)
        for client_record in client_records:
            if not isinstance(client_record, dict):
                continue
            if (
                client_record.get("pid") not in ancestor_set
                or not client_record.get("address")
            ):
                continue
            try:
                focus_command_result = self.runner(
                    [
                        "hyprctl",
                        "dispatch",
                        f'hl.dsp.focus({{ window = "address:{client_record["address"]}" }})',
                    ],
                    check=False,
                )
            except OSError:
                return False
            return command_succeeded(focus_command_result)
        return False


def parse_pid(target: Any) -> Optional[int]:
    try:
        target_text = str(target)
        return int(target_text.removeprefix("pid:"))
    except (TypeError, ValueError):
        return None


class FocusService:
    """Try the available desktop focus targets in their configured order."""

    def __init__(self, process_source: ProcessSource, targets: Sequence[FocusTarget]):
        self.process_source = process_source
        self.targets = targets

    def focus(self, target: Any) -> bool:
        process_pid = parse_pid(target)
        if process_pid is None:
            return False
        process_ancestors = self.process_source.ancestors(process_pid)
        return any(
            focus_target.focus(process_ancestors) for focus_target in self.targets
        )
