# OpenCode Praefectus Fabrum

## What This Package Does

OpenCode Praefectus Fabrum is an Omarchy bar widget for monitoring and
switching between running OpenCode sessions.

The widget shows compact live counts in the bar:

```text
total:working|response|permission|idle
```

- `total`: all top-level OpenCode processes currently running.
- `working`: sessions currently working.
- `response`: sessions waiting for a response.
- `permission`: sessions waiting for permission.
- `idle`: sessions currently idle.

The counts are clickable. Click the total to open the complete session list.
Click a status count to open its filtered list. Click a session row to focus its
terminal or tmux pane.

Expand a session row to see its latest context-window percentage when OpenCode
provides model and token usage metadata.

When the session list is open, expand `Settings` to toggle colored counter
numbers and session notifications. Turn colored counters off to show every
counter number in white. Session notifications are enabled by default and
appear when a session needs attention or finishes. They close after 10 seconds
by default; the timeout can be changed between 8 and 30 seconds. Clicking a
notification focuses its session. These choices are saved in the widget
configuration.

The widget watches live top-level `opencode` processes. Nested OpenCode
processes created by subagents are ignored. A new process starts as idle until
its status is reported.

## Why This Name

*Praefectus fabrum* was a Roman title for an officer responsible for skilled
craftsmen, engineers, and technical workers. OpenCode agents are modern
technical workers, and this widget organizes their work and directs you to the
session that needs attention.

## Keyboard Shortcuts

The watcher provides a focus command for each session state. Add these
keybindings to your Hyprland bindings file, for example
`~/.config/hypr/bindings.conf`:

```ini
bind = SUPER ALT, W, exec, ~/.config/omarchy/plugins/opencode.praefectus-fabrum/bin/opencode-watch --focus-state working
bind = SUPER ALT, R, exec, ~/.config/omarchy/plugins/opencode.praefectus-fabrum/bin/opencode-watch --focus-state response
bind = SUPER ALT, P, exec, ~/.config/omarchy/plugins/opencode.praefectus-fabrum/bin/opencode-watch --focus-state permission
bind = SUPER ALT, I, exec, ~/.config/omarchy/plugins/opencode.praefectus-fabrum/bin/opencode-watch --focus-state idle
bind = SUPER ALT, TAB, exec, ~/.config/omarchy/plugins/opencode.praefectus-fabrum/bin/opencode-watch --focus-next
bind = SUPER ALT SHIFT, TAB, exec, ~/.config/omarchy/plugins/opencode.praefectus-fabrum/bin/opencode-watch --focus-previous
```

The shortcuts are:

```text
SUPER + ALT + W  focus a working session
SUPER + ALT + R  focus a session waiting for a response
SUPER + ALT + P  focus a session waiting for permission
SUPER + ALT + I  focus an idle session
SUPER + ALT + TAB          focus the next session, regardless of state
SUPER + ALT + SHIFT + TAB  focus the previous session, regardless of state
```

Each state shortcut starts with the first matching session and repeated presses
cycle forward. `SUPER + ALT + TAB` starts with the first tracked session, while
`SUPER + ALT + SHIFT + TAB` starts with the last. Both shortcuts wrap around
and include sessions in every state. Reload Hyprland after adding the bindings:

```bash
hyprctl reload
```

If the plugin was installed somewhere else, replace the plugin path in each
binding with the actual path to `bin/opencode-watch`.

## Prerequisites

- Omarchy with its bar and plugin support.
- The `omarchy` command for installing the bar plugin.
- OpenCode, if you want OpenCode sessions to be tracked.
- Python 3. The watcher uses only the Python standard library.
- Hyprland, for focusing OpenCode windows.
- `tmux` is optional. It is used when an OpenCode session runs in a tmux pane.
- No npm packages are required.

## Installation

Install the Omarchy bar plugin:

```bash
omarchy plugin add https://github.com/jcergolj/opencode-praefectus-fabrum.git --enable
```

The total and process-based idle counts work without the OpenCode status
bridge. To receive live working, response, and permission states, link the
bundled OpenCode plugin and restart OpenCode:

```bash
mkdir -p ~/.config/opencode/plugins
ln -sfn \
  ~/.config/omarchy/plugins/opencode.praefectus-fabrum/opencode-plugin/index.js \
  ~/.config/opencode/plugins/opencode-praefectus-fabrum.js
```

The bridge writes per-process status records under `$XDG_RUNTIME_DIR`, or
`~/.cache` when that variable is not set. The widget does not scrape terminal
output or access OpenCode's private storage.

Status records are matched to live processes using exact Linux process start
ticks, avoiding differences between JavaScript and kernel epoch estimates.
When ticks are unavailable (including records from older bridges), the watcher
retains its five-second process-start timestamp tolerance.

## Watcher Architecture

The watcher is organized as a small Python package under `bin/opencode_watch`:

- `domain.py`: session models and lifecycle state rules.
- `sources.py`: Linux `/proc` and runtime status-file adapters.
- `snapshots.py`: process collection and the QML snapshot contract.
- `focus.py`: tmux, Hyprland, and focus-target adapters.
- `cycling.py`: session selection and persistent focus cycling.
- `cli.py`: dependency composition and command-line behavior.

The `bin/opencode-watch` executable remains the only runtime entry point. Each
adapter is injected through a small protocol, so collection and focus policy can
be tested without a live desktop.

## Tests

Run the watcher and bridge tests from the repository root:

```bash
python3 -m unittest discover -s tests -v
node --test tests/test_opencode_plugin.mjs
```
