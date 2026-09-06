import json
import tempfile
import unittest
from pathlib import Path

from watch_test_support import watch


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
                watch.ProcessInfo(101, str(project), 1002.0, 200),
            )
            self.assertEqual(source.ancestors(101), [101])

            (process_dir / "stat").write_text("malformed")
            self.assertIsNone(source.inspect(101).start_ticks)


if __name__ == "__main__":
    unittest.main()
