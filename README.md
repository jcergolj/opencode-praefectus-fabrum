# OpenCode Praefectus Fabrum

An Omarchy bar widget for OpenCode sessions managed by
[agent-tesserarius](https://github.com/jcergolj/agent-tesserarius). It shows
compact attention counts in the right side of the bar:

```text
response|permission|idle↓
```

Install the daemon first, then install the widget:

```bash
git clone git@github.com:jcergolj/agent-tesserarius.git
cd agent-tesserarius
./install.sh
omarchy plugin add git@github.com:jcergolj/opencode-praefectus-fabrum.git --enable
```

Click a count to focus its only matching session. When several sessions share
the status, the count opens a filtered list. The down arrow opens the complete
session panel, where each row focuses the selected terminal or tmux pane.

The name comes from the Roman *praefectus fabrum*, an officer responsible for
the *fabri*: skilled craftsmen, engineers, and technical workers. OpenCode
agents are the modern *fabri*, while this widget organizes their work and
directs the user to the agent that needs attention.

The widget reads the local daemon state file. It does not scrape terminal
output or access OpenCode's private storage.
