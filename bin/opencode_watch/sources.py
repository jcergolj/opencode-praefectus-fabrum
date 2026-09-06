"""Operating-system and runtime-file adapters used by the watcher."""

import json
import os
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple, Union

from .domain import ProcessInfo


class AttentionStateReader:
    """Read optional status records indexed by source PID."""

    def __init__(self, status_path: Union[os.PathLike, str]):
        self.status_path = os.fspath(status_path)

    @staticmethod
    def _read_file(record_path: str) -> Dict[int, Mapping[str, Any]]:
        try:
            with open(record_path, encoding="utf-8") as state_file:
                status_document = json.load(state_file)
        except (OSError, ValueError, TypeError):
            return {}

        records_by_pid: Dict[int, Mapping[str, Any]] = {}
        status_records = (
            status_document.get("sessions")
            if isinstance(status_document, dict)
            else None
        )
        if isinstance(status_records, list):
            status_records = [
                status_record
                for status_record in status_records
                if isinstance(status_record, dict)
            ]
        elif isinstance(status_document, dict):
            status_records = [status_document]
        else:
            status_records = []

        for status_record in status_records:
            if status_record.get("parent_id") or status_record.get("parentID"):
                continue
            try:
                source_pid = int(status_record["source_pid"])
            except (KeyError, TypeError, ValueError):
                continue
            records_by_pid[source_pid] = status_record
        return records_by_pid

    def read(self) -> Dict[int, Mapping[str, Any]]:
        if not os.path.isdir(self.status_path):
            return self._read_file(self.status_path)

        records_by_pid: Dict[int, Mapping[str, Any]] = {}
        try:
            status_entries = os.scandir(self.status_path)
        except OSError:
            return records_by_pid

        with status_entries:
            for status_entry in status_entries:
                if not status_entry.name.endswith(".json") or not status_entry.is_file():
                    continue
                records_by_pid.update(self._read_file(status_entry.path))
        return records_by_pid


def process_stat(
    pid: int,
    proc_root: Union[os.PathLike, str] = "/proc",
) -> Tuple[Optional[int], int]:
    try:
        stat_path = os.path.join(os.fspath(proc_root), str(pid), "stat")
        with open(stat_path, encoding="utf-8") as stat_file:
            stat_fields = stat_file.read().rsplit(")", 1)[1].split()
        return int(stat_fields[1]), int(stat_fields[19])
    except (OSError, IndexError, ValueError):
        return None, 0


def read_boot_time(proc_root: Union[os.PathLike, str] = "/proc") -> float:
    try:
        proc_stat_path = os.path.join(os.fspath(proc_root), "stat")
        with open(proc_stat_path, encoding="utf-8") as stat_file:
            for line in stat_file:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return time.time()


class ProcProcessSource:
    """Adapt Linux /proc process information to the process source interface."""

    def __init__(
        self,
        proc_root: Union[os.PathLike, str] = "/proc",
        boot_time_reader: Optional[Callable[[], float]] = None,
        clock_ticks: Optional[int] = None,
    ):
        self.proc_root = os.fspath(proc_root)
        read_boot = boot_time_reader or (lambda: read_boot_time(self.proc_root))
        self.boot_time = read_boot()
        self.clock_ticks = (
            clock_ticks
            if clock_ticks is not None
            else os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        )

    def _comm(self, pid: int) -> Optional[str]:
        try:
            comm_path = os.path.join(self.proc_root, str(pid), "comm")
            with open(comm_path, encoding="utf-8") as comm_file:
                return comm_file.read().strip()
        except OSError:
            return None

    def opencode_pids(self) -> Set[int]:
        opencode_pids: Set[int] = set()
        try:
            process_entries = os.scandir(self.proc_root)
        except OSError:
            return opencode_pids

        with process_entries:
            for process_entry in process_entries:
                if not process_entry.name.isdigit():
                    continue
                try:
                    process_pid = int(process_entry.name)
                except ValueError:
                    continue
                if self._comm(process_pid) == "opencode":
                    opencode_pids.add(process_pid)
        return opencode_pids

    def ancestors(self, pid: int) -> List[int]:
        ancestor_pids: List[int] = []
        current_pid = pid
        seen_pids: Set[int] = set()
        while current_pid > 1 and current_pid not in seen_pids:
            ancestor_pids.append(current_pid)
            seen_pids.add(current_pid)
            parent_pid, _ = process_stat(current_pid, self.proc_root)
            if not parent_pid:
                break
            current_pid = parent_pid
        return ancestor_pids

    def inspect(self, pid: int) -> Optional[ProcessInfo]:
        if self._comm(pid) != "opencode":
            return None
        try:
            cwd_path = os.path.join(self.proc_root, str(pid), "cwd")
            directory = os.path.realpath(os.readlink(cwd_path))
        except (OSError, ValueError):
            return None

        parent_pid, process_start_ticks = process_stat(pid, self.proc_root)
        process_started_at = self.boot_time + process_start_ticks / self.clock_ticks
        return ProcessInfo(
            pid,
            directory,
            process_started_at,
            process_start_ticks if parent_pid is not None else None,
        )
