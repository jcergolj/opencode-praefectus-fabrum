import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// OpenCode Praefectus Fabrum: compact live agent counts for the Omarchy bar.
// Counts are clickable; the total count toggles the full session list.
Panel {
  id: root
  moduleName: "opencode.praefectus-fabrum"
  ipcTarget: "opencode.praefectus-fabrum"
  manageIpc: false

  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.45)
  readonly property color workingColor: "#3b82f6"
  readonly property color responseColor: "#f97316"
  readonly property color permissionColor: "#ef4444"
  readonly property color idleColor: "#22c55e"
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string watcher: Qt.resolvedUrl("bin/opencode-watch").toString().replace(/^file:\/\//, "")
  readonly property var emptyState: ({
    counts: { sessions: 0, attention: 0, response: 0, permission: 0, idle: 0, working: 0 },
    sessions: []
  })

  property var liveState: null
  property string filter: ""
  property string expandedSessionId: ""
  property int cursor: 0
  property double nowMs: Date.now()

  function setting(name, fallback) {
    var value = root.settings ? root.settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  readonly property bool coloredCounts: String(setting("coloredCounts", true)) !== "false"
  readonly property var state: liveState || emptyState
  readonly property var counts: state.counts || emptyState.counts
  readonly property var sessions: state.sessions || []

  function countFor(bucket) {
    return Number(counts[bucket] || 0)
  }

  function countColor(bucket, count) {
    if (!coloredCounts) return count > 0 ? foreground : dim
    if (bucket === "all") return foreground
    if (bucket === "working") return workingColor
    if (bucket === "response") return responseColor
    if (bucket === "permission") return permissionColor
    if (bucket === "idle") return idleColor
    return foreground
  }

  function bucketLabel(bucket) {
    return {
      all: "all OpenCode sessions",
      working: "working OpenCode agents",
      response: "waiting for response",
      permission: "waiting for permission",
      idle: "idle"
    }[bucket] || bucket
  }

  function statusLabel(status) {
    return {
      WORKING: "working",
      WAITING: "waiting for response",
      NEEDS_APPROVAL: "waiting for permission",
      IDLE: "idle"
    }[status] || String(status || "unknown").toLowerCase()
  }

  function statusColor(status) {
    if (status === "WAITING") return responseColor
    if (status === "NEEDS_APPROVAL") return permissionColor
    if (status === "IDLE") return idleColor
    if (status === "WORKING") return workingColor
    return dim
  }

  function bucketFor(session) {
    if (!session) return ""
    if (session.state === "WORKING") return "working"
    if (session.state === "WAITING") return "response"
    if (session.state === "NEEDS_APPROVAL") return "permission"
    if (session.state === "IDLE") return "idle"
    return ""
  }

  function sortedSessions() {
    var result = sessions.slice()
    result.sort(function(a, b) {
      var aAttention = a.attention ? 0 : 1
      var bAttention = b.attention ? 0 : 1
      if (aAttention !== bAttention) return aAttention - bAttention
      var aTs = Number(a.attention_since || a.last_transition_ts || 0)
      var bTs = Number(b.attention_since || b.last_transition_ts || 0)
      if (a.attention && b.attention && aTs !== bTs) return aTs - bTs
      return bTs - aTs
    })
    return result
  }

  readonly property var visibleSessions: {
    var result = sortedSessions()
    if (filter === "") return result
    return result.filter(function(session) { return bucketFor(session) === filter })
  }

  function openBucket(bucket) {
    filter = bucket
    expandedSessionId = ""
    cursor = 0
    root.open()
  }

  function openAll() {
    clearFilter()
    expandedSessionId = ""
    root.toggle()
  }

  function clearFilter() {
    filter = ""
    cursor = 0
  }

  function focusSession(sessionId) {
    if (!sessionId) return
    var session = sessions.find(function(item) { return item.session_id === sessionId })
    var target = session ? session.source_pid : sessionId
    Quickshell.execDetached([root.watcher, "--focus", String(target)])
    root.close()
  }

  function moveCursor(delta) {
    if (visibleSessions.length === 0) return
    cursor = Math.max(0, Math.min(visibleSessions.length - 1, cursor + delta))
  }

  function activateCursor() {
    if (visibleSessions.length > 0) focusSession(visibleSessions[cursor].session_id)
  }

  function toggleExpanded(sessionId) {
    expandedSessionId = expandedSessionId === sessionId ? "" : sessionId
  }

  function formatAge(timestamp) {
    var seconds = Math.max(0, nowMs / 1000 - Number(timestamp || 0))
    if (!isFinite(seconds) || seconds < 10) return "now"
    if (seconds < 60) return Math.floor(seconds) + "s"
    var minutes = Math.floor(seconds / 60)
    if (minutes < 60) return minutes + "m"
    var hours = Math.floor(minutes / 60)
    if (hours < 24) return hours + "h"
    return Math.floor(hours / 24) + "d"
  }

  function previewFor(session) {
    if (session.state === "IDLE") return statusLabel(session.state)
    if (session.preview) return String(session.preview)
    return statusLabel(session.state)
  }

  function parseState(text) {
    try {
      var parsed = JSON.parse(String(text || ""))
      if (parsed && typeof parsed === "object") {
        liveState = parsed
        nowMs = Date.now()
        if (cursor >= visibleSessions.length) cursor = Math.max(0, visibleSessions.length - 1)
      }
    } catch (error) {
      console.warn("praefectus-fabrum", "bad state line", error)
    }
  }

  Process {
    id: watcherProcess
    command: [root.watcher, "--interval", "1"]
    running: true
    stdout: SplitParser { onRead: function(data) { root.parseState(data) } }
    stderr: SplitParser {
      onRead: function(data) {
        if (String(data).trim() !== "") console.warn("praefectus-fabrum", String(data).trim())
      }
    }
  }

  Timer {
    interval: 30000
    running: true
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  implicitWidth: barRow.implicitWidth
  implicitHeight: bar ? bar.barSize : Style.bar.sizeHorizontal

  component CompactCount: WidgetButton {
    property string bucket: ""
    property string value: "0"

    bar: root.bar
    text: value
    fontSize: Style.font.bodySmall
    foreground: root.countColor(bucket, Number(value))
    horizontalMargin: 0
    verticalPadding: 0
    implicitHeight: root.bar ? root.bar.barSize : Style.bar.sizeHorizontal
    tooltipText: root.bucketLabel(bucket)

    onPressed: function(button) {
      if (button !== Qt.LeftButton) return
      if (bucket === "all") {
        root.openAll()
      }
      else root.openBucket(bucket)
    }

  }

  Row {
    id: barRow
    anchors.centerIn: parent
    spacing: 0

    CompactCount {
      bucket: "all"
      value: String(root.countFor("sessions"))
    }

    Text {
      text: ":"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.verticalCenter: parent.verticalCenter
    }

    CompactCount {
      bucket: "working"
      value: String(root.countFor("working"))
    }

    Text {
      text: "|"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.verticalCenter: parent.verticalCenter
    }

    CompactCount {
      bucket: "response"
      value: String(root.countFor("response"))
    }

    Text {
      text: "|"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.verticalCenter: parent.verticalCenter
    }

    CompactCount {
      bucket: "permission"
      value: String(root.countFor("permission"))
    }

    Text {
      text: "|"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.verticalCenter: parent.verticalCenter
    }

    CompactCount {
      bucket: "idle"
      value: String(root.countFor("idle"))
    }

  }

  IpcHandler {
    enabled: root.bar !== null
    target: root.ipcTarget

    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function clearFilter(): string { root.clearFilter(); return "all sessions" }
    function focus(sessionId: string): string { root.focusSession(sessionId); return sessionId }
  }

  KeyboardPanel {
    id: sessionPanel
    anchorItem: barRow
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: sessionPanel.fittedContentWidth(Style.space(430))
    contentHeight: sessionPanel.fittedContentHeight(panelColumn.implicitHeight + Style.space(16), Style.space(720))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.moveCursor(dy) }
      onActivateRequested: root.activateCursor()
      onCloseRequested: root.close()
      onTextKey: function(text) { if (text === "r") root.clearFilter() }

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: panelColumn.implicitHeight
        clip: true
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: panelColumn
          width: parent.width
          spacing: 0

          Text {
            width: parent.width
            text: root.filter === ""
              ? "OpenCode sessions"
              : "OpenCode · " + root.bucketLabel(root.filter)
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            bottomPadding: Style.space(7)
          }

          Text {
            width: parent.width
            text: root.counts.attention + " needing attention · " + root.counts.working + " working"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            bottomPadding: Style.space(8)
          }

          Rectangle { width: parent.width; height: 1; color: root.dim }

          Repeater {
            model: root.visibleSessions

            delegate: Item {
              required property var modelData
              required property int index
              readonly property bool expanded: root.expandedSessionId === modelData.session_id
              readonly property bool selected: index === root.cursor
              width: panelColumn.width
              height: expanded ? Style.space(104) : Style.space(60)

              Rectangle {
                anchors.fill: parent
                anchors.topMargin: Style.space(2)
                anchors.bottomMargin: Style.space(2)
                radius: Style.cornerRadius
                color: selected ? Util.alpha(root.foreground, 0.10) : "transparent"
              }

              Row {
                anchors.fill: parent
                anchors.leftMargin: Style.space(8)
                anchors.rightMargin: Style.space(8)
                spacing: Style.space(8)

                Text {
                  width: Style.space(12)
                  anchors.top: parent.top
                  anchors.topMargin: Style.space(12)
                  text: modelData.attention ? "*" : "."
                  color: root.statusColor(modelData.state)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  horizontalAlignment: Text.AlignHCenter
                }

                Column {
                  width: parent.width - Style.space(12) - Style.space(8) - Style.space(22)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(1)

                  Row {
                    width: parent.width
                    spacing: Style.space(5)

                    Text {
                      text: String(modelData.project || "OpenCode")
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.bodySmall
                      font.bold: !!modelData.attention
                      elide: Text.ElideRight
                      width: Math.max(1, parent.width - ageText.implicitWidth - Style.space(5))
                    }

                    Text {
                      id: ageText
                      text: root.formatAge(modelData.attention_since || modelData.last_transition_ts)
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }

                  Text {
                    width: parent.width
                    text: root.previewFor(modelData)
                    color: root.statusColor(modelData.state)
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                    maximumLineCount: expanded ? 3 : 1
                    wrapMode: expanded ? Text.Wrap : Text.NoWrap
                  }

                  Text {
                    visible: expanded
                    width: parent.width
                    text: (modelData.tmux_pane ? "tmux " + modelData.tmux_pane : "terminal")
                      + (modelData.directory ? " · " + modelData.directory : "")
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideMiddle
                  }
                }

                Text {
                  width: Style.space(22)
                  anchors.verticalCenter: parent.verticalCenter
                  text: expanded ? "v" : ">"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  horizontalAlignment: Text.AlignHCenter
                }
              }

              MouseArea {
                anchors.fill: parent
                anchors.rightMargin: Style.space(26)
                acceptedButtons: Qt.LeftButton
                onClicked: root.focusSession(modelData.session_id)
              }

              MouseArea {
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: Style.space(30)
                acceptedButtons: Qt.LeftButton
                onClicked: root.toggleExpanded(modelData.session_id)
              }
            }
          }

          Text {
            visible: root.visibleSessions.length === 0
            width: parent.width
            text: root.filter === "" ? "No OpenCode sessions tracked" : "No sessions in this category"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            topPadding: Style.space(12)
          }

          Text {
            width: parent.width
            text: "click row to focus · click row arrow to preview · r clears filter"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            topPadding: Style.space(12)
          }
        }
      }
    }
  }
}
