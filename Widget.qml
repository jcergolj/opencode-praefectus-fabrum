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
  readonly property string watcherPath: Qt.resolvedUrl("bin/opencode-watch").toString().replace(/^file:\/\//, "")
  readonly property var emptySnapshot: ({
    counts: { sessions: 0, attention: 0, response: 0, permission: 0, idle: 0, working: 0 },
    sessions: []
  })

  property var liveSnapshot: null
  property string activeFilter: ""
  property string expandedSessionId: ""
  property bool settingsOpen: false
  property int selectedSessionIndex: 0
  property double currentTimeMs: Date.now()
  property var previousSessionsByIdentity: ({})
  property bool hasPreviousSnapshot: false

  function setting(settingName, defaultValue) {
    var settingValue = root.settings ? root.settings[settingName] : undefined
    return settingValue === undefined || settingValue === null ? defaultValue : settingValue
  }

  function boundedNotificationTimeout(value) {
    var timeoutSeconds = Number(value)
    if (!isFinite(timeoutSeconds)) timeoutSeconds = 10
    return Math.max(8, Math.min(30, Math.round(timeoutSeconds)))
  }

  readonly property bool coloredCounts: String(setting("coloredCounts", true)) !== "false"
  readonly property bool notificationsEnabled: String(setting("notificationsEnabled", true)) !== "false"
  readonly property int notificationTimeoutSeconds: boundedNotificationTimeout(setting("notificationTimeoutSeconds", 10))
  readonly property int notificationTimeoutMs: notificationTimeoutSeconds * 1000
  readonly property var snapshot: liveSnapshot || emptySnapshot
  readonly property var counts: snapshot.counts || emptySnapshot.counts
  readonly property var sessions: snapshot.sessions || []

  function updateSetting(settingName, settingValue) {
    var updatedSettings = Object.assign({}, root.settings)
    updatedSettings[settingName] = settingValue
    root.settings = updatedSettings
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, updatedSettings)
  }

  function setColoredCounts(enabled) {
    updateSetting("coloredCounts", enabled)
  }

  function setNotificationsEnabled(enabled) {
    updateSetting("notificationsEnabled", enabled)
  }

  function setNotificationTimeout(seconds) {
    updateSetting("notificationTimeoutSeconds", boundedNotificationTimeout(seconds))
  }

  function countFor(statusBucket) {
    return Number(counts[statusBucket] || 0)
  }

  function countColor(statusBucket) {
    if (!coloredCounts) return foreground
    if (statusBucket === "all") return foreground
    if (statusBucket === "working") return workingColor
    if (statusBucket === "response") return responseColor
    if (statusBucket === "permission") return permissionColor
    if (statusBucket === "idle") return idleColor
    return foreground
  }

  function bucketLabel(statusBucket) {
    return {
      all: "all OpenCode sessions",
      working: "working OpenCode agents",
      response: "waiting for response",
      permission: "waiting for permission",
      idle: "idle"
    }[statusBucket] || statusBucket
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
    var sessionStatus = String(session.state || "").trim().toUpperCase()
    if (sessionStatus === "WORKING") return "working"
    if (sessionStatus === "WAITING") return "response"
    if (sessionStatus === "NEEDS_APPROVAL") return "permission"
    if (sessionStatus === "IDLE") return "idle"
    return ""
  }

  function sortedSessions() {
    var sortedSessionList = sessions.slice()
    sortedSessionList.sort(function(leftSession, rightSession) {
      var leftAttentionRank = leftSession.attention ? 0 : 1
      var rightAttentionRank = rightSession.attention ? 0 : 1
      if (leftAttentionRank !== rightAttentionRank)
        return leftAttentionRank - rightAttentionRank
      var leftTimestamp = Number(leftSession.attention_since || leftSession.last_transition_ts || 0)
      var rightTimestamp = Number(rightSession.attention_since || rightSession.last_transition_ts || 0)
      if (leftSession.attention && rightSession.attention && leftTimestamp !== rightTimestamp)
        return leftTimestamp - rightTimestamp
      return rightTimestamp - leftTimestamp
    })
    return sortedSessionList
  }

  readonly property var visibleSessions: {
    var filteredSessionList = sortedSessions()
    if (activeFilter === "") return filteredSessionList
    return filteredSessionList.filter(function(session) { return bucketFor(session) === activeFilter })
  }

  function openBucket(statusBucket) {
    activeFilter = statusBucket
    expandedSessionId = ""
    selectedSessionIndex = 0
    root.open()
  }

  function openAll() {
    clearFilter()
    expandedSessionId = ""
    root.toggle()
  }

  function clearFilter() {
    activeFilter = ""
    selectedSessionIndex = 0
  }

  function focusSession(sessionId, sourcePid) {
    if (!sessionId && (sourcePid === undefined || sourcePid === null || String(sourcePid) === "")) return
    var selectedSession = sessionId
      ? sessions.find(function(item) { return item.session_id === sessionId })
      : null
    var focusTarget = selectedSession
      ? selectedSession.source_pid
      : (sourcePid !== undefined && sourcePid !== null && String(sourcePid) !== "" ? sourcePid : sessionId)
    Quickshell.execDetached([root.watcherPath, "--focus", String(focusTarget)])
    root.close()
  }

  function moveCursor(cursorDelta) {
    if (visibleSessions.length === 0) return
    selectedSessionIndex = Math.max(0, Math.min(visibleSessions.length - 1, selectedSessionIndex + cursorDelta))
  }

  function activateCursor() {
    if (visibleSessions.length > 0) focusSession(visibleSessions[selectedSessionIndex].session_id)
  }

  function toggleExpanded(sessionId) {
    expandedSessionId = expandedSessionId === sessionId ? "" : sessionId
  }

  function formatAge(transitionTimestamp) {
    var seconds = Math.max(0, currentTimeMs / 1000 - Number(transitionTimestamp || 0))
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

  function contextUsageLabel(session) {
    var contextPercentage = Number(session.context_percentage)
    if (!isFinite(contextPercentage)) return "context usage unavailable"
    return "context " + Math.round(contextPercentage) + "% used"
  }

  function sessionIdentity(session) {
    if (!session) return ""
    if (session.source_pid !== undefined && session.source_pid !== null && String(session.source_pid) !== "")
      return "pid:" + String(session.source_pid)
    if (session.session_id !== undefined && session.session_id !== null && String(session.session_id) !== "")
      return "session:" + String(session.session_id)
    return ""
  }

  function sessionsByIdentity(sessionList) {
    var records = {}
    for (var index = 0; index < sessionList.length; index++) {
      var session = sessionList[index]
      var identity = sessionIdentity(session)
      if (identity) records[identity] = session
    }
    return records
  }

  function requiresAttention(session) {
    if (!session) return false
    return !!session.attention || session.state === "WAITING" || session.state === "NEEDS_APPROVAL"
  }

  function notificationEvent(previousSession, currentSession) {
    if (!currentSession) return ""
    if (requiresAttention(currentSession) && !requiresAttention(previousSession)) return "attention"
    if (previousSession && previousSession.state === "WORKING" && currentSession.state === "IDLE")
      return "finished"
    return ""
  }

  function notificationSummary(eventType) {
    return eventType === "attention"
      ? "OpenCode session needs attention"
      : "OpenCode session finished"
  }

  function notificationBody(eventType, session) {
    var project = String(session.project || "OpenCode")
    if (eventType !== "attention") return project
    var preview = String(session.preview || "")
    if (!preview || preview === statusLabel(session.state)) return project
    return project + " · " + preview
  }

  function sendNotification(eventType, session) {
    if (!notificationsEnabled || !session) return
    var notificationProcess = desktopNotificationProcess.createObject(root, {
      targetSessionId: session.session_id === undefined || session.session_id === null ? "" : String(session.session_id),
      targetSourcePid: session.source_pid === undefined || session.source_pid === null ? "" : String(session.source_pid),
      notificationSummary: notificationSummary(eventType),
      notificationBody: notificationBody(eventType, session)
    })
    if (!notificationProcess) console.warn("praefectus-fabrum", "could not create notification process")
  }

  function notifyForTransitions(currentSessions) {
    if (!hasPreviousSnapshot) return
    var currentSessionsByIdentity = sessionsByIdentity(currentSessions)
    for (var identity in currentSessionsByIdentity) {
      var currentSession = currentSessionsByIdentity[identity]
      var eventType = notificationEvent(previousSessionsByIdentity[identity], currentSession)
      if (eventType) sendNotification(eventType, currentSession)
    }
    previousSessionsByIdentity = currentSessionsByIdentity
  }

  function parseState(inputText) {
    try {
      var parsedSnapshot = JSON.parse(String(inputText || ""))
      if (parsedSnapshot && typeof parsedSnapshot === "object") {
        var currentSessions = Array.isArray(parsedSnapshot.sessions) ? parsedSnapshot.sessions : []
        liveSnapshot = parsedSnapshot
        currentTimeMs = Date.now()
        if (selectedSessionIndex >= visibleSessions.length)
          selectedSessionIndex = Math.max(0, visibleSessions.length - 1)
        if (!hasPreviousSnapshot) {
          previousSessionsByIdentity = sessionsByIdentity(currentSessions)
          hasPreviousSnapshot = true
        }
        else notifyForTransitions(currentSessions)
      }
    } catch (parseError) {
      console.warn("praefectus-fabrum", "bad state line", parseError)
    }
  }

  Process {
    id: watcherProcess
    command: [root.watcherPath, "--interval", "1"]
    running: true
    stdout: SplitParser { onRead: function(outputChunk) { root.parseState(outputChunk) } }
    stderr: SplitParser {
      onRead: function(outputChunk) {
        if (String(outputChunk).trim() !== "") console.warn("praefectus-fabrum", String(outputChunk).trim())
      }
    }
  }

  Component {
    id: desktopNotificationProcess

    Process {
      id: notificationProcess
      property string targetSessionId: ""
      property string targetSourcePid: ""
      property string notificationSummary: ""
      property string notificationBody: ""
      property bool actionHandled: false

      command: [
        "notify-send",
        "--wait",
        "--transient",
        "--action=default=Open",
        "--expire-time",
        String(root.notificationTimeoutMs),
        "--app-name",
        "OpenCode",
        notificationSummary,
        notificationBody
      ]
      running: true

      stdout: SplitParser {
        onRead: function(outputChunk) {
          if (String(outputChunk).trim() !== "default" || notificationProcess.actionHandled) return
          notificationProcess.actionHandled = true
          root.focusSession(notificationProcess.targetSessionId, notificationProcess.targetSourcePid)
        }
      }

      stderr: SplitParser {
        onRead: function(outputChunk) {
          if (String(outputChunk).trim() !== "") console.warn("praefectus-fabrum", String(outputChunk).trim())
        }
      }

      onExited: notificationProcess.destroy()
    }
  }

  Timer {
    interval: 30000
    running: true
    repeat: true
    onTriggered: root.currentTimeMs = Date.now()
  }

  implicitWidth: barRow.implicitWidth
  implicitHeight: bar ? bar.barSize : Style.bar.sizeHorizontal

  component CompactCount: WidgetButton {
    property string statusBucket: ""
    property string displayValue: "0"

    bar: root.bar
    text: displayValue
    fontSize: Style.font.bodySmall
    foreground: root.countColor(statusBucket)
    horizontalMargin: 0
    verticalPadding: 0
    implicitHeight: root.bar ? root.bar.barSize : Style.bar.sizeHorizontal
    tooltipText: root.bucketLabel(statusBucket)

    onPressed: function(button) {
      if (button !== Qt.LeftButton) return
      if (statusBucket === "all") {
        root.openAll()
      }
      else root.openBucket(statusBucket)
    }

  }

  Row {
    id: barRow
    anchors.centerIn: parent
    spacing: 0

    CompactCount {
      statusBucket: "all"
      displayValue: String(root.countFor("sessions"))
    }

    Text {
      text: ":"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.verticalCenter: parent.verticalCenter
    }

    CompactCount {
      statusBucket: "working"
      displayValue: String(root.countFor("working"))
    }

    Text {
      text: "|"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.verticalCenter: parent.verticalCenter
    }

    CompactCount {
      statusBucket: "response"
      displayValue: String(root.countFor("response"))
    }

    Text {
      text: "|"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.verticalCenter: parent.verticalCenter
    }

    CompactCount {
      statusBucket: "permission"
      displayValue: String(root.countFor("permission"))
    }

    Text {
      text: "|"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.verticalCenter: parent.verticalCenter
    }

    CompactCount {
      statusBucket: "idle"
      displayValue: String(root.countFor("idle"))
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
            text: root.activeFilter === ""
              ? "OpenCode sessions"
              : "OpenCode · " + root.bucketLabel(root.activeFilter)
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
              readonly property bool selected: index === root.selectedSessionIndex
              width: panelColumn.width
              height: expanded ? Style.space(120) : Style.space(60)

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

                  Text {
                    visible: expanded
                    width: parent.width
                    text: root.contextUsageLabel(modelData)
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
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
            text: root.activeFilter === "" ? "No OpenCode sessions tracked" : "No sessions in this category"
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

          Button {
            width: parent.width
            text: root.settingsOpen ? "Hide settings" : "Settings"
            selected: root.settingsOpen
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.settingsOpen = !root.settingsOpen
          }

          Column {
            visible: root.settingsOpen
            width: parent.width
            spacing: Style.space(6)

            Toggle {
              width: parent.width
              label: "Colored counter numbers"
              description: root.coloredCounts
                ? "Use a different color for each status."
                : "Use white numbers for every status."
              checked: root.coloredCounts
              fontFamily: root.fontFamily
              titleSize: Style.font.bodySmall
              descriptionSize: Style.font.caption
              foreground: root.foreground
              onClicked: root.setColoredCounts(!root.coloredCounts)
            }

            Toggle {
              width: parent.width
              label: "Session notifications"
              description: root.notificationsEnabled
                ? "Notify when a session needs attention or finishes."
                : "Session notifications are disabled."
              checked: root.notificationsEnabled
              fontFamily: root.fontFamily
              titleSize: Style.font.bodySmall
              descriptionSize: Style.font.caption
              foreground: root.foreground
              onClicked: root.setNotificationsEnabled(!root.notificationsEnabled)
            }

            NumberField {
              width: parent.width
              label: "Notification timeout (seconds)"
              value: root.notificationTimeoutSeconds
              from: 8
              to: 30
              stepSize: 1
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onModified: root.setNotificationTimeout(value)
            }

            Text {
              width: parent.width
              text: "Choose between 8 and 30 seconds."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }
        }
      }
    }
  }
}
