pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// GTD: the panel, and the owner of all state.
//
// Loaded by BarWidget.qml (the manifest entry point), which injects bar,
// settings, anchorItem and hostWidget and forwards open/close/toggle to us.
// IPC is left to the bar widget so the target is registered once.
//
// Every row on screen is one `gtd <verb> --json` invocation, so the panel is
// another face of the same engine the terminal uses: capture, clarify,
// organize, reflect, engage. The database is the only place state lives.
Panel {
  id: root
  moduleName: "com.auino.omagtd"
  manageIpc: false

  // Injected by BarWidget.qml. The bar tracks the widget mounted in its slot,
  // not this nested panel, so everything the bar identifies a panel by has to
  // be that widget.
  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  // ---- Counts, mirrored from `gtd bar --json`.
  property int next: 0
  property int today: 0
  property int dueToday: 0
  property int dueTodayLate: 0
  property int overdue: 0
  property int upcoming: 0
  property string dueTime: "18:00"
  property string timeFormat: "24h"
  property int waiting: 0
  property int flagged: 0
  property int flaggedToday: 0
  property int projectsOpen: 0
  property string nextTitle: ""
  property bool stale: false

  // ---- The one list this panel shows: what is due today.
  // The other views have a home in the fullscreen window, which has the room
  // for them; the panel is the thing you glance at, so it keeps one list.
  readonly property var section: ({ key: "today", label: "Today",
                                    args: ["list", "due_today", "--limit", "80"] })

  property var rows: []        // display rows, precomputed from the JSON
  property var current: null   // the row under the cursor
  property int selected: 0
  // True while the capture field should own the keyboard. The panel opens with
  // the list cursor instead, so arrows move the selection; "c" or clicking the
  // capture box brings the caret into the field, and Escape drops back out.
  property bool captureFocus: false
  property string status: ""   // the last thing that happened, for the footer

  readonly property string pluginDir: Qt.resolvedUrl(".").toString().replace("file://", "")
  readonly property string cli: root.pluginDir + "bin/gtd"
  readonly property string fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
  readonly property color textColor: Color.popups.text
  readonly property color dim: Qt.darker(root.textColor, 1.5)
  readonly property color urgent: root.bar ? root.bar.urgent : Color.urgent

  // Status colours and the mark in front of a title, from the active theme.
  GtdTheme { id: gtdTheme }

  readonly property int pollSeconds: {
    var n = root.settings && typeof root.settings.pollIntervalSec === "number"
        ? root.settings.pollIntervalSec : 60
    return Math.max(10, Math.min(900, Math.round(n)))
  }

  Component.onDestruction: {
    barProc.running = false
    listProc.running = false
    actionProc.running = false
  }

  // ---- Presentation helpers shared with the pill.

  function shortWhen(stamp) {
    if (!stamp) return ""
    var text = String(stamp)
    if (text.length < 16) return text
    return text.slice(0, 10) + " " + root.formatClock(text.slice(11, 16))
  }

  // "HH:MM" in the store, "6:00 PM" when the setting asks for a 12h clock.
  function formatClock(hhmm) {
    if (root.timeFormat !== "12h") return hhmm
    var parts = String(hhmm).split(":")
    if (parts.length !== 2) return hhmm
    var hours = parseInt(parts[0], 10)
    if (isNaN(hours)) return hhmm
    var suffix = hours < 12 ? "AM" : "PM"
    hours = hours % 12
    if (hours === 0) hours = 12
    return hours + ":" + parts[1] + " " + suffix
  }

  // "YYYY-MM-DD HH:MM", the same shape a due date is stored in, so a
  // lexicographic compare is enough to call a task late. Read on demand, for
  // the same reason the fullscreen reads it: a property would hold the time
  // this panel was created at, and nothing would ever go late afterwards.
  function nowStamp() {
    var d = new Date()
    function two(n) { return (n < 10 ? "0" : "") + n }
    return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate())
      + " " + two(d.getHours()) + ":" + two(d.getMinutes())
  }

  function isLate(item) {
    if (!item || !item.due_at) return false
    var status = item.status || ""
    if (status === "done" || status === "dropped" || status === "trashed") return false
    return String(item.due_at).slice(0, 16) < root.nowStamp()
  }

  function humanMinutes(mins) {
    var n = parseInt(mins)
    if (!n || n <= 0) return ""
    var h = Math.floor(n / 60)
    var m = n % 60
    if (h && m) return h + "h" + (m < 10 ? "0" : "") + m + "m"
    if (h) return h + "h"
    return m + "m"
  }

  function itemRow(row) {
    var meta = []
    if (row.kind === "action" && row.parent_title) meta.push(row.parent_title)
    var tagList = row.tags || []
    if (tagList.length) meta.push("#" + tagList.join(" #"))
    if (row.waiting_for) meta.push("waiting " + row.waiting_for)
    var due = row.due_at || row.defer_at
    if (due) meta.push(shortWhen(due))
    // A finished task only turns up in search, so when it does the one thing
    // worth saying is when it was finished.
    var archived = row.status === "done"
    if (archived && row.completed_at) meta.push("completed " + shortWhen(row.completed_at))
    return { id: row.id, title: row.title,
             meta: meta.join("  "),
             glyph: gtdTheme.rowGlyph(row),
             late: root.isLate(row), archived: archived, raw: row, kind: "item" }
  }

  // Waiting is filed by project, so the bucketing matches the fullscreen
  // view: named projects first, alphabetical, no project last.
  function waitingGroups(rows) {
    var buckets = []
    var seen = ({})
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i]
      var name = row.parent_title || ""
      var bucketKey = name.toLowerCase()
      if (!seen[bucketKey]) {
        seen[bucketKey] = { name: name, rows: [] }
        buckets.push(seen[bucketKey])
      }
      seen[bucketKey].rows.push(row)
    }
    buckets.sort(function(a, b) {
      if (a.name === b.name) return 0
      if (a.name === "") return 1
      if (b.name === "") return -1
      return a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1
    })
    return buckets
  }

  function buildRows(key, data) {
    var out = []
    if (!data) return out

    if (key === "calendar") {
      var days = data.days || []
      for (var i = 0; i < days.length; i++) {
        var cell = days[i]
        if (!cell.count) continue
        out.push({ header: true, title: cell.date + "  " + cell.weekday, meta: "" })
        var groups = [["overdue", "!"], ["due", "due"], ["scheduled", "defer"], ["completed", "done"]]
        for (var g = 0; g < groups.length; g++) {
          var list = cell[groups[g][0]] || []
          for (var j = 0; j < list.length; j++) {
            var it = list[j]
            out.push({ id: it.id, title: it.title,
                       meta: groups[g][1] + (it.due_at ? " " + shortWhen(it.due_at) : ""),
                       glyph: gtdTheme.rowGlyph(it), raw: it,
                       late: groups[g][0] === "overdue" || root.isLate(it),
                       kind: groups[g][0] === "completed" ? "done" : "open" })
          }
        }
      }
      return out
    }

    if (key === "log") {
      var items = data.items || []
      for (var k = 0; k < items.length; k++) {
        var e = items[k]
        out.push({ header: false, id: e.item_id, title: (e.detail || "").trim() || e.title || "",
                   meta: e.kind + "  " + shortWhen(e.at), glyph: "·", raw: e, kind: "log" })
      }
      return out
    }

    if (key === "contexts") {
      var tags = data.items || data.tags || []
      for (var p = 0; p < tags.length; p++) {
        var tag = tags[p]
        out.push({ id: tag.id, title: "#" + tag.name, meta: tag.open_count + " open",
                   glyph: " ", raw: tag, kind: "tag" })
      }
      return out
    }

    if (key === "waiting") {
      var groups = root.waitingGroups(data.items || [])
      for (var b = 0; b < groups.length; b++) {
        var bucket = groups[b]
        out.push({ header: true, title: bucket.name || "No project",
                   meta: bucket.rows.length + "", raw: null })
        for (var q = 0; q < bucket.rows.length; q++)
          out.push(root.itemRow(bucket.rows[q]))
      }
      return out
    }

    var rows = data.items || []
    for (var r = 0; r < rows.length; r++) out.push(root.itemRow(rows[r]))
    return out
  }

  function setRows(key, data) {
    root.rows = root.buildRows(key, data)
    if (root.selected >= root.rows.length) root.selected = Math.max(0, root.rows.length - 1)
    // Headings carry no row to act on; if the cursor sits on one, step off it.
    while (root.selected < root.rows.length && root.rows[root.selected].header)
      root.selected++
    root.updateCurrent()
  }

  function updateCurrent() {
    var row = root.rows[root.selected]
    root.current = row && !row.header ? row : null
  }

  // ---- Processes.
  //
  // Quickshell only launches a Process when `running` is set, so every command
  // is followed by an explicit `running = true`. Setting `running = false`
  // first is what makes a repeat run replace the previous command instead of
  // being ignored, which is the pattern the Omarchy shell uses for its own
  // imperative runs.

  function exec(proc, args) {
    proc.command = args
    proc.running = false
    proc.running = true
  }

  Process {
    id: barProc
    stdout: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out === "") { root.stale = true; return }
        try {
          var d = JSON.parse(out)
          root.stale = false
          root.next = d.next || 0
          root.today = d.today || 0
          root.dueToday = d.due_today || 0
          root.dueTodayLate = d.due_today_late || 0
          root.overdue = d.overdue || 0
          root.upcoming = d.upcoming || 0
          root.waiting = d.waiting || 0
          root.flaggedToday = d.flagged_today || 0
        } catch (e) {
          root.stale = true
        }
      }
    }
  }

  Process {
    id: listProc
    onStarted: root.listStarts = root.listStarts + 1
    onExited: { root.listExits = root.listExits + 1; root.listPending = false }
    stdout: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out === "") return
        try {
          var d = JSON.parse(out)
          root.setRows(root.section.key, d)
        } catch (e) {
          root.status = "could not read the list"
        }
      }
    }
  }

  Process {
    id: actionProc
    onExited: { root.actionPending = false; root.reload() }
    stdout: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out === "") return
        try {
          var d = JSON.parse(out)
          if (d.ok === false && d.error) {
            root.status = String(d.error)
            // `o` with nothing behind it says so in the shell, where the eye
            // already is, rather than only in the status line below.
            if (d.reason === "no-link")
              root.showOsd("", "No link available for the task")
          } else if (d.ok && root.lastVerb === "open") {
            root.showOsd("↗", "Opening link…")
          }
        } catch (e) { }
      }
    }
  }

  // Capture returns the new item; the panel cannot edit it in place, so it
  // hands the id to the bar widget, which opens the full view on that editor.
  Process {
    id: captureProc
    onExited: root.reload()
    stdout: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out === "") return
        try {
          var d = JSON.parse(out)
          if (d.ok === false && d.error) { root.status = String(d.error); return }
          if (d.item && d.item.id && root.hostWidget
              && typeof root.hostWidget.openItem === "function")
            root.hostWidget.openItem(d.item.id)
        } catch (e) {
          // A capture that printed nothing still succeeded if it exited 0.
        }
      }
    }
  }

  // ---- Capture ------------------------------------------------------------
  property string captureValue: ""

  property var listPending: false
  property bool actionPending: false
  // Which verb the in-flight `runAction()` is, so a reply can be recognised
  // as an open (worth a toast) without the reply naming itself again.
  property string lastVerb: ""
  property int listStarts: 0
  property int listExits: 0
  readonly property bool busy: listPending || actionPending

  // Poll the counts always, the list only while the panel is on screen.
  function reload() {
    exec(barProc, [root.cli, "bar", "--json"])
    if (root.opened) root.reloadList()
  }

  function reloadList() {
    if (!root.opened) return
    if (root.listPending || listProc.running) return
    root.listPending = true
    root.status = ""
    exec(listProc, [root.cli].concat(root.section.args).concat(["--json"]))
  }

  // The name the bar widget and the IPC handler call it by.
  function refresh() { root.reload() }

  function runAction(args) {
    if (root.actionPending || actionProc.running) return
    root.actionPending = true
    root.lastVerb = args[0]
    exec(actionProc, [root.cli].concat(args).concat(["--json"]))
    if (args[0] === "capture") root.status = "captured"
    else if (args[0] === "flag") root.status = "flagged"
    else if (args[0] === "postpone") root.status = "postponed"
    else if (args[0] === "open") root.status = "opened"
    else if (args[0] === "undo") root.status = "undone"
    else if (args[0] === "engage" && args[2] === "done") root.status = "done"
    else if (args[0] === "engage" && args[2] === "drop") root.status = "dropped"
    else root.status = "done"
  }

  // A toast in the shell's OSD — the same card the launch message uses. The
  // shell may not be running, so it goes through the quiet IPC wrapper, which
  // fails silently instead of breaking the panel.
  function showOsd(icon, message) {
    try {
      Quickshell.execDetached(
        ["omarchy-shell", "-q", "osd", "show",
         JSON.stringify({ icon: icon, message: message, duration: 2000 })])
    } catch (e) { }
  }

  function capture(text) {
    var t = String(text || "").trim()
    if (t === "") return
    root.runAction(["capture", t])
    root.captureValue = ""
  }

  // Capture+edit files the capture and opens its editor in the full view.
  // With an empty box it leaves the panel in place; task details live in the
  // full-screen view, not in this quick panel.
  function captureAndEdit(text) {
    var t = String(text || "").trim()
    if (t === "") {
      root.status = "type a capture to capture+edit"
      return
    }
    root.status = "captured"
    exec(captureProc, [root.cli, "capture", t, "--json"])
    root.captureValue = ""
  }

  // What to say when a key needs a row and there is none.
  function emptyHint() {
    if (root.rows.length === 0) return "nothing in " + root.section.label.toLowerCase()
    return "nothing selected"
  }

  function step(delta) {
    if (root.rows.length === 0) { root.status = root.emptyHint(); return }
    var i = root.selected + delta
    while (i >= 0 && i < root.rows.length && root.rows[i].header) i += delta
    if (i < 0 || i >= root.rows.length) return
    root.selected = i
    root.updateCurrent()
    listView.positionViewAtIndex(i, ListView.Contain)
  }

  function choose(i) {
    if (i < 0 || i >= root.rows.length) return
    root.selected = i
    root.updateCurrent()
  }

  function activate() {
    // Enter is the capture+edit command; it never opens task details.
    root.captureAndEdit(root.captureValue)
  }

  // What the panel can do to the selected task: finish it, drop it, flag it.
  // Everything else — dates, projects, notes — is the fullscreen editor's.
  function act(name) {
    if (!root.current || !root.current.id) return
    var id = root.current.id
    if (name === "done") root.runAction(["engage", id, "done"])
    else if (name === "drop") root.runAction(["engage", id, "drop"])
    else if (name === "flag") root.runAction(["flag", id])
    else if (name === "open") root.runAction(["open", id])
    else if (name === "undo") root.runAction(["undo", id])
  }

  function toggleFlag() { root.act("flag") }

  function focusCapture() {
    root.captureFocus = true
    captureField.forceActiveFocus()
  }

  // Escape unwinds one step rather than slamming the panel shut: the capture
  // field hands the caret back to the list first, then the panel closes.
  function escapePanel() {
    if (captureField.activeFocus) {
      root.captureFocus = false
      keyCatcher.forceActiveFocus()
      return
    }
    root.close()
  }

  // Hand off to the bar widget, which owns the full view surface.
  function openFullscreen() {
    if (root.hostWidget && typeof root.hostWidget.openFullscreen === "function")
      root.hostWidget.openFullscreen()
  }

  Timer {
    interval: root.pollSeconds * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.reload()
  }

  // Poll faster, and reload the list, while the panel is open. Opening leaves
  // the keyboard with the list cursor, so arrows move the selection; "c" or a
  // click in the capture box puts the caret in the field to write a task.
  onOpenedChanged: if (root.opened) {
    gtdTheme.refresh()
    root.selected = 0
    root.captureFocus = false
    root.status = ""
    root.captureValue = ""
    root.reload()
  }

  // ---- UI.

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: root.captureFocus ? captureField : keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(440))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight, Style.space(940))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // Suspend the panel cursor model while the capture field owns the
      // keyboard, so typing "d" writes a d instead of marking something done.
      blocked: captureField.activeFocus
      onCloseRequested: root.escapePanel()
      onMoveRequested: function(dx, dy) { if (dy !== 0) root.step(dy) }
      onActivateRequested: root.activate()
      onDeleteRequested: root.act("drop")
      onTextKey: function(t) {
        if (t === "c" || t === "C") root.focusCapture()
        else if (t === "d" || t === "D") root.act("done")
        else if (t === "f" || t === "F") root.toggleFlag()
        else if (t === "o" || t === "O") root.act("open")
        else if (t === "z" || t === "Z") root.act("undo")
        else if (t === "x" || t === "X") root.act("drop")
        else if (t === "r" || t === "R") root.reload()
        else if (t === "m" || t === "M") root.openFullscreen()
        else if (t === "p" || t === "P") {
          if (!root.current || !root.current.id) { root.status = root.emptyHint(); return }
          root.runAction(["postpone", root.current.id])
        }
      }

      Flickable {
        id: outerFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: panelColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: panelColumn
          width: outerFlick.width
          spacing: Style.spacing.sm

          PanelHero {
            id: heroBox
            width: parent.width
            title: "OmaGTD"
            // The counts are a Row of their own below rather than the hero's
            // `meta` string, because the overdue segment has to be able to turn
            // urgent on its own — `meta` is a single Text and one colour for the
            // whole line.
            meta: ""
            foreground: root.textColor
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: root.overdue > 0 ? "!" : "✓"
                color: root.overdue > 0 ? root.urgent : root.textColor
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          // Overdue is the one count worth interrupting for: it is the only one
          // that means something already went wrong, so it turns red the moment
          // it is above zero. The rest stay dim whatever they read.
          Row {
            width: parent.width
            spacing: Style.spacing.sm
            visible: heroBox.visible

            Text {
              textFormat: Text.PlainText
              color: root.overdue > 0 ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              text: (root.overdue + " overdue").toUpperCase()
            }
            Text {
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              text: "·"
            }
            Text {
              textFormat: Text.PlainText
              // Red when anything has expired (today's time passed, or work
              // left over from an earlier day); orange when a task on today's
              // list is flagged and nothing has expired; dim otherwise. Every
              // flagged action sits in the `today` view, so the global flagged
              // count is exactly "at least one flagged task on today".
              color: (root.overdue > 0 || root.dueTodayLate > 0) ? root.urgent
                   : (root.flaggedToday > 0 ? gtdTheme.orange : root.dim)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              text: (root.dueToday + " today").toUpperCase()
            }
            Text {
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              text: "·"
            }
            Text {
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              text: (root.upcoming + " upcoming").toUpperCase()
            }
          }

          PanelSeparator {
            anchors.left: parent.left
            anchors.right: parent.right
            foreground: root.textColor
          }

          Row {
            width: parent.width
            spacing: Style.spacing.xs

            TextField {
              id: captureField
              width: parent.width - editButton.width - parent.spacing
              foreground: root.textColor
              placeholderText: "Capture…  #context /Project/ <defer >due =who ~repeat *30m"
              text: root.captureValue
              onTextEdited: root.captureValue = text
              onAccepted: root.capture(text)
              Keys.onDownPressed: function(event) {
                root.captureAndEdit(root.captureValue)
                event.accepted = true
              }
              Keys.onEscapePressed: function(event) {
                // Escape only takes the caret back out of the box, never out of
                // the panel; what has been typed stays, so coming back to finish
                // the thought is not a retype.
                root.escapePanel()
                event.accepted = true
              }
            }

            Button {
              id: editButton
              text: "↓"
              bordered: true
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.textColor
              onClicked: root.captureAndEdit(root.captureValue)
            }
          }

          PanelSeparator {
            anchors.left: parent.left
            anchors.right: parent.right
            foreground: root.textColor
          }

          // With the section buttons gone this label is the only thing that
          // names the list.
          Text {
            width: parent.width
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            text: root.section.label + "   " + root.rows.length
          }

          // An empty list used to look like a dead panel: the header said
          // "Today 0" and every key silently did nothing. Say why, and say
          // what to press, so an empty section reads as an answer.
          Text {
            width: parent.width
            visible: root.rows.length === 0
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            text: "nothing in " + root.section.label.toLowerCase()
          }

          ListView {
            id: listView
            width: parent.width
            height: Math.min(contentHeight, Style.space(430))
            model: root.rows
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            spacing: 1

            delegate: Item {
              id: rowItem
              required property var modelData
              required property int index
              width: listView.width
              height: modelData.header ? headerText.implicitHeight + Style.space(10)
                                       : rowText.implicitHeight + Style.space(8)

              Rectangle {
                anchors.fill: parent
                radius: Style.cornerRadius
                color: (!rowItem.modelData.header && rowItem.index === root.selected)
                    ? Style.selectedFillFor(root.textColor, Color.accent) : "transparent"
              }

              Text {
                id: headerText
                visible: rowItem.modelData.header === true
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                text: rowItem.modelData.title || ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }

              Text {
                id: rowText
                visible: rowItem.modelData.header !== true
                width: parent.width
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                elide: Text.ElideRight
                // Panel rows stay compact: the mark and title only.
                text: (rowItem.modelData.glyph || " ") + "  "
                  + (rowItem.modelData.title || "")
                color: gtdTheme.colorFor(rowItem.modelData.raw, rowItem.modelData.late,
                  rowItem.index === root.selected
                    ? root.textColor
                    : (rowItem.modelData.kind === "done" ? root.dim : root.textColor))
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.strikeout: rowItem.modelData.archived === true
              }

              MouseArea {
                anchors.fill: parent
                enabled: rowItem.modelData.header !== true
                onClicked: root.choose(rowItem.index)
              }
            }
          }

          PanelSeparator {
            anchors.left: parent.left
            anchors.right: parent.right
            foreground: root.textColor
          }

          Text {
            id: footerText
            width: parent.width
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            text: {
              if (root.status !== "") return root.status
              return "↑↓ move  c capture  ↓ capture+edit  f flag  d done  x drop  p postpone  o open  z undo  m full screen  r reload  esc close"
            }
          }
        }
      }
    }
  }

}
