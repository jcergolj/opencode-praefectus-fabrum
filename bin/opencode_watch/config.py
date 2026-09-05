"""Runtime configuration for the OpenCode watcher."""

import os


RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", os.path.expanduser("~/.cache"))
# Status metadata is optional; live process counting never depends on it.
STATUS_RECORD_PATH = os.environ.get(
    "OPENCODE_STATUS_FILE",
    os.path.join(RUNTIME_DIR, "opencode-praefectus-fabrum"),
)
FOCUS_CYCLE_STATE_FILE = os.path.join(RUNTIME_DIR, "opencode-focus-cycle.json")
# OpenCode can initialize the plugin a few seconds after the process starts.
PROCESS_START_TOLERANCE = 5.0
