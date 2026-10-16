pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as ControlsBasic
import QtQuick.Dialogs
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// GTD: the full view — the whole system, laid out to be read.
//
// Three columns, left to right, because that is the order of the questions
// GTD asks:
//
//   left    what is the work, and where does it live? Areas, the projects
//           inside them, each project's actions, and whatever has no project
//           yet. This is the shape of everything you have committed to.
//   middle  the list for whichever step you are standing in: inbox, clarify,
//           focus, today, forecast, overdue, flagged, waiting, someday,
//           projects, contexts, calendar, review, log, trash.
//   right   everything about the selected thing, editable in place, plus the
//           settings that move data in and out: import from OmniFocus, export
//           to OmniFocus, and one zip file that holds the lot.
//
// The popover panel stays the fast surface — this one is for sitting down.
// Every row is still one `gtd <verb> --json` call, so nothing here can
// disagree with the terminal.
FocusScope {
  id: root

  // Injected by the plugin surfaces when available.
  property var bar: null
  property var settings: null
  property var hostWidget: null
  property var sibling: null

  property string pluginDir: Qt.resolvedUrl(".").toString().replace("file://", "") + "/"
  readonly property string cli: root.pluginDir + "bin/gtd"
  readonly property string fontFamily: root.bar ? root.bar.fontFamily : gtdTheme.fontFamily
  // Every surface colour comes through the theme object, so choosing a palette
  // repaints the whole view in one place. "omarchy" passes the desktop theme
  // straight through.
  readonly property color textColor: gtdTheme.text
  readonly property color surfaceBackground: gtdTheme.background
  readonly property color dim: gtdTheme.dim
  readonly property color line: gtdTheme.line
  readonly property color urgent: gtdTheme.urgent
  readonly property color accent: gtdTheme.accent
  readonly property string home: Quickshell.env("HOME")

  // The app version, shown as info in Settings. Read from the plugin manifest
  // so there is one source of truth; a missing or unreadable file leaves this
  // empty and the About line falls back to "dev".
  property string appVersion: ""
  function parseVersion(raw) {
    try {
      var d = JSON.parse(String(raw || ""))
      return d && d.version ? String(d.version) : ""
    } catch (e) { return "" }
  }
  FileView {
    id: manifestFile
    path: root.pluginDir + "../manifest.json"
    watchChanges: true
    printErrors: false
    onLoaded: root.appVersion = root.parseVersion(text())
    onFileChanged: root.appVersion = root.parseVersion(text())
  }

  // ---- Lifetime. Mirrors Panel.qml's contract so the bar widget can drive
  // both surfaces the same way.
  property bool opened: false
  property bool manageIpc: false
  property bool popoutSwitchClosing: false
  // Set by the fullscreen overlay so Escape requests a shell-managed close.
  property bool fullscreenMode: false
  signal closeRequested()

  // Closing the fullscreen view asks first. Escape raises this; while it is up
  // Escape is inert (the user just pressed it), Space takes the highlighted
  // button, and arrows or Tab move between Confirm (the default) and Cancel.
  property bool confirmCloseOpen: false
  property int confirmCloseIndex: 1

  function open() {
    if (root.opened) return
    root.opened = true
    if (root.sibling && root.sibling.opened) root.sibling.close()
    // Every opening lands on Today. Creating or opening a task is the one
    // carve-out: BarWidget calls showItem() right after this, which hands the
    // pane over to the editor regardless of which view was picked here.
    var today = root.viewIndexFor("today")
    if (root.viewIndex !== today) root.viewIndex = today
    root.reload()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function close() {
    // Releasing the layer-shell surface and dropping keyboardFocus is enough;
    // BarWidget has no focus() to hand the keyboard back to.
    root.opened = false
  }

  function toggle() { root.opened ? root.close() : root.open() }
  function closeForPopoutSwitch() { root.close() }

  function handleCloseRequested() {
    // The fullscreen overlay delegates close to its host, so ask there; the
    // quick panel simply closes in place.
    if (root.fullscreenMode) root.confirmCloseRequest()
    else root.close()
  }

  // Raise the close confirmation with Confirm highlighted, and hand the
  // keyboard to it so Escape can no longer reach the close handler.
  function confirmCloseRequest() {
    if (root.confirmCloseOpen) return
    root.confirmCloseIndex = 1
    root.confirmCloseOpen = true
    Qt.callLater(function() { confirmCloseFocus.forceActiveFocus() })
  }

  // Take the highlighted button: Confirm (1) closes the app, Cancel (0) puts
  // the keyboard back where it was.
  function confirmCloseActivate() {
    var confirmed = root.confirmCloseIndex === 1
    root.confirmCloseOpen = false
    if (!confirmed) { keyCatcher.forceActiveFocus(); return }
    if (root.fullscreenMode) root.closeRequested()
    else root.close()
  }

  function closeSettingsPane() {
    root.settingsOpen = false
    root.column = 1
    root.reloadList()
  }

  // ---- Status colours and the mark in front of a title, from the active
  // theme. Colour is owned by the host window; this only tracks the file.
  GtdTheme { id: gtdTheme }

  onOpenedChanged: {
    if (!root.opened) return
    gtdTheme.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  // ---- What is on screen ---------------------------------------------------

  property var treeData: null
  property var treeRows: []
  property int treeSelected: 0

  // When set, the middle column shows this container's tasks instead of the
  // current nav view. Selecting a project (or area) in the left tree sets it;
  // picking a nav view clears it.
  property var projectFocus: null
  readonly property bool projectFocused: root.projectFocus !== null

  // The left column holds two trees and this says which one is on screen: the
  // project hierarchy or the context hierarchy. Whichever is showing is the
  // one the left column's cursor walks, and whichever row it points at pours
  // that container's tasks into the middle column.
  property string leftMode: "projects"

  // Contexts, same shape as the project tree: a flat list ordered by name,
  // indented by how deep the name sits in a "parent>child" path.
  property var contextData: null
  property var contextRows: []
  property int contextSelected: 0
  property var contextFocus: null
  readonly property bool contextFocused: root.contextFocus !== null

  property var views: [
    { key: "inbox",     label: "Inbox",     args: ["list", "inbox", "--limit", "200"] },
    { key: "clarify",   label: "Clarify",   args: ["list", "clarify", "--limit", "200"] },
    { key: "focus",     label: "Focus",     args: ["list", "focus", "--limit", "200"] },
    { key: "today",     label: "Today",     args: ["list", "today_open"] },
    { key: "forecast",  label: "Upcoming",  args: ["list", "upcoming"] },
    { key: "overdue",   label: "Overdue",   args: ["list", "overdue"] },
    { key: "flagged",   label: "Flagged",   args: ["list", "flagged", "--limit", "200"] },
    { key: "waiting",   label: "Waiting",   args: ["list", "waiting", "--limit", "200"] },
    { key: "someday",   label: "Someday",   args: ["list", "someday", "--limit", "200"] },
       { key: "projects",  label: "Projects",  args: ["list", "projects", "--limit", "200"] },
       { key: "contexts",  label: "Contexts",  args: ["list", "tags"] },
       { key: "calendar",  label: "Calendar",  args: ["calendar"] },
      { key: "review",    label: "Review",    args: ["review", "weekly"] },
      { key: "log",       label: "Log",       args: ["list", "log", "--limit", "200"] },
       { key: "archived",  label: "Archive",   args: ["list", "archived", "--limit", "200"] },
       { key: "trash",     label: "Trash",     args: ["trash", "--list"] },
       { key: "hideZeroCounts", label: "Top Bar: Hide 0/0", args: ["setting", "--json"] },
       { key: "search",    label: "Search",    args: ["search", "--limit", "200"] }
  ]
  property int viewIndex: 0
  readonly property var view: root.views[Math.max(0, Math.min(root.viewIndex, root.views.length - 1))]

  // The buttons the left column shows, laid out as three rows. Tab walks these
  // and only these, so the keyboard matches what is on screen. The rest of
  // `views` stays reachable by name (`view <key>`) and by the number keys.
readonly property var primaryViews: [
                 ["inbox"],
                  ["calendar", "waiting", "review"],
                  ["overdue", "today", "forecast"]
                ]
  readonly property var primaryKeys: {
    var keys = []
    for (var r = 0; r < root.primaryViews.length; r++)
      for (var c = 0; c < root.primaryViews[r].length; c++)
        keys.push(root.primaryViews[r][c])
    return keys
  }

  // The calendar page keeps its own month and a grid built from `gtd calendar`.
  property string calendarMonth: {
    var d = new Date()
    return d.getFullYear() + "-" + (d.getMonth() < 9 ? "0" : "") + (d.getMonth() + 1)
  }
  property var calendarData: null
  property var calendarWeeks: []
  property string calendarSelectedDate: ""
  property string pickerMonth: {
    var d = new Date()
    return d.getFullYear() + "-" + (d.getMonth() < 9 ? "0" : "") + (d.getMonth() + 1)
  }
  property var pickerWeeks: buildPickerWeeks(pickerMonth)
  property string pickerTarget: "due"
  // The clock a picked day lands on. Seeded from the settings default each
  // time the popup opens; an empty box keeps the day bare so the backend
  // applies the same setting.
  property string pickerTime: ""
  readonly property string pickerLabel: {
    var names = ["January", "February", "March", "April", "May", "June",
                 "July", "August", "September", "October", "November", "December"]
    var parts = String(root.pickerMonth).split("-")
    var m = parseInt(parts[1]) - 1
    return (names[m] || parts[1]) + " " + parts[0]
  }
  readonly property string monthLabel: {
    var names = ["January", "February", "March", "April", "May", "June",
                 "July", "August", "September", "October", "November", "December"]
    var parts = root.calendarMonth.split("-")
    var m = parseInt(parts[1]) - 1
    return (names[m] || parts[1]) + " " + parts[0]
  }

  property var rows: []
  property int rowSelected: 0
  // A hand-off from the panel's Enter asks the middle cursor to come to rest on
  // one task. The list is fetched asynchronously, so the id is parked here and
  // the first page that carries the row moves the cursor onto it.
  property string pendingSelectId: ""
  // The query behind the middle column, without `--json` or an offset, so a
  // later page is the same request with a bigger offset. `listOffset` is how
  // many items the backend has handed over so far; `listMore` says the last
  // page came back full, so another may exist.
  property var listArgs: []
  property int listOffset: 0
  property int listRequestOffset: 0
  property bool listMore: false

  // Which column the keyboard cursor is in: 0 left, 1 middle, 2 right.
  property int column: 1
  // "settings" shows the settings pane in the right column instead of an item.
  property bool settingsOpen: false

  // ---- Where the keyboard is -----------------------------------------------
  //
  // The cursor rests either on the header row (topFocused), on one of its
  // controls (topIndex: 0 settings, 1 capture, 2 search, 3 the down button), or
  // in one of the columns. Inside the left column it walks three stacked parts
  // in turn (leftSection): the nav menu, the projects/contexts switcher, and the
  // tree. menuSelected is the nav-menu button the cursor highlights while it is
  // in the menu, so Tab has something to walk and Return something to pick.
  property bool topFocused: false
  property int topIndex: 1
  property string leftSection: "tree"
  property int menuSelected: 0

    // Mirrored from `gtd settings --json`: the clock a bare due date lands on,
    // the clock a bare defer date lands on, how every stored clock time is
    // shown, and how long the empty projects and contexts, and the finished
    // tasks, are kept before they are swept. A day count of 0 means keep it
    // forever.
    property string dueTime: "18:00"
    property string deferTime: "09:00"
    property string timeFormat: "24h"
   // The app's own palette: "omarchy", "of-light" or "of-dark".
   property string appTheme: "omarchy"
   property int emptyProjectsKeepDays: 7
   property int emptyContextsKeepDays: 7
    property int doneRetentionDays: 30
    property int archiveRetentionDays: 0
    // Whether the calendar shows a green mark (and the selected-day list the
    // rows) for completed tasks. Mirrored from the same knob in the database.
    property bool showCompleted: false
    // The narrowest the window gets before the layout drops to a message
    // asking to enlarge it, mirrored from the same setting in the database.
    property int fullscreenWidthLimit: 768

    // Bundled app icon: the plugin's app/icon.png, shown in the hero.
    property url appIcon: Qt.resolvedUrl("icon.png")

  // Mirrored from the bar widget's own settings, which live in shell.json and
  // only change through `omarchy bar set`. The shell pushes them back in here
  // whenever it writes them, so this follows the pill on its own.
  readonly property bool hideZeroCounts: !!(settings && settings.hideZeroCounts === true)

  property var detail: null
  property string detailName: ""
  // What the right column is currently waiting for. A detail load that comes
  // back after the selection moved on is stale and must not overwrite the in
  // flight request, so the result is matched against these before it is shown.
  property string detailWantedId: ""
  property string detailWantedKind: ""
  property string status: ""
  property bool stale: false
  // Which verb the in-flight `run()` is, so a reply can be recognised as an
  // open (worth a toast) without the reply having to name itself again.
  property string lastVerb: ""

  // Form fields for the right-hand editor, seeded from the selection.
  property string editTitle: ""
  property string editProject: ""
  property string editTags: ""
  // Waiting is a boolean, like the flag: on/off, not a name to type. Read off
  // the loaded item rather than seeded, so the switch shows the truth even
  // while the form is holding unsaved edits — flipping it is not an edit, it
  // is the same immediate act the flag is.
  readonly property bool editWait: {
    var item = root.detail && (root.detail.item || root.detail)
    return !!(item && item.status === "waiting")
  }
  property string editDue: ""
  property string editDefer: ""
  property string editDuration: ""
  property string editRepeat: ""
  property string editNotes: ""
  property string editLink: ""
  property string editAttachments: ""
  property bool editHideZeroCounts: false
  // The editor is a live form: a background refresh (the poll, or another
  // command's reload) must never overwrite what the user has typed but not yet
  // saved. formSeededId remembers which item the form currently holds, so a
  // seed of that same item is dropped while the form is dirty.
  property bool formDirty: false
  property string formSeededId: ""

  property string captureValue: ""
  property string searchQuery: ""

  property string importPath: ""
  property bool importDryRun: true
  // The context stamped on every imported task, so a batch can be found and
  // reviewed together. Dated by default; editable, and clearing it adds none.
  property string importTag: "omnifocus_import_" + root.stamp.split("-")[0]
  property string exportOfPath: ""
  property string exportZipPath: ""
  property string stamp: {
    var d = new Date()
    function two(n) { return (n < 10 ? "0" : "") + n }
    return d.getFullYear() + two(d.getMonth() + 1) + two(d.getDate())
      + "-" + two(d.getHours()) + two(d.getMinutes())
  }
  property string dbInfo: ""
  property string dbPath: ""
  readonly property string today: {
    var d = new Date()
    return d.getFullYear() + "-" + (d.getMonth() < 9 ? "0" : "") + (d.getMonth() + 1)
      + "-" + (d.getDate() < 10 ? "0" : "") + d.getDate()
  }
  // "YYYY-MM-DD HH:MM", the same shape the database stores a due date in, so
  // a lexicographic compare is enough to call a task late. Read on demand: as
  // a property it would be read once, when this window was created, and every
  // task that went late after that would keep looking on time.
  function nowStamp() {
    var d = new Date()
    function two(n) { return (n < 10 ? "0" : "") + n }
    return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate())
      + " " + two(d.getHours()) + ":" + two(d.getMinutes())
  }
  // Project ids whose subtree is folded shut in the left column.
  property var collapsed: ({})

  property bool statePending: false
  property bool listPending: false
  property bool detailPending: false
  property bool subtreePending: false
  property bool commandPending: false
  // The container whose branch the in-flight `subtree` fetch is for, so a late
  // reply for a project the cursor has already left is ignored.
  property string subtreeWantedId: ""
  // While the left cursor is moving, the branch to fetch once it settles.
  property string pendingSubtreeId: ""
  // Set when capture-and-edit opens an item, so the caret lands in the first
  // editor field once that item's form has loaded.
  property bool focusEditorPending: false
  readonly property bool busy: root.statePending || root.listPending || root.detailPending
    || root.subtreePending || root.commandPending

  // ---- Processes ------------------------------------------------------------
  //
  // Quickshell only launches a Process when `running` is set, so each command
  // is followed by an explicit `running = true`; `false` first is what makes a
  // repeat run replace the previous command instead of being ignored.

  function exec(proc, args) {
    proc.command = args
    proc.running = false
    proc.running = true
  }

  // One call fills the whole panel: settings, stats, the context list, the
  // area/project containers and the database facts. The middle column is
  // loaded separately — a list, a calendar, or a container's subtree.
  Process {
    id: stateProc
    onExited: { root.statePending = false; root.maybeRefreshDetail() }
    stdout: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out === "") return
        try {
          root.applyState(JSON.parse(out))
        } catch (e) {
          root.status = "could not read the panel state"
        }
      }
    }
  }

  Process {
    id: listProc
    onExited: root.listPending = false
    stdout: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out === "") return
        try {
          var data = JSON.parse(out)
          if (root.contextFocused) {
            // `list tasks --tag` already returns plain items, so the generic
            // row builder is the whole of it.
            root.applyListPage(root.buildRows("", data), (data.items || []).length)
          } else if (root.view.key === "calendar") {
            root.listMore = false
            root.loadCalendar(data)
          } else {
            root.applyListPage(root.buildRows(root.view.key, data),
                               (data.items || []).length)
          }
        } catch (e) {
          root.status = "could not read the list"
        }
      }
    }
  }

  // A focused project or area is fetched one branch at a time: its nested
  // projects and the open actions filed under each, so the middle column can
  // group the tasks as it always did without the whole tree in memory.
  Process {
    id: subtreeProc
    onExited: { root.subtreePending = false; root.maybeRefreshDetail() }
    stdout: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out === "") return
        try {
          var node = JSON.parse(out).container
          if (!node) return
          if (root.subtreeWantedId !== String(node.id)) return
          root.projectFocus = node
          root.rows = root.buildProjectRows(node)
          root.rowSelected = Math.min(root.rowSelected, Math.max(0, root.rows.length - 1))
          while (root.rowSelected < root.rows.length
                 && root.rows[root.rowSelected].kind === "header") root.rowSelected++
          root.syncDetailToSelection()
        } catch (e) {
          root.status = "could not read the container"
        }
      }
    }
  }

  Process {
    id: detailProc
    onExited: root.detailPending = false
    stdout: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out === "") return
        try {
          var d = JSON.parse(out)
          if (d.item) {
            // Ignore a reply for an item that is no longer selected.
            if (root.detailWantedKind !== "item"
                || String(d.item.id || "") !== root.detailWantedId) return
            root.detail = d
            root.seedForm()
          } else if (d.items) {
            // A context lists what it holds rather than being one item.
            if (root.detailWantedKind !== "tag") return
            root.detail = { kind: "tagList", name: root.detailName, items: d.items }
          } else {
            root.detail = d
          }
        } catch (e) {
          root.status = "could not read the item"
        }
      }
    }
  }

  Process {
    id: commandProc
    onExited: {
      root.commandPending = false
      statusTimer.restart()
      root.reload()
      root.refreshBar()
    }
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
        } catch (e) {
          // Commands that print nothing useful still succeeded if they exited 0.
        }
      }
    }
  }

  // Capture returns the new item, so the down arrow can capture and then open
  // the structured editor on exactly that item.
  Process {
    id: captureProc
    onExited: { statusTimer.restart(); root.reload(); root.refreshBar() }
    stdout: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out === "") return
        try {
          var d = JSON.parse(out)
          if (d.ok === false && d.error) { root.status = String(d.error); return }
          if (d.item && d.item.id) root.showItem(d.item.id, true)
        } catch (e) {
          // A capture that printed nothing still succeeded if it exited 0.
        }
      }
    }
  }

  Process {
    id: completeProc
    stdout: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out === "") return
        var d = null
        try { d = JSON.parse(out) } catch (e) { return }
        if (!d || !d.completion) return
        var field = root.completeField
        // A keystroke since the request means the answer is already stale.
        if (!field || field.text !== root.completeText) return
        var done = d.completion
        if (root.completeKind !== "date") {
          var tail = root.completePrefix
          if (field.text.slice(-tail.length) !== tail) return
          done = field.text.slice(0, field.text.length - tail.length) + done
        }
        root[root.completeProp] = done
        if (root.formSeededId !== "" && String(root.completeProp).indexOf("edit") === 0)
          root.formDirty = true
      }
    }
  }

  // The pill's text lives in shell.json, not in the database, so it is written
  // with the bar CLI. A failure there is worth saying out loud, because the
  // pane would otherwise look like it simply did nothing.
  Process {
    id: barSetProc
    stderr: StdioCollector {
      onStreamFinished: {
        var out = String(this.text).trim()
        if (out !== "") root.status = out
      }
    }
  }

  // ---- Loading --------------------------------------------------------------

  function reload() {
    if (!root.opened) return
    root.stale = false
    // Everything that does not depend on the selection arrives in one call.
    // The middle column is (re)loaded once that lands, so it reads the freshest
    // containers.
    if (!root.statePending) {
      root.statePending = true
      exec(stateProc, [root.cli, "state", "--json"])
    }
  }

  // Fold one `gtd state` payload into the panel, then refresh whichever column
  // the keyboard is looking at.
  function applyState(s) {
    root.applySettings(s.settings || {})

    root.contextData = { items: s.contexts || [] }
    root.contextRows = root.flattenContexts(root.contextData)
    root.clampContext()

    root.treeData = { areas: s.areas || [], loose: [], tags: s.contexts || [],
                      counts: s.stats || {} }
    root.treeRows = root.flattenTree(root.treeData)
    root.clampTree()

    var db = s.db || {}
    root.dbPath = db.path || ""
    root.dbInfo = db.path ? (db.path + "   " + (db.items || 0) + " open items, "
      + Math.round((db.size || 0) / 1024) + " KiB") : ""

    root.resyncMiddle()
    root.maybeRefreshDetail()
  }

  function applySettings(d) {
    root.dueTime = d.due_time || "18:00"
    root.deferTime = d.defer_time || "09:00"
    root.timeFormat = d.time_format || "24h"
    root.emptyProjectsKeepDays = d.auto_remove_empty_projects !== undefined
      ? Number(d.auto_remove_empty_projects) : 7
    root.emptyContextsKeepDays = d.auto_remove_empty_contexts !== undefined
      ? Number(d.auto_remove_empty_contexts) : 7
    root.doneRetentionDays = d.done_retention_days !== undefined
      ? Number(d.done_retention_days) : 30
    root.archiveRetentionDays = d.archive_retention_days !== undefined
      ? Number(d.archive_retention_days) : 0
    root.showCompleted = d.calendar_show_completed === "on"
    root.appTheme = d.theme || "omarchy"
    root.fullscreenWidthLimit = d.fullscreen_width_limit !== undefined
      ? Number(d.fullscreen_width_limit) : 768
    gtdTheme.applyPreset(root.appTheme)
  }

  // After the state lands, re-point the middle column at whatever is selected:
  // a container's branch, a context's tasks, or the current nav view.
  function resyncMiddle() {
    if (root.projectFocused) { root.resyncProjectFocus(); return }
    if (root.contextFocused) { root.resyncContextFocus(); return }
    root.reloadList()
  }

  function loadSubtree(id) {
    if (!id) return
    subtreeDebounce.stop()
    root.subtreeWantedId = String(id)
    root.subtreePending = true
    exec(subtreeProc, [root.cli, "subtree", id, "--json"])
  }

  // Walking the left cursor with the arrow keys should not spawn a process per
  // row. Wait a beat for the cursor to settle, then fetch the branch it landed
  // on; holding the key down refreshes the wait instead of piling up calls.
  function scheduleSubtree(id) {
    if (!id) return
    root.pendingSubtreeId = String(id)
    root.subtreeWantedId = String(id)
    subtreeDebounce.restart()
  }

  function flushSubtree() {
    // Only a timer still waiting to fire needs flushing; an in-flight or done
    // fetch is already the settled cursor's branch.
    if (!subtreeDebounce.running) return
    subtreeDebounce.stop()
    if (root.pendingSubtreeId) root.loadSubtree(root.pendingSubtreeId)
  }

  Timer {
    id: subtreeDebounce
    interval: 80
    repeat: false
    onTriggered: if (root.pendingSubtreeId) root.loadSubtree(root.pendingSubtreeId)
  }

  function reloadList() {
    if (!root.opened) return
    // A focused project or area shows its own branch, fetched one container at
    // a time rather than held whole in memory.
    if (root.projectFocused) {
      root.listMore = false
      root.loadSubtree(root.projectFocus.id)
      return
    }
    // A focused context has no subtree sitting in memory the way a project
    // does, so ask for its tasks outright. `tasks` is every open action, so a
    // context reads the same however the work happens to be filed.
    if (root.contextFocused) {
      root.beginList(["list", "tasks", "--tag",
                      root.contextFocus.name, "--limit", "200"])
      return
    }
    // Restarting the process, rather than skipping while it runs, means a view
    // switch during a load can never apply the old view's data. Read the view
    // straight from the index: `root.view` may still hold the previous value
    // when this runs from onViewIndexChanged.
    var v = root.views[Math.max(0, Math.min(root.viewIndex, root.views.length - 1))]
    if (v && v.key === "calendar") {
      root.listArgs = []
      root.listOffset = 0
      root.listMore = false
      root.listPending = true
      exec(listProc, [root.cli, "calendar", "--month", root.calendarMonth, "--all", "--json"])
      return
    }
    if (v && v.key === "search") {
      var q = root.searchQuery.trim()
      if (q === "") {
        root.rows = []; root.rowSelected = 0; root.listPending = false
        root.listArgs = []; root.listMore = false
        return
      }
      root.beginList(["search", q, "--limit", "200"])
      return
    }
    root.beginList((v ? v.args : []).slice())
  }

  // Start a paged list: remember the query, then ask for the first page. A
  // later page is the same request with a bigger offset.
  function beginList(args) {
    root.listArgs = args
    root.listOffset = 0
    root.listMore = false
    root.listPending = true
    root.loadListPage(0)
  }

  // The page size a query asked for, or 0 when it is not paged at all. Only a
  // query with an explicit limit is worth paging; the rest are small.
  function argsLimit(args) {
    var at = args ? args.indexOf("--limit") : -1
    return at >= 0 ? Number(args[at + 1]) || 0 : 0
  }

  function loadListPage(offset) {
    var args = root.listArgs.slice()
    if (offset > 0) args = args.concat(["--offset", String(offset)])
    root.listRequestOffset = offset
    exec(listProc, [root.cli].concat(args).concat(["--json"]))
  }

  // The cursor is nearing the end of what is loaded and the last page was full:
  // fetch the next one and append it, so scrolling keeps flowing.
  function maybeLoadMore() {
    if (root.projectFocused || root.listPending || !root.listMore) return
    if (root.rowSelected >= root.rows.length - 3) {
      root.listPending = true
      root.loadListPage(root.listOffset)
    }
  }

  // Fold one page of a list into the middle column. A first page replaces the
  // rows; a later page appends, so scrolling keeps its place.
  function applyListPage(pageRows, count) {
    var append = root.listRequestOffset > 0
    root.rows = append ? root.rows.concat(pageRows) : pageRows
    var limit = root.argsLimit(root.listArgs)
    root.listOffset = root.listRequestOffset + count
    root.listMore = limit > 0 && count >= limit
    root.rowSelected = Math.min(root.rowSelected, Math.max(0, root.rows.length - 1))
    // A grouped list can land the cursor on a heading; the right column and
    // Enter only act on rows, so step forward to the first real one.
    while (root.rowSelected < root.rows.length
           && root.rows[root.rowSelected].kind === "header") root.rowSelected++
    // A hand-off (the panel's Enter) wants the cursor on one task. Land it now
    // that the rows are known, so the highlight and the right pane both follow.
    if (root.pendingSelectId !== "" && root.listRequestOffset === 0) {
      var wanted = root.pendingSelectId
      root.pendingSelectId = ""
      for (var s = 0; s < root.rows.length; s++) {
        if (String(root.rows[s].id) === wanted) { root.rowSelected = s; break }
      }
    }
    // The selection is live even before the code loads: show its editor
    // straight away instead of an empty pane.
    root.syncDetailToSelection()
  }

  function reloadDetail(forceId) {
    var target = forceId ? { id: forceId, kind: "item" } : root.selectedItem()
    if (!target || !target.id) {
      root.detail = null
      root.detailWantedId = ""
      root.detailWantedKind = ""
      return
    }
    // Back-to-back calls for the same item (the poll, and refreshing after a
    // command both land here) must not restart the `gtd show` already in
    // flight: killing it mid-write can hand the collector a partial payload,
    // which reads as a JSON parse error. A redundant request is a no-op.
    if (!forceId && root.detailPending && String(target.id) === root.detailWantedId)
      return
    // A different selection still restarts the load — the latest pick wins —
    // and the handler below matches every reply against the wanted id, so a
    // stale one is never applied.
    root.detailWantedId = String(target.id)
    root.detailPending = true
    // Contexts live in their own table, where `gtd show` would fail, so list
    // what they hold instead.
    if (target.kind === "tag") {
      root.detailWantedKind = "tag"
      root.detailName = target.name || target.title
      exec(detailProc, [root.cli, "list", "tasks", target.flag || "--tag",
                        root.detailName, "--limit", "200", "--json"])
      return
    }
    root.detailWantedKind = "item"
    exec(detailProc, [root.cli, "show", target.id, "--json"])
  }

  // Open an item by id in the right column, from anywhere in the UI. When
  // `edit` is true (the ↓ capture-and-edit path) the caret is placed in the
  // first editor field as soon as the form loads.
  function showItem(id, edit) {
    if (!id) return
    root.topFocused = false
    root.column = 2
    root.settingsOpen = false
    root.detail = null
    root.formDirty = false
    root.formSeededId = ""
    root.focusEditorPending = edit === true
    // Same flickable as the settings pane: a task opens at its own top.
    detailFlick.contentY = 0
    root.reloadDetail(id)
  }

  // The panel's Enter hands one task over: land on Today, drop the cursor on
  // the task's row, and let the right column show its editor. The task always
  // comes from the panel's Today list, so the Today view is where its row sits.
  function showItemSelected(id) {
    if (!id) return
    root.topFocused = false
    root.projectFocus = null
    root.contextFocus = null
    root.column = 1
    root.pendingSelectId = String(id)
    var today = root.viewIndexFor("today")
    if (root.viewIndex !== today) root.viewIndex = today
    else if (root.opened) root.reloadList()
  }

  // Point the middle cursor at a row and let the right column follow it, so a
  // single click (or a j/k step) both selects and reveals the editor. A context
  // row is the exception: picking one drills into that context, its tasks
  // filling the middle column exactly as the left switcher does, rather than
  // pushing them into the right column.
  function selectRow(index) {
    if (index < 0 || index >= root.rows.length) return
    var row = root.rows[index]
    if (row && row.kind === "tag") { root.focusContextRow(row); return }
    root.topFocused = false
    root.column = 1
    root.rowSelected = index
    root.reloadDetail()
  }

  // Focus a context picked from the middle column: swap the left tree to
  // contexts, land its cursor on the matching row, and let the middle column
  // reload the context's own task list. The nav view's flat context list is
  // left behind; asking for it again (its key, or the switcher) brings it back.
  function focusContextRow(row) {
    var name = row.name || row.title
    if (!name) return
    root.topFocused = false
    root.projectFocus = null
    root.leftMode = "contexts"
    root.column = 0
    root.leftSection = "tree"
    for (var i = 0; i < root.contextRows.length; i++) {
      if (root.contextRows[i].name === name) {
        root.contextSelected = i
        return
      }
    }
    // Not in the tree (a filtered-out context): focus it straight away.
    root.contextFocus = row.raw || row
    root.reloadList()
  }

  // Make the editor show whatever the middle cursor now sits on, loading it
  // only when it is not already the one on screen. Keeps the pane in step with
  // a fresh list without a redundant `gtd show` on every poll.
  function syncDetailToSelection() {
    if (!root.opened || root.column < 1) return
    var target = root.selectedItem()
    if (!target || !target.id) return
    if (root.detail && String(target.id) === root.detailWantedId) return
    root.reloadDetail()
  }

  function maybeRefreshDetail() {
    // Only refresh something that is actually on screen. During a load the
    // detail is null, and reloading then would clear the request in flight.
    // The right column mirrors the middle cursor, so a task stays refreshed
    // even when the keyboard never stepped into the editor.
    if (root.opened && root.detail && root.column >= 1) root.reloadDetail()
  }

  // ---- The selection --------------------------------------------------------

  function selectedTreeRow() {
    var row = root.treeRows[root.treeSelected]
    return row || null
  }

  // Whether a tree row should paint as active. While a container is focused,
  // the focused row stays lit even when the middle column has the cursor, so
  // the project reads as the current context. Otherwise it is the row the tree
  // selection points at, and only while the tree column is in play.
  function treeRowActive(row) {
    if (!row) return false
    if (root.projectFocused)
      return row.modelData.id === root.projectFocus.id
        && row.modelData.kind === root.projectFocus.kind
    return row.index === root.treeSelected && root.column === 0
      && root.leftSection === "tree" && !root.topFocused
  }

  function selectedRow() {
    var row = root.rows[root.rowSelected]
    return row || null
  }

  // One place that answers "what is selected", so the right column, the
  // keyboard and every button agree.
  function selectedItem() {
    if (root.column === 0) {
      // The left column shows one tree at a time, so which list answers
      // "what is selected" depends on which one is on screen.
      if (root.leftMode === "contexts") {
        var context = root.selectedContextRow()
        if (!context || context.kind === "header") return null
        return context
      }
      var row = root.selectedTreeRow()
      if (!row || row.kind === "header") return null
      return row
    }
    if (root.column === 1) {
      var cell = root.selectedRow()
      if (!cell || cell.kind === "header" || cell.kind === "step") return null
      return cell
    }
    var shown = root.detail && root.detail.item ? root.detail.item : root.detail
    if (!shown || !shown.id) return null
    return { kind: shown.kind || "item", id: shown.id,
             title: shown.title || shown.name, raw: shown }
  }

  // Whether the selection is something a verb can act on. Headers, step
  // checklists and history rows carry no id to address, so save and the action
  // buttons no-op instead of throwing.
  function editable(target) {
    if (!target || !target.id) return false
    return target.kind !== "log" && target.kind !== "summary"
      && target.kind !== "header" && target.kind !== "step"
  }

  // A task whose deadline has passed and which is still open.
  function isLate(item) {
    if (!item || !item.due_at) return false
    var status = item.status || ""
    if (status === "done" || status === "dropped" || status === "trashed" || status === "archived") return false
    return String(item.due_at).slice(0, 16) < root.nowStamp()
  }

  // Every open task under a project, and how many of them are late, so a
  // project row can read `late/total`. The engine rolls the counts up when it
  // builds the tree, so the panel does not have to walk the actions; a payload
  // without the counts (an older reply) still falls back to the walk.
  function subtreeCounts(project) {
    if (!project) return { total: 0, late: 0 }
    if (project.total !== undefined)
      return { total: Number(project.total) || 0, late: Number(project.late) || 0 }
    var total = 0
    var late = 0
    var actions = project.actions || []
    for (var a = 0; a < actions.length; a++) {
      total += 1
      if (root.isLate(actions[a])) late += 1
    }
    var subs = project.projects || []
    for (var s = 0; s < subs.length; s++) {
      var child = root.subtreeCounts(subs[s])
      total += child.total
      late += child.late
    }
    return { total: total, late: late }
  }

  // The middle column for a focused container. For a project that is its whole
  // subtree — the project's own tasks, then each subproject as a header with
  // its tasks under it — so a project row's `late/total` badge always matches
  // what the list holds. For an area it is the area's top-level projects.
  function buildProjectRows(container) {
    var out = []
    if (!container) return out
    if (container.kind === "area") {
      var tops = container.projects || []
      for (var t = 0; t < tops.length; t++) {
        var counts = root.subtreeCounts(tops[t])
        out.push({ kind: "project", id: tops[t].id, title: tops[t].title,
                   meta: counts.late + "/" + counts.total,
                   late: counts.late > 0, raw: tops[t] })
      }
      return out
    }
    function walk(node) {
      var actions = node.actions || []
      for (var a = 0; a < actions.length; a++) {
        var act = actions[a]
        // The same defer mark the calendar gives work scheduled for a day, so a
        // deferred task reads identically across the tree and the day's list.
        var deferred = gtdTheme.isDeferred(act)
          || gtdTheme.scheduledIntoToday(act, root.today)
        out.push({ kind: "item", id: act.id, title: act.title,
                   meta: root.itemMeta(act), glyph: gtdTheme.rowGlyph(act, deferred),
                   raw: act, deferred: deferred, late: root.isLate(act),
                   archived: root.isArchived(act),
                    dead: act.status === "done" || act.status === "dropped"
                      || act.status === "trashed" || act.status === "archived" })
      }
      var subs = node.projects || []
      for (var s = 0; s < subs.length; s++) {
        out.push({ kind: "header", id: subs[s].id, title: subs[s].title,
                   meta: "", raw: subs[s] })
        walk(subs[s])
      }
    }
    walk(container)
    return out
  }

  // Walk the tree payload for a container id, so a reload can refresh the raw
  // object the focused middle column was built from.
  function findContainerById(data, id) {
    if (!data || !id) return null
    function search(list) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].id === id) return list[i]
        var found = search(list[i].projects || [])
        if (found) return found
      }
      return null
    }
    return search(data.areas || [])
  }

  function clampRow() {
    if (root.rowSelected >= root.rows.length)
      root.rowSelected = Math.max(0, root.rows.length - 1)
  }

  // The header's "n rows" count. A focused project's rows include subproject
  // labels, which are headings rather than tasks, so they are not counted.
  function rowCount() {
    if (!root.projectFocused) return root.rows.length
    var n = 0
    for (var i = 0; i < root.rows.length; i++)
      if (root.rows[i].kind !== "header") n++
    return n
  }

  // Point the middle column at whatever the left tree has selected. Projects
  // and areas both focus; anything else (an empty selection) falls back to the
  // nav view.
  function syncProjectFocus() {
    var row = root.selectedTreeRow()
    if (!row || (row.kind !== "project" && row.kind !== "area")) {
      root.projectFocus = null
      root.subtreeWantedId = ""
      root.pendingSubtreeId = ""
      subtreeDebounce.stop()
      return
    }
    // Already focused on this exact container: keep the middle selection
    // instead of resetting it, so stepping out to the tree and back is cheap.
    if (root.projectFocus && root.projectFocus.id === row.raw.id
        && root.projectFocus.kind === row.raw.kind)
      return
    // Stand the container up now so its row lights and the columns agree,
    // then pull its branch's tasks once the cursor settles.
    root.projectFocus = row.raw
    root.rows = []
    root.rowSelected = 0
    root.scheduleSubtree(row.raw.id)
  }

  // After the containers reload, re-point the focus at the fresh one; if the
  // project is gone, drop back to the nav view. The branch itself is fetched
  // afresh, since only the containers came down with the state.
  function resyncProjectFocus() {
    if (!root.projectFocus || !root.projectFocus.id) return
    var fresh = root.findContainerById(root.treeData, root.projectFocus.id)
    if (!fresh) {
      root.projectFocus = null
      root.subtreeWantedId = ""
      root.pendingSubtreeId = ""
      root.reloadList()
      return
    }
    // Keep the left highlight on the focused row even if rows shifted.
    for (var i = 0; i < root.treeRows.length; i++) {
      if (root.treeRows[i].id === fresh.id && root.treeRows[i].kind === fresh.kind) {
        root.treeSelected = i
        break
      }
    }
    root.loadSubtree(fresh.id)
  }

  function toggleProject(id) {
    if (!id) return
    var next = {}
    for (var key in root.collapsed) next[key] = root.collapsed[key]
    if (next[id]) delete next[id]
    else next[id] = true
    root.collapsed = next
    root.treeRows = root.flattenTree(root.treeData)
    root.clampTree()
  }

  // ---- The context tree -----------------------------------------------------
  //
  // Contexts nest by name — "home" then "home>kitchen" — and `gtd list tags`
  // already returns them ordered by name, so the tree is that list read as
  // depth. The parent row sorts before its children, so indentation is enough
  // to show the shape without a separate hierarchy walk.

  function flattenContexts(data) {
    var out = []
    var places = data && data.items ? data.items : []
    for (var i = 0; i < places.length; i++) {
      var full = places[i].name || ""
      var depth = (full.match(/>/g) || []).length
      var leaf = depth > 0 ? full.slice(full.lastIndexOf(">") + 1) : full
      out.push({ kind: "tag", id: places[i].id, title: "#" + leaf, name: full,
                 depth: depth, open_count: places[i].open_count || 0,
                 raw: places[i] })
    }
    return out
  }

  function clampContext() {
    if (root.contextSelected >= root.contextRows.length)
      root.contextSelected = Math.max(0, root.contextRows.length - 1)
  }

  function selectedContextRow() {
    var row = root.contextRows[root.contextSelected]
    return row || null
  }

  // Whether a context row should paint as active: the focused one while the
  // middle column is showing its tasks, otherwise the row the left cursor is on.
  function contextRowActive(row) {
    if (!row) return false
    if (root.contextFocused)
      return row.modelData.name === root.contextFocus.name
    return row.index === root.contextSelected && root.column === 0
      && root.leftSection === "tree" && !root.topFocused
  }

  // Point the middle column at whichever context the left cursor is on.
  function syncContextFocus() {
    var row = root.selectedContextRow()
    if (!row || row.kind !== "tag") {
      root.contextFocus = null
      return
    }
    // Already pointed here: keep the middle selection rather than jumping back
    // to the top every time the cursor passes over this row.
    if (root.contextFocus && root.contextFocus.name === row.name) return
    root.contextFocus = row.raw
    root.rows = []
    root.rowSelected = 0
    root.reloadList()
  }

  // After the contexts reload, re-point the focus at the fresh object; if the
  // context is gone, drop back to the nav view.
  function resyncContextFocus() {
    if (!root.contextFocus || !root.contextFocus.name) return
    for (var i = 0; i < root.contextRows.length; i++) {
      if (root.contextRows[i].name === root.contextFocus.name) {
        root.contextSelected = i
        root.contextFocus = root.contextRows[i].raw
        root.reloadList()
        return
      }
    }
    root.contextFocus = null
    root.reloadList()
  }

  // Swap which tree the left column shows. The row the left cursor is already
  // on becomes the focused container straight away, so the lit row and the
  // middle column never disagree; an empty tree falls back to the nav view.
  function setLeftMode(mode) {
    if (root.leftMode === mode) return
    root.topFocused = false
    root.leftMode = mode
    root.column = 0
    root.settingsOpen = false
    root.projectFocus = null
    root.contextFocus = null
    root.detail = null
    root.formDirty = false
    root.formSeededId = ""
    if (mode === "contexts") {
      root.clampContext()
      if (root.contextRows.length) root.syncContextFocus()
      else root.reloadList()
    } else {
      root.clampTree()
      if (root.treeRows.length) root.syncProjectFocus()
      else root.reloadList()
    }
    root.reloadDetail()
  }

  // Left/right on a project row behave like a file tree: left folds it, right
  // unfolds it (and only then walks on into the middle column). Returns true
  // when the key was spent on folding, so the caller skips the column move.
  function foldStep(dx) {
    if (root.column !== 0 || root.leftSection !== "tree" || root.leftMode !== "projects") return false
    var row = root.selectedTreeRow()
    if (!row || row.kind !== "project") return false
    if (dx < 0) {
      if (row.collapsed) return false
      root.toggleProject(row.id)
      return true
    }
    if (row.collapsed) {
      root.toggleProject(row.id)
      return true
    }
    return false
  }

  function clampTree() {
    if (root.treeSelected >= root.treeRows.length)
      root.treeSelected = Math.max(0, root.treeRows.length - 1)
  }

  onColumnChanged: {
    // Stepping back into the tree re-focuses the selected container, so the
    // middle column never keeps showing a nav view while the tree is active.
    if (root.column === 0) {
      if (root.leftSection === "tree") root.syncTreeFocus()
    } else if (root.leftMode === "projects" && root.projectFocused) {
      // Leaving the tree before the settle timer fired: fetch now, so the list
      // is there the moment the cursor enters it.
      root.flushSubtree()
    }
    if (root.column !== 2) {
      root.settingsOpen = false
      root.detail = null
      root.formDirty = false
      root.formSeededId = ""
      // Moving between the tree and the list changes which row is "selected"
      // without changing its index, so the row-selection signal may not fire.
      // Reload here, or clicking a task that is already row 0 shows nothing.
      root.reloadDetail()
    } else if (!root.settingsOpen) {
      root.reloadDetail()
    }
  }

  // Selecting a container in the left tree pours its tasks into the middle
  // column; the right column still shows the container's own detail.
  onTreeSelectedChanged: if (root.column === 0) {
    root.settingsOpen = false
    root.syncProjectFocus()
    root.reloadDetail()
  }
  onContextSelectedChanged: if (root.column === 0 && root.leftMode === "contexts") {
    root.settingsOpen = false
    root.syncContextFocus()
    root.reloadDetail()
  }
  onRowSelectedChanged: if (root.column === 1) {
    root.settingsOpen = false
    root.reloadDetail()
    root.maybeLoadMore()
  }
  onViewIndexChanged: {
    root.rowSelected = 0
    root.settingsOpen = false
    root.detail = null
    root.formDirty = false
    root.formSeededId = ""
    root.reloadList()
  }

  // ---- Row building ---------------------------------------------------------

  function stamp16(value) {
    if (!value) return ""
    var text = String(value)
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

  function humanMinutes(mins) {
    var n = parseInt(mins)
    if (!n || n <= 0) return ""
    var h = Math.floor(n / 60)
    var m = n % 60
    if (h && m) return h + "h" + (m < 10 ? "0" : "") + m + "m"
    if (h) return h + "h"
    return m + "m"
  }

  // The store keeps a repeat rule as {interval, unit, from}; the editor works
  // in the same human text the CLI takes ("every week", "every 2 months"), so
  // this renders a stored rule back to that text for the field to show.
  function repeatText(rule) {
    if (!rule) return ""
    var interval = parseInt(rule.interval || 1)
    var unit = String(rule.unit || "week")
    var text = interval > 1 ? "every " + interval + " " + unit + "s"
                            : "every " + unit
    if (String(rule.from || "complete") === "schedule") text += " after schedule"
    return text
  }

  // The three one-tap periods, plus "never" to clear the rule. The value is
  // the same human text the field holds, so a preset lights up whenever the
  // field matches it.
  readonly property var repeatPresets: [
    { value: "every week", label: "1 week" },
    { value: "every month", label: "1 month" },
    { value: "every year", label: "1 year" },
    { value: "", label: "never" }
  ]

  // Every task row uses the same layout: decorator and title at the left,
  // deadline at the right. Defer dates and completion stamps are not deadlines.
  function itemMeta(item) {
    if (item.due_at) return root.stamp16(item.due_at)
    return ""
  }

  // The record the store keeps on every task: when it was captured, last
  // edited, and finished. Read-only, and the same stamps the database holds.
  function itemTimes(item) {
    if (!item) return ""
    var bits = []
    if (item.created_at) bits.push("created " + root.stamp16(item.created_at))
    if (item.updated_at) bits.push("edited " + root.stamp16(item.updated_at))
    if (item.completed_at) bits.push("completed " + root.stamp16(item.completed_at))
    return bits.join("   ·   ")
  }

  // Finished tasks are struck through: visible only in search, and visibly not
  // something still to be done.
  function isArchived(item) {
    return !!item && (item.status === "done" || item.status === "archived")
  }

  function flattenTree(data) {
    var out = []
    if (!data) return out
    var areas = data.areas || []
    for (var i = 0; i < areas.length; i++) {
      var area = areas[i]
      var titled = !!area.title
      if (titled) {
        out.push({ kind: "area", depth: 0, id: area.id || "",
                   title: area.title,
                   meta: (area.projects || []).length + " projects", raw: area })
      }
      var projects = area.projects || []
      for (var p = 0; p < projects.length; p++)
        root.pushProject(out, projects[p], titled ? 1 : 0)
    }
    return out
  }

  // Projects nest as deep as the data does; walk the whole branch so every
  // subproject shows up, indented by depth. This column is the shape of the
  // work — areas and projects only. The tasks live in the middle column, for
  // whichever project is selected. A folded project keeps its own row and,
  // like a file tree, hides everything under it.
  function pushProject(out, project, depth) {
    var subs = project.projects || []
    var counts = root.subtreeCounts(project)
    var folded = !!root.collapsed[project.id]
    out.push({ kind: "project", depth: depth, id: project.id, title: project.title,
               meta: counts.late + "/" + counts.total, late: counts.late,
               total: counts.total, collapsed: folded, raw: project })
    if (folded) return
    for (var s = 0; s < subs.length; s++)
      root.pushProject(out, subs[s], depth + 1)
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n }

  // What the calendar symbols mean, per day. Every rule is separate, so one
  // task can earn several different marks: a task that has expired, is waiting
  // on someone and is flagged shows the red, orange, blue and purple all at
  // once. Grey is the plain backlog — not expired, not flagged. Green is a
  // task finished on that day; it only earns a mark when the completed knob
  // is on, so done work stays off the grid by default.
  function calendarMarks(cell) {
    var marks = { red: 0, orange: 0, blue: 0, purple: 0, grey: 0, green: 0 }
    if (!cell) return marks
    var openGroups = { overdue: true, due: false, scheduled: false }
    var names = ["overdue", "due", "scheduled", "completed"]
    for (var n = 0; n < names.length; n++) {
      var list = cell[names[n]] || []
      for (var i = 0; i < list.length; i++) {
        var entry = list[i]
        var complete = entry.status === "done" || entry.status === "archived"
        if (complete) { if (root.showCompleted) marks.green += 1; continue }
        var late = names[n] === "overdue" || root.isLate(entry)
        var waiting = entry.status === "waiting"
        var flagged = (entry.priority || 0) > 0
        if (late) marks.red += 1
        if (flagged) marks.orange += 1
        if (late && waiting) marks.blue += 1
        if (late && waiting && flagged) marks.purple += 1
        if (!late && !flagged) marks.grey += 1
      }
    }
    return marks
  }

  // How many symbols a day actually shows: its marks, minus the green ones
  // that are only earned when the completed knob is on.
  function calendarMarkTotal(cell) {
    var m = root.calendarMarks(cell)
    return m.red + m.orange + m.blue + m.purple + m.grey + m.green
  }

  function buildCalendarWeeks(data) {
    var days = data && data.days ? data.days : []
    if (!days.length) return []
    var byDate = {}
    for (var i = 0; i < days.length; i++) byDate[days[i].date] = days[i]
    // Anchor the grid to the month being shown. With --all the data window can
    // begin at an earlier past deadline, so data.start is not the month.
    var parts = String(root.calendarMonth || data.start).split("-")
    var year = parseInt(parts[0])
    var month = parseInt(parts[1]) - 1
    var firstDow = (new Date(year, month, 1).getDay() + 6) % 7
    var weeks = []
    var cursor = new Date(year, month, 1 - firstDow)
    for (var w = 0; w < 6; w++) {
      var week = []
      for (var d = 0; d < 7; d++) {
        var key = cursor.getFullYear() + "-" + pad2(cursor.getMonth() + 1)
          + "-" + pad2(cursor.getDate())
        var cell = byDate[key] || { overdue: [], due: [], scheduled: [],
                                    completed: [], count: 0 }
        // A day reads as late when it already holds an overdue task, or when a
        // task due or starting today has passed its time.
        var late = (cell.overdue || []).length > 0
        var openEntries = (cell.due || []).concat(cell.scheduled || [])
        for (var z = 0; z < openEntries.length && !late; z++)
          if (root.isLate(openEntries[z])) late = true
        var marks = root.calendarMarks(cell)
        week.push({
          date: key,
          day: cursor.getDate(),
          inMonth: cursor.getMonth() === month,
          isToday: key === root.today,
          count: cell.count || 0,
          late: late,
          overdue: (cell.overdue || []).length,
          due: (cell.due || []).length,
          scheduled: (cell.scheduled || []).length,
          completed: (cell.completed || []).length,
          marks: marks,
          sym: root.calendarMarkTotal(cell)
        })
        cursor = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + 1)
      }
      weeks.push(week)
    }
    return weeks
  }

  function calendarCellFor(date) {
    var days = root.calendarData && root.calendarData.days ? root.calendarData.days : []
    for (var i = 0; i < days.length; i++) if (days[i].date === date) return days[i]
    return null
  }

  function dayRows(cell) {
    var out = []
    if (!cell) return out
    var groups = [["overdue", "late", true], ["due", "due", false],
                  ["scheduled", "defer", false]]
    if (root.showCompleted) groups.push(["completed", "done", true])
    var today = String(root.today || root.nowStamp()).slice(0, 10)
    for (var g = 0; g < groups.length; g++) {
      var list = cell[groups[g][0]] || []
      var scoped = groups[g][0] === "scheduled"
      for (var i = 0; i < list.length; i++) {
        var entry = list[i]
        // A task earns the defer mark across its whole defer window. On the day
        // it comes due the calendar files it under "due" rather than
        // "scheduled", so recompute the mark from the dates: a task whose
        // defer window has opened reads the same here as the Today list.
        var deferred = gtdTheme.isDeferred(entry)
          || gtdTheme.scheduledIntoToday(entry, today)
        out.push({ kind: "item", id: entry.id, title: entry.title,
                   meta: root.itemMeta(entry),
                   glyph: gtdTheme.rowGlyph(entry, deferred),
                   raw: entry,
                   deferred: deferred,
                   late: groups[g][0] === "overdue" || root.isLate(entry),
                   dead: groups[g][2] })
      }
    }
    return out
  }

  function selectDay(date) {
    root.calendarSelectedDate = date
    root.rows = root.dayRows(root.calendarCellFor(date))
    root.rowSelected = 0
    root.column = 1
    root.syncDetailToSelection()
  }

  function loadCalendar(data) {
    root.calendarData = data
    root.calendarWeeks = root.buildCalendarWeeks(data)
    var days = data && data.days ? data.days : []
    var pick = ""
    for (var i = 0; i < days.length; i++)
      if (days[i].date === root.today) { pick = root.today; break }
    if (!pick)
      for (var j = 0; j < days.length; j++)
        if (days[j].count) { pick = days[j].date; break }
    if (!pick && days.length) pick = days[0].date
    root.selectDay(pick)
  }

  function shiftMonth(delta) {
    var parts = root.calendarMonth.split("-")
    var d = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1 + delta, 1)
    root.calendarMonth = d.getFullYear() + "-" + pad2(d.getMonth() + 1)
    root.reloadList()
  }

  function shiftPickerMonth(delta) {
    var parts = root.pickerMonth.split("-")
    var d = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1 + delta, 1)
    root.pickerMonth = d.getFullYear() + "-" + pad2(d.getMonth() + 1)
    root.pickerWeeks = root.buildPickerWeeks(root.pickerMonth)
  }

  function buildPickerWeeks(monthKey) {
    var parts = String(monthKey).split("-")
    var year = parseInt(parts[0])
    var month = parseInt(parts[1]) - 1
    var firstDow = (new Date(year, month, 1).getDay() + 6) % 7
    var cells = []
    var cursor = new Date(year, month, 1 - firstDow)
    for (var i = 0; i < 42; i++) {
      var key = cursor.getFullYear() + "-" + pad2(cursor.getMonth() + 1)
        + "-" + pad2(cursor.getDate())
      cells.push({
        date: key,
        day: cursor.getDate(),
        inMonth: cursor.getMonth() === month,
        isToday: key === root.today
      })
      cursor.setDate(cursor.getDate() + 1)
    }
    return cells
  }

  function openPicker(target) {
    root.pickerTarget = target
    root.pickerMonth = root.today.slice(0, 7)
    root.pickerWeeks = root.buildPickerWeeks(root.pickerMonth)
    root.pickerTime = root.formatClock(root.dueTime)
    dueDatePopup.open()
  }

  // A picked day plus the box's clock. With an empty clock the day stays bare
  // and the backend folds in the settings default, exactly as before.
  function pickDate(date) {
    var time = String(root.pickerTime || "").trim()
    var value = time === "" ? date : date + " " + time
    if (root.pickerTarget === "due") root.editDue = value
    else root.editDefer = value
    root.formDirty = true
    dueDatePopup.close()
  }

  function goToday() {
    root.calendarMonth = root.today.slice(0, 7)
    root.reloadList()
  }

  function itemRow(row) {
    // A task reads the same here as on the calendar: one in its defer window,
    // or whose window has opened with the deadline still ahead, wears the defer
    // mark, grey colour and italics the calendar gives work scheduled for a day.
    var today = String(root.today || root.nowStamp()).slice(0, 10)
    var deferred = gtdTheme.isDeferred(row)
      || gtdTheme.scheduledIntoToday(row, today)
    return { kind: "item", id: row.id, title: row.title,
              meta: root.itemMeta(row), glyph: gtdTheme.rowGlyph(row, deferred),
              raw: row, deferred: deferred,
              late: root.isLate(row), archived: root.isArchived(row),
              dead: row.status === "done" || row.status === "dropped"
                || row.status === "trashed" || row.status === "archived" }
  }

  // The Today list mixes what is due today with work whose defer window covers
  // today. When such a task carries no deadline of its own, the defer date is
  // the only stamp worth showing beside it.
  function todayRow(row) {
    var item = root.itemRow(row)
    // A task in Today because its defer window opened wears the defer mark, in
    // the same grey and italics the calendar gives work scheduled for a day.
    var deferred = gtdTheme.isDeferred(row)
      || gtdTheme.scheduledIntoToday(row, root.today)
    item.deferred = deferred
    if (deferred) item.glyph = gtdTheme.rowGlyph(row, true)
    if (!item.meta && row.defer_at) item.meta = root.stamp16(row.defer_at)
    return item
  }

  // Bucket rows by the project they belong to: named buckets first,
  // alphabetical, and the tasks filed nowhere last under "No project". The
  // flat view order is the wrong question once they are on screen — what
  // blocks what is the project.
  function byProject(rows) {
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
      var groups = [["overdue", "late"], ["due", "due"], ["scheduled", "defer"]]
      if (root.showCompleted) groups.push(["completed", "done"])
      for (var i = 0; i < days.length; i++) {
        var cell = days[i]
        if (!cell.count) continue
        out.push({ kind: "header", title: cell.date + "  " + (cell.weekday || ""),
                   meta: cell.count + "", raw: cell })
        for (var g = 0; g < groups.length; g++) {
          var group = cell[groups[g][0]] || []
          var scoped = groups[g][0] === "scheduled"
          for (var j = 0; j < group.length; j++) {
            var entry = group[j]
            out.push({ kind: "item", id: entry.id, title: entry.title,
                 meta: root.itemMeta(entry),
                 glyph: gtdTheme.rowGlyph(entry, scoped), raw: entry,
                 deferred: scoped,
                 late: groups[g][0] === "overdue" || root.isLate(entry),
                 dead: groups[g][0] === "completed" })
          }
        }
      }
      return out
    }

    if (key === "review") {
      var stats = data.stats || {}
      out.push({ kind: "header", title: (data.kind || "review") + " review",
                 meta: root.today, raw: data })
      var bits = []
      var keys = ["inbox", "next", "today", "overdue", "waiting", "someday",
                  "flagged", "projects", "clarify"]
      for (var k = 0; k < keys.length; k++) {
        if (stats[keys[k]]) bits.push(keys[k] + " " + stats[keys[k]])
      }
      out.push({ kind: "summary", title: bits.join("  ·  "), meta: "", raw: data })
      // The month behind the week: what arrived, and what got finished or
      // dropped. Two numbers that only mean anything next to each other.
      if (stats.created_30 !== undefined || stats.closed_30 !== undefined) {
        out.push({ kind: "summary",
                   title: "last 30 days  ·  " + (stats.created_30 || 0)
                          + " created  ·  " + (stats.closed_30 || 0) + " closed",
                   meta: "", raw: data })
      }
      var steps = data.steps || []
      for (var s = 0; s < steps.length; s++) {
        out.push({ kind: "step", id: steps[s].key, title: steps[s].question,
                   meta: steps[s].count + " / " + steps[s].target
                     + (steps[s].ok ? "  ✓" : ""), raw: steps[s] })
      }
      return out
    }

    if (key === "contexts") {
      var tags = data.items || []
      for (var q = 0; q < tags.length; q++) {
        var fullName = tags[q].name || ""
        var depth = (fullName.match(/>/g) || []).length
        var leaf = depth > 0 ? fullName.slice(fullName.lastIndexOf(">") + 1) : fullName
        out.push({ kind: "tag", id: tags[q].id, title: "#" + leaf,
                   meta: (tags[q].open_count || 0) + " open", raw: tags[q],
                   flag: "--tag", name: fullName, depth: depth })
      }
      return out
    }

    if (key === "log") {
      var entries = data.items || []
      for (var e = 0; e < entries.length; e++) {
        var entry = entries[e]
        out.push({ kind: "log", id: entry.item_id,
                   title: (entry.detail || "").trim() || entry.title || "",
                   meta: entry.kind + "  " + root.stamp16(entry.at), raw: entry })
      }
      return out
    }

    if (key === "waiting") {
      var buckets = root.byProject(data.items || [])
      for (var b = 0; b < buckets.length; b++) {
        var bucket = buckets[b]
        out.push({ kind: "header", title: bucket.name || "No project",
                   meta: String(bucket.rows.length), raw: null })
        for (var q = 0; q < bucket.rows.length; q++)
          out.push(root.itemRow(bucket.rows[q]))
      }
      return out
    }

    if (key === "today") {
      var tItems = data.items || []
      for (var tr = 0; tr < tItems.length; tr++) out.push(root.todayRow(tItems[tr]))
      return out
    }

    var items = data.items || []
    for (var r = 0; r < items.length; r++) out.push(root.itemRow(items[r]))
    return out
  }

  // ---- Autocomplete --------------------------------------------------------
  //
  // Tab in a field asks `gtd complete` to finish it. The backend only answers
  // when the text so far is an unambiguous prefix of exactly one existing
  // name, so an ambiguous or unknown prefix leaves the field alone rather than
  // guessing; and because Tab is a deliberate act, the answer is committed
  // outright instead of being shown as a greyed-out tail to type over.
  property string completeKind: ""
  property string completeProp: ""
  property string completeText: ""
  property string completePrefix: ""
  property var completeField: null

  function askComplete(kind, prop, field, prefix) {
    if (field.text.slice(-prefix.length) !== prefix) return
    if (completeProc.running) return
    root.completeKind = kind
    root.completeProp = prop
    root.completeField = field
    root.completeText = field.text
    root.completePrefix = prefix
    exec(completeProc, [root.cli, "complete", kind, prefix, "--json"])
  }

  // The trailing quick-entry token: /Project/ or #context.
  function captureToken(text) {
    var m = String(text || "").match(/(\S*)$/)
    if (!m) return null
    var token = m[1]
    if (token.charAt(0) === "/") {
      var body = token.slice(1)
      if (body.slice(-1) === "/") return { kind: "project", prefix: body.slice(0, -1) }
      if (body.indexOf("/") >= 0) return null
      return { kind: "project", prefix: body }
    }
    if (token.charAt(0) === "#")
      return { kind: "context", prefix: token.slice(1).split('"')[0] }
    return null
  }

  function completeCaptureField() {
    var tok = root.captureToken(root.captureValue)
    if (!tok || tok.prefix === "") return
    root.askComplete(tok.kind, "captureValue", captureField, tok.prefix)
  }

  // A date lands on the configured default time: a defer date on the defer
  // clock, everything else on the due clock. Tab asks the backend to finish it.
  function completeDateField(prop, field, defer) {
    if (field.text.trim() === "") return
    var kind = defer ? "defer" : "date"
    root.askComplete(kind, prop, field, field.text.trim())
  }

  // "YYYY-MM-DD" plus whole days, month ends and leap years included, with
  // whatever trailed the date ("18:00", " 6:00 PM") carried across untouched.
  // Anything that is not an absolute date comes back null rather than guessed
  // at: a word ("friday") is finished by Tab, and then it can be shifted too.
  function plusDays(stamp, days) {
    var m = String(stamp || "").match(/^(\d{4})-(\d{2})-(\d{2})(.*)$/)
    if (!m) return null
    var year = Number(m[1])
    var month = Number(m[2])
    var day = Number(m[3])
    var d = new Date(year, month - 1, day)
    if (d.getFullYear() !== year || d.getMonth() !== month - 1 || d.getDate() !== day)
      return null
    d.setDate(d.getDate() + days)
    function two(n) { return (n < 10 ? "0" : "") + n }
    return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate())
      + m[4]
  }

  // "+1d" on a date field: move it a day later and keep the clock, so a
  // postpone never quietly shifts the time. A bare date stays bare and keeps
  // landing on the time from settings; an empty field becomes tomorrow, which
  // is what postponing a date that has not been typed yet means.
  function shiftDate(prop, field) {
    var text = String(field.text || "").trim()
    // Read today from the clock rather than from root.today, which is a
    // binding with nothing to change it and so holds the day this shell started.
    var today = root.nowStamp().slice(0, 10)
    var moved = text === "" ? root.plusDays(today, 1) : root.plusDays(text, 1)
    if (moved === null) {
      root.status = "Tab finishes that date first"
      return
    }
    root[prop] = moved
    if (root.formSeededId !== "" && String(prop).indexOf("edit") === 0)
      root.formDirty = true
  }

  // ---- The editor -----------------------------------------------------------

  function seedForm() {
    var item = root.detail && (root.detail.item || root.detail)
    if (!item) return
    // Re-seeding the same item while the form is dirty would throw away the
    // user's unsaved edits. A different item always loads fresh.
    if (root.formDirty && String(item.id || "") === root.formSeededId) return
    root.formSeededId = String(item.id || "")
    root.formDirty = false
    root.editTitle = item.title || ""
    root.editProject = item.parent_path || item.parent_title || ""
    root.editTags = (item.tags || []).join(" ")
    root.editDue = root.stamp16(item.due_at)
    root.editDefer = root.stamp16(item.defer_at)
    root.editDuration = item.duration ? root.humanMinutes(item.duration) : ""
    root.editRepeat = root.repeatText(item.repeat)
    root.editNotes = item.notes || ""
    root.editLink = item.url || ""
    var attach = item.attachments || []
    var uris = []
    for (var a = 0; a < attach.length; a++) uris.push(attach[a].uri || attach[a].name || "")
    // One file per task. An older row could still hold more than one, and the
    // first of them is the one shown; saving takes the rest away.
    root.editAttachments = uris.length > 0 ? uris[0] : ""
    if (root.focusEditorPending) {
      root.focusEditorPending = false
      Qt.callLater(function() {
        titleField.forceActiveFocus()
        titleField.selectAll()
      })
    }
  }

  function splitList(text) {
    var out = String(text || "").split(/[\s,]+/)
    var clean = []
    for (var i = 0; i < out.length; i++) {
      if (out[i] !== "") clean.push(out[i])
    }
    return clean
  }

  // One `gtd edit` carrying every field the form changed, so a save is atomic
  // and the log records it as one step. Each flag is pushed with its own value:
  // --undue and --undefer take none, so handing them an empty
  // argument would leave argparse with a stray positional and fail the save.
   function saveEdits() {
     var target = root.selectedItem()
     if (!root.editable(target)) return
     var original = root.detail && (root.detail.item || root.detail)
     var args = ["edit", target.id]

     var title = root.editTitle.trim()
    if (title !== (original ? (original.title || "") : "")) {
      if (title === "") return
      args.push("--title", title)
    }

    var project = root.editProject.trim()
    var originalProject = original ? (original.parent_path || original.parent_title || "") : ""
    if (project !== originalProject) {
      // There is no "no project" flag; the inbox is where an unfiled item lives.
      if (project === "") args.push("--inbox")
      else args.push("--project", project)
    }


    var due = root.editDue.trim()
    if (due !== root.stamp16(original && original.due_at)) {
      if (due === "") args.push("--undue")
      else args.push("--due", due)
    }

    var defer = root.editDefer.trim()
    if (defer !== root.stamp16(original && original.defer_at)) {
      if (defer === "") args.push("--undefer")
      else args.push("--defer", defer)
    }

    var wasDuration = original && original.duration
      ? root.humanMinutes(original.duration) : ""
    var duration = root.editDuration.trim()
    if (duration !== wasDuration) {
      if (duration === "") args.push("--clear-duration")
      else args.push("--duration", duration)
    }

    // The rule is stored as a struct but edited as the human text, so compare
    // the two rendered forms; --repeat only sets, --no-repeat only clears.
    var repeat = root.editRepeat.trim()
    var wasRepeat = root.repeatText(original && original.repeat)
    if (repeat !== wasRepeat) {
      if (repeat === "") args.push("--no-repeat")
      else args.push("--repeat", repeat)
    }

    // --tag only adds and --untag only removes, so diff the two lists instead of
    // saving the field as one context with spaces in its name.
    var was = (original && original.tags) || []
    var now = root.splitList(root.editTags)
    for (var i = 0; i < was.length; i++) {
      if (now.indexOf(was[i]) === -1) args.push("--untag", was[i])
    }
    for (var j = 0; j < now.length; j++) {
      if (was.indexOf(now[j]) === -1) args.push("--tag", now[j])
    }

    if (root.editNotes !== (original ? (original.notes || "") : ""))
      args.push("--notes", root.editNotes)

    var link = root.editLink.trim()
    var wasLink = original ? (original.url || "") : ""
    if (link !== wasLink) {
      if (link === "") args.push("--unlink")
      else args.push("--link", link)
    }

    // One attachment, so saving means "hold this one": anything else the task
    // was carrying goes, and the picked file comes in.
    var origUris = []
    var origAttach = (original && original.attachments) || []
    for (var p = 0; p < origAttach.length; p++)
      origUris.push(origAttach[p].uri || origAttach[p].name || "")
    var nowAttach = (root.editAttachments || "").trim()
    for (var q = 0; q < origUris.length; q++)
      if (origUris[q] !== "" && origUris[q] !== nowAttach)
        args.push("--unattach", origUris[q])
    if (nowAttach !== "" && origUris.indexOf(nowAttach) === -1)
      args.push("--attach", nowAttach)

    if (args.length === 2) { root.formDirty = false; root.status = "nothing to save"; return }
    root.formDirty = false
    root.run(args, "saved " + target.title)
  }

  function act(name) {
     var target = root.selectedItem()
     if (!root.editable(target)) return
     var id = target.id
     if (name === "done") root.run(["engage", id, "done"])
     else if (name === "drop") root.run(["engage", id, "drop"])
     else if (name === "trash") root.run(["trash", id])
     else if (name === "restore") root.run(["restore", id])
     else if (name === "inbox") root.run(["engage", id, "inbox"])
      else if (name === "start") root.run(["start", id])
      else if (name === "stop") root.run(["stop", id])
      else if (name === "wait") root.run(["engage", id, "wait"])
      else if (name === "resume") root.run(["engage", id, "resume"])
      else if (name === "hold") root.run(["edit", id, "--onhold"])
      else if (name === "flag") root.run(["flag", id])
      else if (name === "unflag") root.run(["unflag", id])
      else if (name === "postpone") root.run(["postpone", id])
      else if (name === "open") root.run(["open", id], "opened")
      else if (name === "undo") root.run(["undo", id], "undone")
      else if (name === "undone") root.run(["engage", id, "undone"], "undone")
      else if (name === "archive") root.run(["engage", id, "archive"], "archived")
      else if (name === "unarchive") root.run(["engage", id, "unarchive"], "unarchived")
      }

    // ---- Drag and drop ------------------------------------------------------
    //
    // A task row drags itself. Dropping onto a calendar day moves its deadline;
    // onto a project moves the task there; onto a context tags it. The payload
    // rides in the MIME text so nothing has to outlive the gesture in a local
    // variable a delegate destroy could take with it.

      property string dragMime: "application/x-gtd-item"

        // Decode the id off a drop's payload. `MimeData.data` hands back bytes, so
        // turn the array back into the string that started the drag.
     function dragId(drop) {
       var bytes = drop.mimeData.data(root.dragMime)
       if (bytes.length === 0) return ""
       var parts = []
       for (var i = 0; i < bytes.length; i++)
         parts.push(String.fromCharCode(bytes[i]))
     return parts.join("")
       }

     // Drop a task onto a calendar day: set its deadline to that day, keeping a
     // bare date so it lands on the clock from settings.
   function dropOnDay(date, drop) {
     if (!drop.mimeData.hasFormat(root.dragMime)) drop.accepted = false
     var id = root.dragId(drop)
     if (!id) return
     root.run(["edit", id, "--due", String(date)], "due " + String(date))
       }

     // Drop a task onto a project: move it under that project.
   function dropOnProject(row, drop) {
     if (!row || row.kind !== "project" || !row.title) {
       if (drop) drop.accepted = false
       return
         }
     if (!drop.mimeData.hasFormat(root.dragMime)) {
       drop.accepted = false
       return
         }
     var id = root.dragId(drop)
     if (!id) return
     root.run(["edit", id, "--project", String(row.title)],
               "moved to " + row.title)
       }

      // Drop a task onto a context: add the context. The backend lowercases and
      // stores it one at a time, so a task can wear many.
   function dropOnContext(row, drop) {
     if (!row || !row.name) {
       if (drop) drop.accepted = false
       return
         }
     if (!drop.mimeData.hasFormat(root.dragMime)) {
       drop.accepted = false
       return
         }
     var id = root.dragId(drop)
     if (!id) return
     root.run(["edit", id, "--tag", String(row.name)],
               "tagged " + row.name)
       }

      // Drop a task onto the project tree's blank space: it has no home, so it
      // goes back to the inbox to be clarified.
   function dropOnInbox(drop) {
     if (!drop.mimeData.hasFormat(root.dragMime)) {
       drop.accepted = false
       return
          }
     var id = root.dragId(drop)
     if (!id) return
     root.run(["edit", id, "--inbox"], "back to inbox")
        }

  // A toast in the shell's OSD — the same card the launch message uses. The
  // shell may be shutting down, so this call fails quietly.
  function showOsd(icon, message) {
    try {
      Quickshell.execDetached(
        ["omarchy-shell", "-q", "osd", "show",
         JSON.stringify({ icon: icon, message: message, duration: 2000 })])
    } catch (e) { }
  }

  // The bar pill does not share this instance's model state. Ask it to re-read
  // the counts, which re-runs the pill text and its colour.
  function refreshBar() {
    try {
      Quickshell.execDetached(
        ["omarchy-shell", "-q", "com.auino.omagtd", "refresh"])
    } catch (e) { }
  }

  function run(args, label) {
    if (root.commandPending) return
    root.commandPending = true
    root.lastVerb = args[0]
    root.status = label || args.join(" ")
    // Restart the countdown so a fresh action is not cleared early by the
    // timer left over from the previous one; onExited restarts it again.
    statusTimer.restart()
    exec(commandProc, [root.cli].concat(args).concat(["--json"]))
  }

  function capture(text) {
    var value = String(text || "").trim()
    if (value === "") return
    root.run(["capture", value], "captured")
    root.captureValue = ""
  }

  // A project is a container, not a task, so it is created with its own verb.
  function addProject(text) {
    var value = String(text || "").trim()
    if (value === "") { root.status = "name the project first"; return }
    projectNameField.text = ""
    root.run(["project", "add", value], "added project")
  }

  // Enter files the capture and stays put; the down arrow files it and opens
  // the detail editor so the rest of the fields can be filled in at once.
  // With an empty box the down arrow opens the editor for whatever is already
  // selected instead, so the key never feels dead.
  function captureAndEdit(text) {
    var value = String(text || "").trim()
    if (value === "") {
      var sel = root.selectedItem()
      if (sel && sel.id) root.showItem(sel.id, true)
      else root.status = "nothing selected"
      return
    }
    root.status = "captured"
    statusTimer.restart()
    root.exec(captureProc, [root.cli, "capture", value, "--json"])
    root.captureValue = ""
  }

  // ---- Settings -------------------------------------------------------------

  // A bare date lands on this clock. The backend validates and normalizes it,
  // so a bad entry just reports back in the status line.
  function saveDueTime(text) {
    var value = String(text || "").trim()
    if (value === "") { root.status = "give a time like 18:00"; return }
    root.run(["settings", "set", "due_time", value], "due time " + value)
  }

  // A bare defer date lands on this clock instead. The backend validates it,
  // so a bad entry just reports back in the status line.
  function saveDeferTime(text) {
    var value = String(text || "").trim()
    if (value === "") { root.status = "give a time like 09:00"; return }
    root.run(["settings", "set", "defer_time", value], "defer time " + value)
  }

  function chooseTimeFormat(fmt) {
    if (root.timeFormat === fmt) return
    root.run(["settings", "set", "time_format", fmt], "time format " + fmt)
  }

  // The fullscreen app's own palette. The command's exit reloads the settings
  // read, which calls applySettings and repaints the view. The preset is also
  // applied straight away so the change lands without waiting for the round
  // trip.
  function chooseTheme(name) {
    if (root.appTheme === name) return
    root.appTheme = name
    gtdTheme.applyPreset(name)
    root.run(["settings", "set", "theme", name], "theme " + name)
  }

  // The narrowest the full-screen window gets before it asks to be enlarged.
  // A whole number of pixels; the backend validates it, and the command's exit
  // reloads the settings read, which is what moves the threshold.
  function setFullscreenWidthLimit(px) {
    var n = Number(px)
    if (isNaN(n) || n <= 0 || n !== Math.floor(n)) {
      root.status = "width must be a whole number of pixels"
      return
    }
    if (n === root.fullscreenWidthLimit) return
    root.run(["settings", "set", "fullscreen_width_limit", String(n)],
             "full-screen width limit " + n + "px")
  }

  // Whether the calendar marks the day a task was finished. A switch in the
  // database, like the deadlines: the command's exit reloads the settings read,
  // so the tick and the green symbols follow on their own.
  function setShowCompleted(on) {
    if (root.showCompleted === on) return
    var value = on ? "on" : "off"
    root.run(["settings", "set", "calendar_show_completed", value],
             "calendar: completed tasks " + value)
  }

  // Whether the pill prints "0/0" when the system is empty. This one belongs
  // to the bar, so it goes to the bar CLI; the shell reloads, injects the new
  // settings back in, and root.hideZeroCounts follows.
  function setHideZeroCounts(hide) {
    if (root.hideZeroCounts === hide) return
    root.status = hide ? "bar pill: GTD only when empty"
                       : "bar pill: GTD 0/0 when empty"
    exec(barSetProc, ["omarchy", "bar", "set", "com.auino.omagtd",
                      "hideZeroCounts", hide ? "true" : "false", "--json"])
  }

  // How many days an empty project or context is kept before the sweeper may
  // take it off the system. A whole count of days, like the completed-task
  // retention: 0 keeps it forever, a positive number removes it once it has sat
  // empty that long. It is a knob in the database, so it goes through
  // `settings set`; the command's exit reloads the settings read.
   function setEmptyKeepDays(which, days) {
     var projects = which === "projects"
     var n = Number(days)
     if (isNaN(n) || n < 0 || n !== Math.floor(n)) {
       root.status = "must be a whole number of days, or 0 to keep forever"
       return
     }
     var current = projects ? root.emptyProjectsKeepDays : root.emptyContextsKeepDays
     if (n === current) return
     var key = projects ? "auto_remove_empty_projects" : "auto_remove_empty_contexts"
     root.run(["settings", "set", key, String(n)],
              n === 0 ? "keeping empty " + which + " forever"
                      : "keeping empty " + which + " " + n + " days, then removing them")
   }

    // How many days a completed task is kept. "0" keeps it forever. The value
    // is a switch in the database like the others, so it goes through
    // `settings set`; the command's exit reloads the settings read, which is
    // what moves the tick.
    function setDoneRetention(days) {
      var n = Number(days)
      if (isNaN(n) || n < 0 || n !== Math.floor(n)) {
        root.status = "retention must be a whole number of days, or 0"
        return
       }
      if (n === root.doneRetentionDays) return
      root.run(["settings", "set", "done_retention_days", String(n)],
               n === 0 ? "keeping completed tasks forever"
                       : "keeping completed " + n + " days")
     }

    function setArchiveRetention(days) {
      var n = Number(days)
      if (isNaN(n) || n < 0 || n !== Math.floor(n)) {
        root.status = "retention must be a whole number of days, or 0"
        return
       }
      if (n === root.archiveRetentionDays) return
      root.run(["settings", "set", "archive_retention_days", String(n)],
               n === 0 ? "keeping archived tasks forever"
                       : "keeping archived " + n + " days")
     }

  // A file:// URL from a picker -> a plain local path the CLI understands.
  function localPath(url) {
    var path = url.toString()
    if (path.indexOf("file://") === 0) path = path.slice(7)
    try { return decodeURIComponent(path) } catch (e) { return path }
  }

  function doImport() {
    var path = root.importPath.trim()
    if (path === "") { root.status = "give a file to import"; return }
    var args = ["import", path, "--format", "auto",
                "--tag", root.importTag.trim()]
    if (root.importDryRun) args = args.concat(["--dry-run"])
    root.run(args, root.importDryRun ? "dry run" : "imported")
  }

  function doExport(kind) {
    if (kind === "zip") {
      var target = root.exportZipPath.trim() || (root.home + "/omagtd-" + root.stamp + ".zip")
      root.exportZipPath = target
      root.run(["export", "--format", "zip", "-o", target], "zipped")
    } else {
      var xml = root.exportOfPath.trim() || (root.home + "/omagtd-" + root.stamp + ".ofxml")
      root.exportOfPath = xml
      root.run(["export", "--format", "ofxml", "-o", xml], "exported for OmniFocus")
    }
  }

  // ---- Keyboard -------------------------------------------------------------

  function step(delta) {
    if (root.column === 0) {
      root.stepLeft(delta)
      return
    }
    if (root.column === 1) {
      var j = root.rowSelected + delta
      while (j >= 0 && j < root.rows.length && root.rows[j].kind === "header") j += delta
      if (j < 0 || j >= root.rows.length) {
        // Up off the top of the list steps out onto the header row.
        if (delta < 0) root.focusTopHeader()
        return
      }
      root.rowSelected = j
      var list = (!root.projectFocused && root.view.key === "calendar") ? dayList : listView
      list.positionViewAtIndex(j, ListView.Contain)
      // The right column follows the cursor: whatever the middle column has
      // lit is what its editor shows, without a separate step into it.
      root.reloadDetail()
      return
    }
    // The right column: Up/Down move between the editor's fields.
    root.formStep(delta)
  }

  // The left column holds three things stacked: the nav menu, the switcher and
  // the tree. Up/Down walk from one to the next — off the menu into the header,
  // off the switcher into the tree and back — while Tab walks the buttons inside
  // the menu and the switcher.
  function stepLeft(delta) {
    if (root.leftSection === "menu") {
      if (delta < 0) root.focusTopHeader()
      else root.leftSection = "switcher"
      return
    }
    if (root.leftSection === "switcher") {
      if (delta < 0) { root.leftSection = "menu"; return }
      root.leftSection = "tree"
      root.syncTreeFocus()
      return
    }
    // In the tree, an Up that cannot move leaves for the switcher above it.
    if (!root.treeStep(delta) && delta < 0) root.leftSection = "switcher"
  }

  // One row of the showing tree; false when the cursor could not move, so the
  // caller knows it has reached the top (or bottom) of the list.
  function treeStep(delta) {
    if (root.leftMode === "contexts") {
      var c = root.contextSelected + delta
      if (c < 0 || c >= root.contextRows.length) return false
      root.contextSelected = c
      contextView.positionViewAtIndex(c, ListView.Contain)
      return true
    }
    var i = root.treeSelected + delta
    while (i >= 0 && i < root.treeRows.length && root.treeRows[i].kind === "header") i += delta
    if (i < 0 || i >= root.treeRows.length) return false
    root.treeSelected = i
    treeView.positionViewAtIndex(i, ListView.Contain)
    return true
  }

  function syncTreeFocus() {
    if (root.leftMode === "contexts") root.syncContextFocus()
    else root.syncProjectFocus()
  }

  // ---- The header row -------------------------------------------------------

  // Put the keyboard on the header row, on the capture box by default. It is a
  // field, so the key catcher steps aside and the box takes the typing; Down
  // (and Escape, then Down) leads back out into the columns.
  function focusTopHeader() {
    root.topFocused = true
    root.topIndex = 1
    Qt.callLater(function() { captureField.forceActiveFocus() })
  }

  // Left/Right walk the header's controls; Down leaves the header for the top of
  // the left column, so the header is never a dead end. Up has nowhere to go.
  function moveTop(dx, dy) {
    if (dy > 0) {
      root.topFocused = false
      root.column = 0
      root.leftSection = "menu"
      root.menuSelected = Math.max(0, root.primaryKeys.indexOf(root.view.key))
      // The capture box may hold the caret; hand the keyboard back so the
      // left column's keys are the left column's again.
      keyCatcher.forceActiveFocus()
      return
    }
    if (dy < 0) return
    var next = Math.max(0, Math.min(3, root.topIndex + dx))
    if (next === root.topIndex) return
    root.topIndex = next
    root.focusTopControl()
  }

  // Put the caret where topIndex points. captureField has an id here; the three
  // buttons are focused by their own ids in the header.
  function focusTopControl() {
    if (root.topIndex === 1) { captureField.forceActiveFocus(); return }
    var target = root.topIndex === 0 ? settingsButton
               : root.topIndex === 2 ? searchButton : downButton
    if (target) target.forceActiveFocus()
  }

  // ---- The editor's fields --------------------------------------------------

  // The editor's text fields, top to bottom, as the Up/Down cursor sees them.
  // Tab keeps moving inside a row (and completes the date and name fields); Up
  // and Down move a whole field at a time.
  readonly property var formFields: [
    titleField, projectField, tagsField,
    dueField, deferField, durationField, repeatGroup, repeatField,
    linkField, notesField
  ]

  function formStep(delta) {
    if (root.settingsOpen) return
    var fields = root.formFields
    var n = fields.length
    if (n === 0) return
    var current = -1
    for (var i = 0; i < n; i++) {
      if (fields[i] && fields[i].activeFocus) { current = i; break }
    }
    // Up off the first field, or off an unfocused pane, steps out to the header.
    if (current === 0 && delta < 0) { root.focusTopHeader(); return }
    var next = current < 0 ? (delta > 0 ? 0 : n - 1)
                           : Math.max(0, Math.min(n - 1, current + delta))
    var target = fields[next]
    if (!target) return
    target.forceActiveFocus()
    revealEditorField(target)
  }

  // Keep the field the Up/Down cursor just landed on on screen: scroll the
  // editor so the whole field sits within the viewport, with a little margin at
  // either edge. Field heights can change (the notes box grows as it is
  // written), so this runs after the layout has settled.
  function revealEditorField(item) {
    if (!item) return
    Qt.callLater(function() {
      if (!item.parent) return
      var y = item.mapToItem(detailColumn, 0, 0).y
      var margin = Style.spacing.md
      var top = detailFlick.contentY
      var bottom = top + detailFlick.height
      if (y - margin < top) {
        detailFlick.contentY = Math.max(0, y - margin)
      } else if (y + item.height + margin > bottom) {
        var maxY = Math.max(0, detailFlick.contentHeight - detailFlick.height)
        detailFlick.contentY = Math.min(maxY, y + item.height + margin - detailFlick.height)
      }
    })
  }

  function switchColumn(delta) {
    var next = Math.max(0, Math.min(2, root.column + delta))
    // Left from the middle column lands in the tree, the left column's home
    // base; the menu and switcher above it are reached with Up from there.
    if (delta < 0 && root.column === 1 && next === 0) root.leftSection = "tree"
    if (next === 2) {
      root.settingsOpen = false
      root.reloadDetail()
    }
    root.column = next
  }

  // Tab keeps its job of cycling the middle nav views everywhere except where
  // there is a row of its own to walk: the nav menu (its buttons) and the
  // projects/contexts switcher (which flips between the two trees).
  function tabStep(direction) {
    if (root.column === 0 && root.leftSection === "menu") {
      var n = root.primaryKeys.length
      if (n === 0) return
      root.menuSelected = ((root.menuSelected + direction) % n + n) % n
      return
    }
    if (root.column === 0 && root.leftSection === "switcher") {
      root.setLeftMode(root.leftMode === "projects" ? "contexts" : "projects")
      return
    }
    root.cycleView(direction)
  }

  // Return/Space: pick the highlighted menu view, work the header button under
  // the cursor, or save the editor when a field or a column below is in play.
  function activateCursor() {
    if (root.settingsOpen) {
      if (root.fullscreenMode) root.closeSettingsPane()
      else root.close()
      return
    }
    if (root.topFocused) {
      if (root.topIndex === 0) root.showSettings()
      else if (root.topIndex === 2) root.focusSearch()
      else if (root.topIndex === 3) root.captureAndEdit(root.captureValue)
      return
    }
    if (root.column === 2 && waitToggle.activeFocus) { root.toggleWait(); return }
    if (root.column === 0 && root.leftSection === "menu") {
      root.pickViewByKey(root.primaryKeys[Math.max(0, root.menuSelected)])
      return
    }
    // A context row in the middle column drills in, the same as clicking it.
    if (root.column === 1) {
      var row = root.selectedRow()
      if (row && row.kind === "tag") { root.focusContextRow(row); return }
    }
    root.saveEdits()
  }

  // Tab walks the buttons that are on screen, not the sixteen views behind
  // them, so the keyboard never lands somewhere the eye cannot follow.
  function cycleView(delta) {
    var keys = root.primaryKeys
    var n = keys.length
    if (n === 0) return
    var at = keys.indexOf(root.view ? root.view.key : "")
    if (at === -1) at = delta > 0 ? -1 : 0
    var next = ((at + delta) % n + n) % n
    root.pickViewByKey(keys[next])
  }

  function pickView(n) {
    if (n >= 1 && n <= root.views.length) {
      root.topFocused = false
      var wasFocused = root.projectFocused || root.contextFocused
      root.projectFocus = null
      root.contextFocus = null
      if (root.viewIndex === n - 1) {
        // Same view: onViewIndexChanged will not fire, so refresh by hand.
        if (wasFocused) root.reloadList()
      } else {
        root.viewIndex = n - 1
      }
      root.column = 1
    }
  }

  function pickViewByKey(key) {
    for (var i = 0; i < root.views.length; i++) {
      if (root.views[i].key === key) {
        root.pickView(i + 1)
        return true
      }
    }
    return false
  }

  // The left column only shows a handful of buttons; these map a view key to
  // its place in `root.views` and to the label that place carries.
  function viewIndexFor(key) {
    for (var i = 0; i < root.views.length; i++) if (root.views[i].key === key) return i
    return 0
  }

  function viewLabel(key) {
    return root.views[root.viewIndexFor(key)].label
  }

  // Jump to the search view and put the caret in its field.
  function focusSearch() {
    root.topFocused = false
    root.projectFocus = null
    root.contextFocus = null
    root.viewIndex = root.viewIndexFor("search")
    root.column = 1
    Qt.callLater(function() { searchField.forceActiveFocus() })
  }

  // Whether the stored attachment is a link (web URL / e-mail) rather than a
  // file. What the engine keeps as a local file is a path — absolute, or
  // relative to the store — and file:// is also a file, so only a remote scheme
  // (or an unsigned link) counts as a link.
  function attachmentIsLink() {
    var uri = String(root.editAttachments || "").trim()
    if (!uri || uri.indexOf("file://") === 0) return false
    return /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(uri) || uri.indexOf("mailto:") === 0
  }

  // The on-disk location an attachment points at: an absolute path, or a
  // store-relative path resolved against the store's folder. A link has no
  // location of its own.
  function attachmentLocation() {
    var uri = String(root.editAttachments || "").trim()
    if (!uri || root.attachmentIsLink()) return ""
    if (uri.indexOf("file://") === 0) {
      try { return decodeURIComponent(uri.slice(7)) } catch (e) { return uri.slice(7) }
    }
    if (uri.charAt(0) === "/") return uri
    if (!root.dbPath) return ""
    var slash = root.dbPath.lastIndexOf("/")
    if (slash < 0) return ""
    return root.dbPath.slice(0, slash + 1) + uri
  }

  // A file attachment is something you take from here, not something the app
  // opens for you, so its button copies its location to the clipboard.
  function copyAttachmentLocation() {
    var path = root.attachmentLocation()
    if (path === "") return
    Quickshell.clipboardText = path
    root.status = "copied " + path
  }

  // The open button is offered where a link actually lives: beside the link
  // field when it holds a URL, and on the attachment row when the stored
  // attachment is itself a link. Use xdg-open if Qt's portal URL handler is
  // unavailable.
  function openAttachment() {
    var att = String(root.editAttachments || "").trim()
    var uri = root.attachmentIsLink() ? att : String(root.editLink || "").trim()
    if (!uri) {
      root.showOsd("", "No link available for the task")
      return
    }
    var target = uri
    if (uri.indexOf("file://") === 0) {
      try { target = decodeURIComponent(uri.slice(7)) } catch (e) { target = uri.slice(7) }
    }
    root.showOsd("↗", "Opening link…")
    try { Quickshell.execDetached(["xdg-open", target]) } catch (e) {}
  }

  // OmniFocus import dialog - browse for export files
  FileDialog {
    id: importDialog
    title: "Import from OmniFocus"
    fileMode: FileDialog.OpenFile
    nameFilters: ["OmniFocus (*.ofxml *.ofocusmx *.json *.csv)", "All files (*)"]
    onAccepted: {
      var path = root.localPath(selectedFile)
      root.importPath = path
      root.doImport()
    }
  }

  // An OmniFocus export is a folder (a .ofocus document), so offer a folder
  // picker alongside the file browser above.
  FolderDialog {
    id: importFolderDialog
    title: "Choose an OmniFocus export folder"
    onAccepted: {
      root.importPath = root.localPath(selectedFolder)
      root.doImport()
    }
  }

  // One attachment per task, so choosing it is the whole editor: a path to
  // type in would only be a second way to get it wrong.
  FileDialog {
    id: attachDialog
    title: "Attach a file"
    fileMode: FileDialog.OpenFile
    onAccepted: {
      var path = selectedFile.toString()
      if (path.indexOf("file://") === 0) path = path.slice(7)
      try { path = decodeURIComponent(path) } catch (e) {}
      root.editAttachments = path
      root.formDirty = true
    }
  }

  // Simple date picker popup (functional fallback)
  Popup {
    id: dueDatePopup
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent
    x: parent.width / 2 - width / 2
    y: parent.height / 2 - height / 2
    width: Style.space(340)
    height: Style.space(420)
    background: Rectangle {
      color: gtdTheme.background
      radius: Style.cornerRadius
      border.color: root.line
      border.width: 1
    }
    Column {
      anchors.fill: parent
      anchors.margins: Style.spacing.md
      spacing: Style.spacing.sm
      Row {
        width: parent.width
        spacing: Style.spacing.xs
        Button {
          text: "‹"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: root.shiftPickerMonth(-1)
        }
        Text {
          text: root.pickerLabel
          color: root.textColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          anchors.verticalCenter: parent.verticalCenter
          width: parent.width - 80
          horizontalAlignment: Text.AlignHCenter
        }
        Button {
          text: "›"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: root.shiftPickerMonth(1)
        }
      }
      Row {
        width: parent.width
        spacing: Style.spacing.xs
        Text { text: "M"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; horizontalAlignment: Text.AlignHCenter; width: (parent.width - 6 * Style.spacing.xs) / 7 }
        Text { text: "T"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; horizontalAlignment: Text.AlignHCenter; width: (parent.width - 6 * Style.spacing.xs) / 7 }
        Text { text: "W"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; horizontalAlignment: Text.AlignHCenter; width: (parent.width - 6 * Style.spacing.xs) / 7 }
        Text { text: "T"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; horizontalAlignment: Text.AlignHCenter; width: (parent.width - 6 * Style.spacing.xs) / 7 }
        Text { text: "F"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; horizontalAlignment: Text.AlignHCenter; width: (parent.width - 6 * Style.spacing.xs) / 7 }
        Text { text: "S"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; horizontalAlignment: Text.AlignHCenter; width: (parent.width - 6 * Style.spacing.xs) / 7 }
        Text { text: "S"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; horizontalAlignment: Text.AlignHCenter; width: (parent.width - 6 * Style.spacing.xs) / 7 }
      }
      Grid {
        columns: 7
        rowSpacing: Style.spacing.xs
        columnSpacing: Style.spacing.xs
        width: parent.width
        Repeater {
          model: root.pickerWeeks
          delegate: Button {
            required property var modelData
            text: String(modelData.day)
            width: (parent.width - 6 * Style.spacing.xs) / 7
            height: Style.space(28)
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: modelData.isToday ? root.accent
              : (modelData.inMonth ? root.textColor : root.dim)
            onClicked: root.pickDate(modelData.date)
          }
        }
      }
      Row {
        width: parent.width
        spacing: Style.spacing.xs
        Text {
          text: "time"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          anchors.verticalCenter: parent.verticalCenter
        }
        TextField {
          id: pickerTimeField
          width: parent.width - pickerTimeClear.width - parent.spacing * 2 - Style.space(40)
          foreground: root.textColor
          placeholderText: "all day, or 09:30"
          text: root.pickerTime
          onTextEdited: root.pickerTime = text
        }
        Button {
          id: pickerTimeClear
          text: "clear"
          bordered: true
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          foreground: root.textColor
          onClicked: root.pickerTime = ""
        }
      }
      Row {
        spacing: Style.spacing.xs
        Button {
          text: "Today"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: {
            var d = new Date()
            var y = d.getFullYear()
            var m = String(d.getMonth() + 1).padStart(2, '0')
            var day = String(d.getDate()).padStart(2, '0')
            root.pickDate(y + '-' + m + '-' + day)
          }
        }
        Button {
          text: "Clear"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: {
            if (root.pickerTarget === "due") root.editDue = ""
            else root.editDefer = ""
            root.formDirty = true
            dueDatePopup.close()
          }
        }
        Button {
          text: "Close"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: dueDatePopup.close()
        }
      }
    }
  }

  PanelKeyCatcher {
    id: keyCatcher
    anchors.fill: parent
    blocked: captureField.activeFocus || projectField.activeFocus || tagsField.activeFocus
      || titleField.activeFocus || dueField.activeFocus
      || deferField.activeFocus || durationField.activeFocus || notesField.activeFocus
      || repeatGroup.activeFocus || repeatField.activeFocus
      || linkField.activeFocus
      || searchField.activeFocus || projectNameField.activeFocus
      || importField.activeFocus || ofField.activeFocus || zipField.activeFocus
      || root.confirmCloseOpen
    onCloseRequested: root.handleCloseRequested()
    onTabRequested: function(direction) { root.tabStep(direction) }
    onMoveRequested: function(dx, dy) {
      if (root.topFocused) { root.moveTop(dx, dy); return }
      if (dx !== 0) {
        if (root.foldStep(dx)) return
        root.switchColumn(dx)
      } else root.step(dy)
    }
    onActivateRequested: root.activateCursor()
    onDeleteRequested: root.act("trash")
    onTextKey: function(t) {
      if (t === "c" || t === "C" || t === "/") root.captureFieldFocus()
      else if (t === "d" || t === "D") root.act("done")
      else if (t === "f" || t === "F") root.toggleFlag()
      else if (t === "w" || t === "W") root.toggleWait()
      else if (t === "i" || t === "I") root.act("inbox")
      else if (t === "x" || t === "X") root.act("drop")
      else if (t === "s" || t === "S") root.showSettings()
      else if (t === "r" || t === "R") root.reload()
      else if (t === "t" || t === "T") root.act("start")
      else if (t === "p" || t === "P") root.act("postpone")
      else if (t === "o" || t === "O") root.act("open")
      else if (t === "z" || t === "Z") root.act("undo")
      else if (t === "u" || t === "U") root.act("undone")
      else if (t === "a" || t === "A") {
        var at = root.selectedItem()
        if (at && root.isArchived(at)) root.act("unarchive")
        else root.act("archive")
      }
      else if (t === "m" || t === "M") root.showSettings()
      else if (t >= "1" && t <= "9") root.pickView(parseInt(t, 10))
    }
  }

  function toggleFlag() {
    var target = root.selectedItem()
    if (!target) return
    var raw = target.raw || {}
    var priority = raw.priority || 0
    root.act(priority > 0 ? "unflag" : "flag")
  }

  // The waiting switch, flipped: an on/off decision with no name to record,
  // so `engage wait` parks it and `engage resume` brings it back.
  function toggleWait() {
    var target = root.selectedItem()
    if (!target) return
    var raw = target.raw || {}
    root.act(raw.status === "waiting" ? "resume" : "wait")
  }

  function captureFieldFocus() {
    root.topFocused = true
    root.topIndex = 1
    captureField.forceActiveFocus()
    captureField.selectAll()
  }

  function showSettings() {
    root.topFocused = false
    root.settingsOpen = true
    root.column = 2
  }

  // The item pane and the settings pane are two faces of one flickable, so a
  // pane that opens keeps whatever depth the other was read to. Settings then
  // starts part way down its own list and the sections above look as though
  // they were never there. Every pane starts at its own top.
  onSettingsOpenChanged: detailFlick.contentY = 0

  // ---- Polling ---------------------------------------------------------------

  property int pollSeconds: {
    var n = root.settings && typeof root.settings.pollIntervalSec === "number"
        ? root.settings.pollIntervalSec : 60
    return Math.max(10, Math.min(900, Math.round(n)))
  }

  Timer {
    interval: root.pollSeconds * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (root.opened) root.reload()
  }

  // Debounce the search field so a keystroke does not run `gtd search` twice.
  Timer {
    id: searchTimer
    interval: 220
    repeat: false
    onTriggered: if (root.opened && root.view.key === "search") root.reloadList()
  }

  // The status line is a passing note about the last action. A few seconds
  // after an action finishes, it clears so the shortcut legend returns to the
  // footer; each new action restarts the countdown.
  Timer {
    id: statusTimer
    interval: 5000
    repeat: false
    onTriggered: root.status = ""
  }

  // ---- Rendering ------------------------------------------------------------

  // If the window is too narrow to show the full layout, drop to a compact
  // message asking to enlarge the window. The threshold is the
  // fullscreen_width_limit setting, 768 by default.
  readonly property bool tooNarrow: width < root.fullscreenWidthLimit

  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.NoButton
    onWheel: function(wheel) { }
    visible: root.tooNarrow
  }

  Text {
    anchors.centerIn: parent
    textFormat: Text.PlainText
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
    text: " OmaGTD — Please enlarge the window to show its content (min "
          + root.fullscreenWidthLimit + "px width)"
    color: root.dim
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    visible: root.tooNarrow
    wrapMode: Text.WordWrap
  }

  // ---- Rendering ------------------------------------------------------------

  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.NoButton
    onWheel: function(wheel) { }
    visible: !root.tooNarrow
  }

  Column {
    id: page
    anchors.fill: parent
    anchors.margins: Style.spacing.xxl
    spacing: Style.spacing.md
    visible: !root.tooNarrow

    // ---- Header
    Item {
      id: header
      width: page.width
      height: Math.max(Style.space(56), captureField.implicitHeight + Style.spacing.lg * 2)

      PanelHero {
        id: hero
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(300)
        title: "OmaGTD"
        meta: root.treeData && root.treeData.counts
          ? (root.treeData.counts.open_items + " open  ·  " + root.treeData.counts.inbox + " inbox")
          : ""
        foreground: root.textColor
        fontFamily: root.fontFamily
        iconComponent: Component {
          Image {
            source: root.appIcon
            sourceSize.width: Style.space(44)
            sourceSize.height: Style.space(44)
            fillMode: Image.PreserveAspectFit
          }
        }
      }

      // The settings button sits first on the header row, ahead of the capture
      // box and the search/down buttons, so Left/Right walk the bar left to
      // right: settings, capture, search, down.
      Button {
        id: settingsButton
        anchors.left: hero.right
        anchors.leftMargin: Style.spacing.sm
        anchors.verticalCenter: parent.verticalCenter
        text: "settings"
        bordered: true
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.textColor
        onClicked: root.showSettings()
      }

      TextField {
        id: captureField
        anchors.left: settingsButton.right
        anchors.leftMargin: Style.spacing.sm
        anchors.right: controls.left
        anchors.rightMargin: Style.spacing.sm
        anchors.verticalCenter: parent.verticalCenter
        foreground: root.textColor
        placeholderText: "Capture…  #context /Project/ <defer >due ~repeat *30m"
        text: root.captureValue
        // Clicking or tabbing into the box is the same as arriving from the
        // columns with Up: the header owns the keyboard from here.
        onActiveFocusChanged: if (activeFocus) { root.topFocused = true; root.topIndex = 1 }
        onTextEdited: root.captureValue = text
        Keys.onTabPressed: function(event) {
          root.completeCaptureField()
          event.accepted = true
        }
        onAccepted: root.capture(text)
        // Down leaves the header for the top of the left column (the capture
        // itself is filed by the down button beside it, or by Return).
        Keys.onDownPressed: function(event) {
          root.moveTop(0, 1)
          event.accepted = true
        }
        Keys.onEscapePressed: function(event) { root.captureValue = ""; keyCatcher.forceActiveFocus(); event.accepted = true }
        // With the box empty the arrows have no text to walk, so they walk the
        // header's controls instead; with text they move the caret as usual.
        Keys.onLeftPressed: function(event) {
          if (captureField.text !== "") return
          root.moveTop(-1, 0)
          event.accepted = true
        }
        Keys.onRightPressed: function(event) {
          if (captureField.text !== "") return
          root.moveTop(1, 0)
          event.accepted = true
        }
      }

      Row {
        id: controls
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.spacing.md

        Button {
          id: searchButton
          text: "search"
          bordered: true
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          foreground: root.textColor
          onClicked: root.focusSearch()
        }

        Button {
          id: downButton
          text: "↓"
          bordered: true
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          foreground: root.textColor
          onClicked: root.captureAndEdit(root.captureValue)
        }
      }
    }

    // ---- The three columns
    Row {
      width: page.width
      height: page.height - header.height - footer.height - page.spacing * 2
      spacing: Style.spacing.lg

      // Left: projects and their hierarchy.
      Item {
        id: leftColumn
        width: Style.space(320)
        height: parent.height

        Column {
          id: leftHeader
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          // The only gap this column carries is above the projects/contexts
          // switcher, so a little more air there reads as a deliberate break.
          spacing: Style.spacing.md

          Column {
            id: primaryButtons
            width: parent.width
            spacing: Style.spacing.xs

            Repeater {
              model: root.primaryViews
              delegate: Row {
                id: buttonRow
                required property var modelData
                readonly property int buttonCount: modelData.length
                width: primaryButtons.width
                spacing: Style.spacing.xs
                Repeater {
                  model: buttonRow.modelData
                  delegate: Button {
                    required property string modelData
                    width: buttonRow.buttonCount > 0
                      ? (buttonRow.width - buttonRow.spacing * (buttonRow.buttonCount - 1))
                        / buttonRow.buttonCount
                      : 0
                    text: root.viewLabel(modelData)
                    bordered: true
                    // While the cursor is in the menu, Tab's highlight is what
                    // is lit; otherwise the button of the showing view is.
                    selected: root.column === 0 && root.leftSection === "menu"
                      && !root.topFocused
                      ? root.primaryKeys[root.menuSelected] === modelData
                      : (!root.projectFocused && !root.contextFocused
                         && root.view && root.view.key === modelData)
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    foreground: root.textColor
                    onClicked: root.pickView(root.viewIndexFor(modelData) + 1)
                  }
                }
              }
            }
          }

          // Which tree the left column is showing. Choosing one swaps the list
          // below it; choosing a row in that list is what fills the middle
          // column. The middle nav views above stay available either way.
          Row {
            id: leftModeRow
            width: parent.width
            spacing: Style.spacing.xs

            Repeater {
              model: [["projects", "Projects"], ["contexts", "Contexts"]]
              delegate: Button {
                required property var modelData
                width: (leftModeRow.width - leftModeRow.spacing) / 2
                text: modelData[1]
                bordered: true
                selected: root.leftMode === modelData[0]
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.textColor
                onClicked: root.setLeftMode(modelData[0])
              }
            }
          }
        }

        PanelSeparator {
          id: leftSeparator
          anchors.top: leftHeader.bottom
          anchors.topMargin: Style.spacing.lg
          anchors.left: parent.left
          anchors.right: parent.right
          foreground: root.textColor
        }

        ListView {
          id: treeView
          anchors.top: leftSeparator.bottom
          anchors.topMargin: Style.spacing.sm
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          anchors.bottomMargin: Style.spacing.sm
          model: root.treeRows
          clip: true
          spacing: 1
          boundsBehavior: Flickable.StopAtBounds
          visible: root.leftMode === "projects"

          delegate: Item {
            id: treeRow
            required property var modelData
            required property int index
            width: treeView.width
            height: treeRowTitle.implicitHeight + Style.spacing.sm

            Rectangle {
              anchors.fill: parent
              radius: Style.cornerRadius
              color: root.treeRowActive(treeRow)
                ? Style.selectedFillFor(root.textColor, root.accent) : "transparent"
            }

            // A project row carries `late/total`; the late number turns urgent.
            Text {
              id: treeRowCount
              visible: treeRow.modelData.kind === "project"
              anchors.right: parent.right
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: treeRow.modelData.kind === "project"
                ? (treeRow.modelData.late || 0) + "/" + (treeRow.modelData.total || 0) : ""
              color: treeRow.modelData.late > 0 ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: treeRow.modelData.late > 0
            }

            Text {
              id: treeRowTitle
              anchors.left: parent.left
              anchors.leftMargin: Style.space(8) + (treeRow.modelData.depth || 0) * Style.space(14)
              anchors.right: treeRowCount.visible ? treeRowCount.left : parent.right
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              elide: Text.ElideRight
              color: treeRow.modelData.kind === "action" && treeRow.modelData.late
                ? root.urgent
                : root.treeRowActive(treeRow) ? root.textColor : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: treeRow.modelData.kind === "area"
              text: (treeRow.modelData.kind === "project"
                       ? (treeRow.modelData.collapsed ? "▸ " : "▾ ")
                     : treeRow.modelData.kind === "header" ? "" : "· ")
                + treeRow.modelData.title
                + (treeRow.modelData.kind !== "project" && treeRow.modelData.meta
                   ? "   " + treeRow.modelData.meta : "")
            }

             MouseArea {
               anchors.fill: parent
               onClicked: function(mouse) {
                 if (treeRow.modelData.kind === "project"
                      && mouse.x < Style.space(24)
                         + (treeRow.modelData.depth || 0) * Style.space(14))
                   root.toggleProject(treeRow.modelData.id)
                 else {
                   root.topFocused = false
                   root.column = 0
                   root.leftSection = "tree"
                   root.treeSelected = treeRow.index
                   }
                 }
               onDoubleClicked: if (treeRow.modelData.kind === "project")
                 root.toggleProject(treeRow.modelData.id)
                }

             DropArea {
               anchors.fill: parent
               onDropped: function(drop) {
                 if (treeRow.modelData.kind === "project")
                   root.dropOnProject(treeRow.modelData, drop)
                 else
                   root.dropOnInbox(drop)
                  }
                }
              }
        }

        // The context tree. Same job as the list above it: pick a context and
        // the middle column fills with everything filed under it.
        ListView {
          id: contextView
          anchors.top: leftSeparator.bottom
          anchors.topMargin: Style.spacing.sm
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          anchors.bottomMargin: Style.spacing.sm
          model: root.contextRows
          clip: true
          spacing: 1
          boundsBehavior: Flickable.StopAtBounds
          visible: root.leftMode === "contexts"

          delegate: Item {
            id: contextRow
            required property var modelData
            required property int index
            width: contextView.width
            height: contextTitle.implicitHeight + Style.spacing.sm

            Rectangle {
              anchors.fill: parent
              radius: Style.cornerRadius
              color: root.contextRowActive(contextRow)
                ? Style.selectedFillFor(root.textColor, root.accent) : "transparent"
            }

            // How many open actions the context holds.
            Text {
              id: contextRowCount
              anchors.right: parent.right
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: (contextRow.modelData.open_count || 0) + ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Text {
              id: contextTitle
              anchors.left: parent.left
              anchors.leftMargin: Style.space(8) + (contextRow.modelData.depth || 0) * Style.space(14)
              anchors.right: contextRowCount.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              elide: Text.ElideRight
              color: root.contextRowActive(contextRow) ? root.textColor : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: (contextRow.modelData.depth || 0) === 0
              text: contextRow.modelData.title
            }

            MouseArea {
              anchors.fill: parent
               onClicked: {
                 root.topFocused = false
                 root.column = 0
                 root.leftSection = "tree"
                 root.contextSelected = contextRow.index
                }
             }

            DropArea {
              anchors.fill: parent
              onDropped: function(drop) {
                root.dropOnContext(contextRow.modelData, drop)
                }
              }
            }
        }
      }

      PanelSeparator {
        width: 1
        height: parent.height
        color: root.line
        foreground: root.textColor
      }

      // Middle: the list for the chosen step, or the calendar.
      Item {
        id: middleColumn
        width: parent.width - leftColumn.width - rightColumn.width - Style.spacing.lg * 4 - 2
        height: parent.height

        Column {
          id: listHeader
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.xs
          visible: root.projectFocused || root.contextFocused || root.view.key !== "calendar"

          Item {
            width: parent.width
            height: Style.space(24)

            Text {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: root.projectFocused
                ? (root.projectFocus && root.projectFocus.title ? root.projectFocus.title : "Project")
                : root.contextFocused
                  ? "#" + (root.contextFocus.name || "")
                  : root.view.label
              color: root.textColor
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }

            Text {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: root.rowCount() + (root.rowCount() === 1 ? " task" : " tasks")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          TextField {
            id: searchField
            width: parent.width
            visible: !root.projectFocused && !root.contextFocused && root.view.key === "search"
            foreground: root.textColor
            onActiveFocusChanged: if (activeFocus) root.topFocused = false
            placeholderText: "Search titles, notes, projects, contexts, attachments…"
            text: root.searchQuery
            onTextChanged: {
              root.searchQuery = text
              searchTimer.restart()
            }
            // Down leaves the field for the results, landing on the first one.
            Keys.onDownPressed: function(event) {
              var i = 0
              while (i < root.rows.length && root.rows[i].kind === "header") i++
              if (i < root.rows.length) {
                root.selectRow(i)
                listView.positionViewAtIndex(i, ListView.Beginning)
                keyCatcher.forceActiveFocus()
              }
              event.accepted = true
            }
            Keys.onEscapePressed: function(event) {
              keyCatcher.forceActiveFocus()
              event.accepted = true
            }
          }
        }

        PanelSeparator {
          id: listSeparator
          anchors.top: listHeader.bottom
          anchors.topMargin: Style.spacing.lg
          anchors.left: parent.left
          anchors.right: parent.right
          foreground: root.textColor
          visible: root.projectFocused || root.contextFocused || root.view.key !== "calendar"
        }

        ListView {
          id: listView
          anchors.top: listSeparator.bottom
          anchors.topMargin: Style.spacing.sm
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: projectAdd.visible ? projectAdd.top : parent.bottom
          anchors.bottomMargin: projectAdd.visible ? Style.spacing.sm : 0
          model: root.rows
          clip: true
          spacing: 1
          boundsBehavior: Flickable.StopAtBounds
          visible: root.projectFocused || root.contextFocused || root.view.key !== "calendar"

          delegate: Item {
            id: row
            required property var modelData
            required property int index
            width: listView.width
            height: Math.max(Style.space(22), rowText.implicitHeight + Style.spacing.sm)

            Rectangle {
              anchors.fill: parent
              radius: Style.cornerRadius
              color: row.index === root.rowSelected && root.column === 1
                ? Style.selectedFillFor(root.textColor, root.accent) : "transparent"
            }

            // <glyph>  <title> on the left, the date stamp right-aligned.
            Text {
              id: rowText
              anchors.left: parent.left
              anchors.leftMargin: Style.space(8) + (row.modelData.depth || 0) * Style.space(14)
              anchors.right: rowMeta.visible ? rowMeta.left : parent.right
              anchors.rightMargin: Style.spacing.md
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              elide: Text.ElideRight
              color: gtdTheme.colorFor(row.modelData.raw, row.modelData.late,
                row.index === root.rowSelected && root.column === 1
                  ? root.textColor
                  : (row.modelData.dead ? root.dim : root.textColor),
                row.modelData.deferred === true)
              font.family: root.fontFamily
              font.pixelSize: row.modelData.kind === "header" ? Style.font.caption : Style.font.body
              font.bold: row.modelData.kind === "header"
              font.strikeout: row.modelData.archived === true
              font.italic: row.modelData.deferred === true
              text: (row.modelData.kind === "header" ? "" : (row.modelData.glyph || " ") + "  ")
                + row.modelData.title
            }

            Text {
              id: rowMeta
              anchors.right: parent.right
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              visible: !!row.modelData.meta
              text: row.modelData.meta
              color: row.modelData.late ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

             MouseArea {
                anchors.fill: parent
                onClicked: root.selectRow(row.index)
                onDoubleClicked: {
                  if (row.modelData.id) {
                    root.column = 2
                    root.settingsOpen = false
                    root.reloadDetail()
                    }
                  }
                }
              }
          }

        // The projects view ends with a way to start a new project. A project
        // is a container, so it goes through `gtd project add` rather than the
        // capture flow, which would only file an inbox task.
        Row {
          id: projectAdd
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          spacing: Style.spacing.xs
          visible: root.leftMode === "projects" && !root.projectFocused
              && !root.contextFocused && root.view.key === "projects"

          TextField {
            id: projectNameField
            width: parent.width - projectAddButton.width - parent.spacing
            foreground: root.textColor
            placeholderText: "New project name… (parent>child nests)"
            onAccepted: root.addProject(projectNameField.text)
            Keys.onEscapePressed: function(event) {
              projectNameField.text = ""
              keyCatcher.forceActiveFocus()
              event.accepted = true
            }
          }

          Button {
            id: projectAddButton
            text: "add project"
            bordered: true
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.textColor
            onClicked: root.addProject(projectNameField.text)
          }
        }

        // A real month grid, Monday first, plus the selected day's commitments.
        Item {
          id: calendarPage
          anchors.fill: parent
          visible: !root.projectFocused && root.view.key === "calendar"

          Item {
            id: calNav
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            height: Style.space(28)

            Text {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: root.monthLabel
              color: root.textColor
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }

            Row {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.xs

              Button {
                text: "‹"
                bordered: true
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.textColor
                onClicked: root.shiftMonth(-1)
              }
              Button {
                text: "today"
                bordered: true
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.textColor
                onClicked: root.goToday()
              }
              Button {
                text: "›"
                bordered: true
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.textColor
                onClicked: root.shiftMonth(1)
              }
            }
          }

          Row {
            id: calWeekdays
            anchors.top: calNav.bottom
            anchors.topMargin: Style.spacing.xs
            anchors.left: parent.left
            anchors.right: parent.right
            spacing: 1

            Repeater {
              model: ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
              delegate: Text {
                required property var modelData
                width: (calWeekdays.width - 6) / 7
                textFormat: Text.PlainText
                horizontalAlignment: Text.AlignHCenter
                text: modelData
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          Column {
            id: calGrid
            anchors.top: calWeekdays.bottom
            anchors.topMargin: Style.spacing.xs
            anchors.left: parent.left
            anchors.right: parent.right
            spacing: 1
            property real cellWidth: (width - 6) / 7
            property real cellHeight: Style.space(44)

            Repeater {
              model: root.calendarWeeks
              delegate: Row {
                id: calWeek
                required property var modelData
                width: calGrid.width
                spacing: 1

                Repeater {
                  model: calWeek.modelData
                  delegate: Item {
                    id: calCell
                    required property var modelData
                    width: calGrid.cellWidth
                    height: calGrid.cellHeight

                    Rectangle {
                      anchors.fill: parent
                      anchors.margins: 1
                      radius: Style.cornerRadius
                      color: calCell.modelData.date === root.calendarSelectedDate
                        ? Style.selectedFillFor(root.textColor, root.accent)
                        : (calCell.modelData.inMonth ? "transparent"
                           : Qt.alpha(root.textColor, 0.04))
                      border.width: calCell.modelData.isToday ? 1 : 0
                      border.color: root.accent
                    }

                    Text {
                      anchors.top: parent.top
                      anchors.topMargin: Style.space(3)
                      anchors.horizontalCenter: parent.horizontalCenter
                      textFormat: Text.PlainText
                      text: calCell.modelData.day
                      color: !calCell.modelData.inMonth ? root.dim
                        : calCell.modelData.late ? root.urgent
                        : root.textColor
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: calCell.modelData.isToday
                    }

                    Row {
                      anchors.bottom: parent.bottom
                      anchors.bottomMargin: Style.space(4)
                      anchors.horizontalCenter: parent.horizontalCenter
                      spacing: 3
                      visible: calCell.modelData.sym > 0

                      Repeater {
                        model: [
                          { n: calCell.modelData.marks.red, c: root.urgent },
                          { n: calCell.modelData.marks.orange, c: gtdTheme.orange },
                          { n: calCell.modelData.marks.blue, c: gtdTheme.blue },
                          { n: calCell.modelData.marks.purple, c: gtdTheme.purple },
                          { n: calCell.modelData.marks.grey, c: gtdTheme.grey },
                          { n: calCell.modelData.marks.green, c: gtdTheme.green,
                            shown: root.showCompleted }
                        ]
                        delegate: Rectangle {
                          required property var modelData
                          width: 4
                          height: 4
                          radius: 2
                          visible: modelData.n > 0 && modelData.shown !== false
                          color: modelData.c
                        }
                      }
                    }

                     MouseArea {
                       anchors.fill: parent
                       onClicked: root.selectDay(calCell.modelData.date)
                      }

                     DropArea {
                       anchors.fill: parent
                       onDropped: function(drop) {
                         root.dropOnDay(calCell.modelData.date, drop)
                         }
                       }
                    }
                }
              }
            }
          }

          PanelSeparator {
            id: calSeparator
            anchors.top: calGrid.bottom
            anchors.topMargin: Style.spacing.md
            anchors.left: parent.left
            anchors.right: parent.right
            foreground: root.textColor
          }

          Text {
            id: calDayLabel
            anchors.top: calSeparator.bottom
            anchors.topMargin: Style.spacing.sm
            anchors.left: parent.left
            anchors.right: parent.right
            textFormat: Text.PlainText
            elide: Text.ElideRight
            text: root.calendarSelectedDate
              ? (root.calendarSelectedDate + "  ·  " + root.rows.length
                 + (root.rows.length === 1 ? " item" : " items"))
              : "no day selected"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          ListView {
            id: dayList
            anchors.top: calDayLabel.bottom
            anchors.topMargin: Style.spacing.xs
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            model: root.rows
            clip: true
            spacing: 1
            boundsBehavior: Flickable.StopAtBounds

            delegate: Item {
              id: dayRow
              required property var modelData
              required property int index
              width: dayList.width
              height: Math.max(Style.space(22), dayRowText.implicitHeight + Style.spacing.sm)

              Rectangle {
                anchors.fill: parent
                radius: Style.cornerRadius
                color: dayRow.index === root.rowSelected && root.column === 1
                  ? Style.selectedFillFor(root.textColor, root.accent) : "transparent"
              }

              Text {
                id: dayRowText
                anchors.left: parent.left
                anchors.leftMargin: Style.space(8)
                anchors.right: dayRowMeta.visible ? dayRowMeta.left : parent.right
                anchors.rightMargin: Style.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                elide: Text.ElideRight
                color: gtdTheme.colorFor(dayRow.modelData.raw, dayRow.modelData.late,
                  dayRow.index === root.rowSelected && root.column === 1
                    ? root.textColor
                    : (dayRow.modelData.dead ? root.dim : root.textColor),
                  dayRow.modelData.deferred === true)
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.italic: dayRow.modelData.deferred === true
                text: (dayRow.modelData.glyph || " ") + "  " + dayRow.modelData.title
              }

              Text {
                id: dayRowMeta
                anchors.right: parent.right
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                visible: !!dayRow.modelData.meta
                text: dayRow.modelData.meta
                color: dayRow.modelData.late ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              MouseArea {
                anchors.fill: parent
                onClicked: root.selectRow(dayRow.index)
                onDoubleClicked: if (dayRow.modelData.id) root.showItem(dayRow.modelData.id)
              }
            }
          }
        }
      }

      PanelSeparator {
        width: 1
        height: parent.height
        color: root.line
        foreground: root.textColor
      }

      // Right: the selected thing, or the settings.
      Item {
        id: rightColumn
        width: Style.space(360)
        height: parent.height

        Flickable {
          id: detailFlick
          anchors.fill: parent
          contentWidth: width
          contentHeight: detailColumn.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds

          Column {
            id: detailColumn
            width: detailFlick.width
            spacing: Style.spacing.md

            // ---- Settings pane
            Column {
              width: parent.width
              spacing: Style.spacing.md
              visible: root.settingsOpen

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: "Settings"
                color: root.textColor
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "The database is the whole system. These move it in and out; the only exception is the bar pill at the top, which is display only."
              }

              PanelSectionHeader {
                width: parent.width
                text: "Theme"
                foreground: root.dim
                fontFamily: root.fontFamily
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "The fullscreen app paints itself in its own theme here, "
                      + "apart from the bar pill and the panel, which always "
                      + "follow the desktop. \"Follow Omarchy\" keeps the desktop "
                      + "palette; the two OF-like palettes pin the app to an "
                      + "OmniFocus-inspired light or dark look."
              }

              Flow {
                width: parent.width
                spacing: Style.spacing.xs

                Button {
                  text: (root.appTheme === "omarchy" ? "✓ " : "") + "Follow Omarchy"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: root.chooseTheme("omarchy")
                }

                Button {
                  text: (root.appTheme === "of-light" ? "✓ " : "") + "OF-like Light"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: root.chooseTheme("of-light")
                }

                Button {
                  text: (root.appTheme === "of-dark" ? "✓ " : "") + "OF-like Dark"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: root.chooseTheme("of-dark")
                }
              }

              PanelSectionHeader {
                width: parent.width
                text: "Window"
                foreground: root.dim
                fontFamily: root.fontFamily
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "Below this width the full-screen app drops to a message "
                      + "asking to enlarge the window, rather than drawing a "
                      + "cramped layout. A whole number of pixels."
              }

              Row {
                width: parent.width
                spacing: Style.spacing.xs

                TextField {
                  id: widthLimitField
                  width: 80
                  foreground: root.textColor
                  placeholderText: "px"
                  text: String(root.fullscreenWidthLimit)
                  onAccepted: root.setFullscreenWidthLimit(text)
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  text: "px (768 by default)"
                }
              }

              PanelSectionHeader {
                width: parent.width
                text: "Bar pill (hideZeroCounts)"
                foreground: root.dim
                fontFamily: root.fontFamily
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "With nothing in flight the chip reads "
                      + (root.hideZeroCounts ? "\"GTD\"" : "\"GTD 0/0\"")
                      + ". This only changes that empty case; the counts are always there when there is any."
              }

              Flow {
                width: parent.width
                spacing: Style.spacing.xs

                Button {
                  text: (root.hideZeroCounts ? "✓ " : "") + "just GTD"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  onClicked: root.setHideZeroCounts(true)
                }

                Button {
                  text: (root.hideZeroCounts ? "" : "✓ ") + "GTD 0/0"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  onClicked: root.setHideZeroCounts(false)
                }
              }

              PanelSectionHeader {
                width: parent.width
                text: "Deadlines"
                foreground: root.dim
                fontFamily: root.fontFamily
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "A due date with no time lands on this clock."
              }

              TextField {
                id: dueTimeField
                width: parent.width
                foreground: root.textColor
                placeholderText: "18:00"
                text: root.dueTime
                onAccepted: root.saveDueTime(text)
              }

              Flow {
                width: parent.width
                spacing: Style.spacing.xs

                Button {
                  text: "set the time"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: root.saveDueTime(dueTimeField.text)
                }
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "A defer date with no time lands on this clock instead."
              }

              TextField {
                id: deferTimeField
                width: parent.width
                foreground: root.textColor
                placeholderText: "09:00"
                text: root.deferTime
                onAccepted: root.saveDeferTime(text)
              }

              Flow {
                width: parent.width
                spacing: Style.spacing.xs

                Button {
                  text: "set the time"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: root.saveDeferTime(deferTimeField.text)
                }
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "Clock times are shown on a " + root.timeFormat + " clock."
              }

              Flow {
                width: parent.width
                spacing: Style.spacing.xs

                Button {
                  text: (root.timeFormat === "24h" ? "✓ " : "") + "24h"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: root.chooseTimeFormat("24h")
                }

                Button {
                  text: (root.timeFormat === "12h" ? "✓ " : "") + "12h"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: root.chooseTimeFormat("12h")
                }
              }

              PanelSectionHeader {
                width: parent.width
                text: "Empty containers"
                foreground: root.dim
                fontFamily: root.fontFamily
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "Each is kept this many days after it empties, then "
                       + "removed. 0 keeps it forever. A project is often one "
                       + "you have just started, so the default is generous."
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "A project with nothing filed under it:"
              }

              Row {
                width: parent.width
                spacing: Style.spacing.xs

                TextField {
                  id: emptyProjectsField
                  width: 80
                  foreground: root.textColor
                  placeholderText: "days"
                  text: root.emptyProjectsKeepDays > 0
                             ? String(root.emptyProjectsKeepDays) : ""
                  onAccepted: root.setEmptyKeepDays("projects", text)
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  text: "days (0 = forever)"
                }
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "A context with nothing on it:"
              }

              Row {
                width: parent.width
                spacing: Style.spacing.xs

                TextField {
                  id: emptyContextsField
                  width: 80
                  foreground: root.textColor
                  placeholderText: "days"
                  text: root.emptyContextsKeepDays > 0
                             ? String(root.emptyContextsKeepDays) : ""
                  onAccepted: root.setEmptyKeepDays("contexts", text)
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  text: "days (0 = forever)"
                }
              }

                PanelSectionHeader {
                  width: parent.width
                  text: "Completed tasks kept"
                  foreground: root.dim
                  fontFamily: root.fontFamily
                  }

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  wrapMode: Text.WordWrap
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  text: "A finished task stays this many days, then clears. "
                         + "0 keeps it forever."
                  }

                Row {
                  width: parent.width
                  spacing: Style.spacing.xs

                  TextField {
                    id: retentionField
                    width: 80
                    foreground: root.textColor
                    placeholderText: "days"
                    text: root.doneRetentionDays > 0 ? String(root.doneRetentionDays) : ""
                    onAccepted: root.setDoneRetention(text)
                  }

                  Text {
                    anchors.verticalCenter: parent.verticalCenter
                    textFormat: Text.PlainText
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "days (0 = forever)"
                  }
                }

                PanelSectionHeader {
                  width: parent.width
                  text: "Archived tasks kept"
                  foreground: root.dim
                  fontFamily: root.fontFamily
                  }

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  wrapMode: Text.WordWrap
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  text: "An archived task stays this many days, then clears. "
                         + "0 keeps it forever."
                  }

                Row {
                  width: parent.width
                  spacing: Style.spacing.xs

                  TextField {
                    id: archiveRetentionField
                    width: 80
                    foreground: root.textColor
                    placeholderText: "days"
                    text: root.archiveRetentionDays > 0 ? String(root.archiveRetentionDays) : ""
                    onAccepted: root.setArchiveRetention(text)
                  }

                  Text {
                    anchors.verticalCenter: parent.verticalCenter
                    textFormat: Text.PlainText
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "days (0 = forever)"
                  }
                }

               PanelSectionHeader {
                 width: parent.width
                 text: "Calendar"
                 foreground: root.dim
                 fontFamily: root.fontFamily
                 }

               Text {
                 width: parent.width
                 textFormat: Text.PlainText
                 wrapMode: Text.WordWrap
                 color: root.dim
                 font.family: root.fontFamily
                 font.pixelSize: Style.font.caption
                 text: "Its days carry a coloured mark per matching state. "
                        + "Red is expired, orange is flagged, blue is expired "
                        + "waiting, purple is expired waiting and flagged, "
                        + "grey is the plain backlog. Finished tasks stay off "
                        + "the grid unless you ask for them."
                 }

               Flow {
                 width: parent.width
                 spacing: Style.spacing.xs

                 Button {
                   text: (root.showCompleted ? "✓ " : "") + "show completed"
                   bordered: true
                   fontFamily: root.fontFamily
                   fontSize: Style.font.caption
                   foreground: root.textColor
                   enabled: !root.busy
                   onClicked: root.setShowCompleted(true)
                  }

                 Button {
                   text: (root.showCompleted ? "" : "✓ ") + "hide them"
                   bordered: true
                   fontFamily: root.fontFamily
                   fontSize: Style.font.caption
                   foreground: root.textColor
                   enabled: !root.busy
                   onClicked: root.setShowCompleted(false)
                  }
                }

               PanelSectionHeader {
                 width: parent.width
                 text: "Bring it in"
                 foreground: root.dim
                fontFamily: root.fontFamily
              }

              TextField {
                id: importField
                width: parent.width
                foreground: root.textColor
                placeholderText: "OmniFocus export folder (.ofocus), or .ofxml / .ofocusmx / .json / .csv"
                text: root.importPath
                onTextEdited: root.importPath = text
                onAccepted: root.doImport()
              }

              TextField {
                id: importTagField
                width: parent.width
                foreground: root.textColor
                placeholderText: "context to add to every imported task"
                text: root.importTag
                onTextEdited: root.importTag = text
              }

              Flow {
                width: parent.width
                spacing: Style.spacing.xs

                Button {
                  text: "browse"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: importDialog.open()
                }

                Button {
                  text: "folder"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: importFolderDialog.open()
                }

                Button {
                  text: root.importDryRun ? "dry run ✓" : "dry run"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  onClicked: root.importDryRun = !root.importDryRun
                }

                Button {
                  text: "import into GTD"
                  bordered: !root.importDryRun
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: root.doImport()
                }
              }

              Text {
                width: parent.width
                visible: root.importDryRun && root.importPath !== ""
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "A dry run reports what would be imported and writes nothing. Turn it off to import for real."
              }

              PanelSectionHeader {
                width: parent.width
                text: "Take it out"
                foreground: root.dim
                fontFamily: root.fontFamily
              }

              TextField {
                id: ofField
                width: parent.width
                foreground: root.textColor
                placeholderText: "OmniFocus .ofxml destination"
                text: root.exportOfPath
                onTextEdited: root.exportOfPath = text
              }

              Button {
                text: "export for OmniFocus"
                bordered: true
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.textColor
                enabled: !root.busy
                onClicked: root.doExport("omnifocus")
              }

              TextField {
                id: zipField
                width: parent.width
                foreground: root.textColor
                placeholderText: "one zip file with everything"
                text: root.exportZipPath
                onTextEdited: root.exportZipPath = text
              }

              Button {
                text: "export one zip file"
                bordered: true
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.textColor
                enabled: !root.busy
                onClicked: root.doExport("zip")
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "The zip holds gtd.json, a manifest, and a snapshot of the database, so it can be read back with or without this program."
              }

              PanelSectionHeader {
                width: parent.width
                text: "Housekeeping"
                foreground: root.dim
                fontFamily: root.fontFamily
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: root.dbInfo
              }

              Flow {
                width: parent.width
                spacing: Style.spacing.xs

                Button {
                  text: "vacuum"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: root.run(["db", "vacuum"], "vacuumed")
                }

                Button {
                  text: "empty the trash"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  enabled: !root.busy
                  onClicked: root.run(["purge"], "trash emptied")
                }

                Button {
                  text: "reload"
                  bordered: true
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  foreground: root.textColor
                  onClicked: root.reload()
                }
              }

               TextField {
                 width: parent.width
                 enabled: false
                 foreground: root.dim
                 text: "poll interval " + root.pollSeconds + "s (bar.layout.right in shell.json)"
                }

               PanelSectionHeader {
                 width: parent.width
                 text: "Activity log"
                 foreground: root.dim
                 fontFamily: root.fontFamily
                }

                Row {
                  spacing: Style.spacing.xs

                  Button {
                    text: "archive"
                    bordered: true
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    foreground: root.textColor
                    onClicked: {
                      root.settingsOpen = false
                      root.pickViewByKey("archived")
                     }
                  }

                  Button {
                    text: "view log"
                    bordered: true
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    foreground: root.textColor
                    onClicked: {
                      root.settingsOpen = false
                      root.pickViewByKey("log")
                     }
                  }

                 Button {
                   text: "trash"
                   bordered: true
                   fontFamily: root.fontFamily
                   fontSize: Style.font.caption
                   foreground: root.textColor
                   onClicked: {
                     root.settingsOpen = false
                     root.pickViewByKey("trash")
                    }
                   }
                 }

               PanelSectionHeader {
                 width: parent.width
                 text: "About"
                 foreground: root.dim
                 fontFamily: root.fontFamily
               }

               Text {
                 width: parent.width
                 textFormat: Text.PlainText
                 wrapMode: Text.WordWrap
                 color: root.dim
                 font.family: root.fontFamily
                 font.pixelSize: Style.font.caption
                 text: "OmaGTD "
                   + (root.appVersion !== "" ? "v" + root.appVersion : "dev")
                   + " — Getting Things Done for Omarchy, modelled on OmniFocus. "
                   + "MIT licensed."
               }
             }

            // ---- Item pane
            Column {
              width: parent.width
              spacing: Style.spacing.md
              visible: !root.settingsOpen

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: {
                  var shown = root.detail && root.detail.item
                  if (shown)
                    return gtdTheme.colorFor(shown, root.isLate(shown), root.textColor)
                  var target = root.selectedItem()
                  if (target && target.raw)
                    return gtdTheme.colorFor(target.raw, root.isLate(target.raw),
                                             root.textColor)
                  return root.textColor
                }
                font.family: root.fontFamily
                font.pixelSize: Style.font.subtitle
                font.bold: true
                font.strikeout: root.isArchived(root.detail && root.detail.item)
                text: {
                  var target = root.selectedItem()
                  if (root.detail && root.detail.item) return root.detail.item.title || ""
                  if (root.detail && root.detail.name) return root.detail.name
                  if (root.detail && root.detail.kind === "step") return root.detail.question || ""
                  return target ? (target.title || "") : "nothing selected"
                }
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: {
                  var item = root.detail && root.detail.item
                  if (item) return root.itemMeta(item) + "   " + (item.id || "")
                  var target = root.selectedItem()
                  if (target && target.raw) {
                    var bits = []
                    if (target.kind) bits.push(target.kind)
                    if (target.raw.kind) bits.push(target.raw.kind)
                    if (target.raw.status) bits.push(target.raw.status)
                    if (target.raw.open_count !== undefined) bits.push(target.raw.open_count + " open")
                    if (target.raw.count !== undefined) bits.push(target.raw.count + " checked")
                    if (target.raw.hint) bits.push(target.raw.hint)
                    if (target.id) bits.push(target.id)
                    return bits.join("  ·  ")
                  }
                  return "pick something on the left or in the middle"
                }
              }

              // A link and any attachments, read-only at a glance. The editor
              // below changes them; this is what the item currently points at.
              Column {
                width: parent.width
                spacing: Style.spacing.xxs
                visible: {
                  var item = root.detail && root.detail.item
                  if (!item) return false
                  return (item.url || "") !== ""
                    || (item.attachments || []).length > 0
                }

                Text {
                  width: parent.width
                  visible: !!(root.detail && root.detail.item
                    && (root.detail.item.url || "") !== "")
                  textFormat: Text.PlainText
                  wrapMode: Text.WrapAnywhere
                  color: root.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  text: root.detail && root.detail.item
                    ? ("↗ " + root.detail.item.url) : ""
                }

                Repeater {
                  model: root.detail && root.detail.item
                    ? (root.detail.item.attachments || []) : []
                  delegate: Text {
                    required property var modelData
                    width: parent.width
                    textFormat: Text.PlainText
                    elide: Text.ElideMiddle
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "attach: " + (modelData.name || modelData.uri || "")
                  }
                }
              }

              // What a project holds, or what a context is attached to.
              Column {
                width: parent.width
                spacing: Style.spacing.xs
                visible: {
                  var step = root.detail && root.detail.kind === "step" ? root.detail : null
                  if (step) return true
                  if (root.detail && root.detail.kind === "tagList")
                    return (root.detail.items || []).length > 0
                  return !!(root.detail && root.detail.item
                    && (root.detail.item.children || []).length)
                }

                PanelSectionHeader {
                  width: parent.width
                  text: root.detail && root.detail.item ? "inside" : "items"
                  foreground: root.dim
                  fontFamily: root.fontFamily
                }

                Repeater {
                  model: {
                    if (root.detail && root.detail.kind === "step") {
                      var ids = root.detail.ids || []
                      var out = []
                      for (var i = 0; i < ids.length; i++) out.push({ id: ids[i], title: ids[i] })
                      return out
                    }
                    if (root.detail && root.detail.kind === "tagList")
                      return root.detail.items || []
                    if (root.detail && root.detail.item) return root.detail.item.children || []
                    return []
                  }
                  delegate: Item {
                    required property var modelData
                    width: detailColumn.width
                    height: Style.space(20)

                    Text {
                      anchors.left: parent.left
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      textFormat: Text.PlainText
                      elide: Text.ElideRight
                      text: (modelData.completed ? "✓ " : "· ") + (modelData.title || modelData.id || "")
                      color: gtdTheme.colorFor(modelData, root.isLate(modelData),
                                               root.textColor)
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.bodySmall
                    }

                    MouseArea {
                      anchors.fill: parent
                      onClicked: root.showItem(modelData.id)
                    }
                  }
                }
              }

              // ---- The editor
              Column {
                width: parent.width
                spacing: Style.spacing.xs
                visible: !!(root.detail && root.detail.item && root.detail.item.id)
                // While a field holds the caret, Escape hands the keyboard back
                // to the list cursor instead of leaving the user stuck.
                Keys.onEscapePressed: function(event) {
                  keyCatcher.forceActiveFocus()
                  event.accepted = true
                }

                PanelSectionHeader {
                  width: parent.width
                  text: "edit"
                  foreground: root.dim
                  fontFamily: root.fontFamily
                }

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  wrapMode: Text.WordWrap
                  visible: text !== ""
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  text: root.itemTimes(root.detail && root.detail.item)
                }

                 Column {
                   width: parent.width
                   spacing: Style.spacing.xxs

                   Text {
                     textFormat: Text.PlainText
                     text: "title"
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                    }

                   TextField {
                     id: titleField
                     width: parent.width
                     foreground: root.textColor
                     placeholderText: "title"
text: root.editTitle
                    onTextEdited: { root.editTitle = text; root.formDirty = true }
                     Keys.onUpPressed: function(event) { root.formStep(-1); event.accepted = true }
                     Keys.onDownPressed: function(event) { root.formStep(1); event.accepted = true }
                     }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "what has to happen; the only field that is required"
                  }
                }

                 Column {
                   width: parent.width
                   spacing: Style.spacing.xxs

                   Text {
                     textFormat: Text.PlainText
                     text: "project"
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                     }

                   TextField {
                     id: projectField
                     width: parent.width
                     foreground: root.textColor
                     placeholderText: "project (empty sends it to the inbox)"
                    text: root.editProject
                    onTextEdited: { root.editProject = text; root.formDirty = true }
                    Keys.onUpPressed: function(event) { root.formStep(-1); event.accepted = true }
                    Keys.onDownPressed: function(event) { root.formStep(1); event.accepted = true }
                    Keys.onTabPressed: function(event) {
                      // Only the leaf of a Move house>Kitchen path is completed.
                      var cut = text.lastIndexOf(">")
                      var leaf = cut >= 0 ? text.slice(cut + 1) : text
                      if (leaf !== "") root.askComplete("project", "editProject",
                                                        projectField, leaf)
                      event.accepted = true
                    }
                  }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "an existing project or area; a name that is not there "
                        + "yet is made as you save"
                  }
                }

                 Column {
                   width: parent.width
                   spacing: Style.spacing.xxs

                   Text {
                     textFormat: Text.PlainText
                     text: "contexts"
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                       }

                   TextField {
                     id: tagsField
                     width: parent.width
                     foreground: root.textColor
                     placeholderText: "contexts, space separated: #errands #home>kitchen"
                    text: root.editTags
                    onTextEdited: { root.editTags = text; root.formDirty = true }
                    Keys.onUpPressed: function(event) { root.formStep(-1); event.accepted = true }
                    Keys.onDownPressed: function(event) { root.formStep(1); event.accepted = true }
                    Keys.onTabPressed: function(event) {
                      // A list of contexts: complete the one being typed.
                      var m = text.match(/(\S*)$/)
                      if (m && m[1] !== "") root.askComplete("context", "editTags",
                                                            tagsField, m[1])
                      event.accepted = true
                    }
                  }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "a context says where or with what this is done; "
                        + "#parent>child nests and makes the parent"
                  }
                }

                 Toggle {
                   id: waitToggle
                   width: parent.width
                   label: "waiting"
                   description: "on = someone else owes this; w flips it, "
                       + "and it leaves Next until they deliver"
                   checked: root.editWait
                   foreground: root.textColor
                   accent: root.accent
                   fontFamily: root.fontFamily
                   titleSize: Style.font.caption
                   descriptionSize: Style.font.caption
                   onClicked: root.toggleWait()
                 }

                 Column {
                   width: parent.width
                   spacing: Style.spacing.xxs

                   Text {
                     textFormat: Text.PlainText
                     text: "deadline"
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                          }

                   Row {
                     width: parent.width
                     spacing: Style.spacing.xs

                      TextField {
                        id: dueField
                        width: parent.width - dueShiftButton.width - dueCalButton.width - parent.spacing * 2
                        foreground: root.textColor
                        placeholderText: "deadline, 2026-10-05 or friday (Tab completes)"
                       text: root.editDue
                       onTextEdited: { root.editDue = text; root.formDirty = true }
                       onEditingFinished: root.editDue = dueField.text
                       onAccepted: root.editDue = dueField.text
                       Keys.onUpPressed: function(event) { root.formStep(-1); event.accepted = true }
                       Keys.onDownPressed: function(event) { root.formStep(1); event.accepted = true }
                       Keys.onTabPressed: function(event) {
                         root.completeDateField("editDue", dueField)
                         event.accepted = true
                        }
                      }

                    Button {
                      id: dueShiftButton
                      text: "+1d"
                      bordered: true
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      foreground: root.textColor
                      enabled: !root.busy
                      onClicked: root.shiftDate("editDue", dueField)
                    }
                    Button {
                      id: dueCalButton
                      text: "pick"
                      bordered: true
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      foreground: root.textColor
                      enabled: !root.busy
                      onClicked: root.openPicker("due")
                    }
                  }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "the deadline; a bare date lands on " + root.dueTime
                        + " from settings, and save writes it"
                  }
                }

                 Column {
                   width: parent.width
                   spacing: Style.spacing.xxs

                   Text {
                     textFormat: Text.PlainText
                     text: "defer date"
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                           }

                   Row {
                     width: parent.width
                     spacing: Style.spacing.xs

                     TextField {
                       id: deferField
                       width: parent.width - deferShiftButton.width - deferCalButton.width - parent.spacing * 2
                       foreground: root.textColor
                       placeholderText: "defer date, <monday (Tab completes)"
                        text: root.editDefer
                        onTextEdited: { root.editDefer = text; root.formDirty = true }
                        Keys.onUpPressed: function(event) { root.formStep(-1); event.accepted = true }
                        Keys.onDownPressed: function(event) { root.formStep(1); event.accepted = true }
                        Keys.onTabPressed: function(event) {
                          root.completeDateField("editDefer", deferField, true)
                          event.accepted = true
                        }
                      }

                    Button {
                      id: deferShiftButton
                      text: "+1d"
                      bordered: true
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      foreground: root.textColor
                      enabled: !root.busy
                      onClicked: root.shiftDate("editDefer", deferField)
                    }
                    Button {
                      id: deferCalButton
                      text: "pick"
                      bordered: true
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      foreground: root.textColor
                      enabled: !root.busy
                      onClicked: root.openPicker("defer")
                    }
                  }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "from this day to the deadline it shows in every "
                        + "day between, in italic with a D; save writes it"
                  }
                }

                 Column {
                   width: parent.width
                   spacing: Style.spacing.xxs

                   Text {
                     textFormat: Text.PlainText
                     text: "effort"
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                          }

                   TextField {
                     id: durationField
                     width: parent.width
                     foreground: root.textColor
                     placeholderText: "effort, 30m or 1h30m"
                    text: root.editDuration
                    onTextEdited: { root.editDuration = text; root.formDirty = true }
                    Keys.onUpPressed: function(event) { root.formStep(-1); event.accepted = true }
                    Keys.onDownPressed: function(event) { root.formStep(1); event.accepted = true }
                  }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                     text: "an estimate of the time it takes, never a deadline"
                   }
                 }

                 Column {
                   width: parent.width
                   spacing: Style.spacing.xxs

                   Text {
                     textFormat: Text.PlainText
                     text: "repeat"
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                   }

                   ButtonGroup {
                     id: repeatGroup
                     options: root.repeatPresets
                     value: root.editRepeat
                     foreground: root.textColor
                     accent: root.accent
                     fontFamily: root.fontFamily
                     fontSize: Style.font.caption
                     onChanged: function(v) { root.editRepeat = v; root.formDirty = true }
                     // Left/Right (or h/l) and Enter are the group's own; Up and
                     // Down step out to the field above and below it.
                     Keys.onUpPressed: function(event) { root.formStep(-1); event.accepted = true }
                     Keys.onDownPressed: function(event) { root.formStep(1); event.accepted = true }
                   }

                   TextField {
                     id: repeatField
                     width: parent.width
                     foreground: root.textColor
                     placeholderText: "or a custom period, e.g. every 2 weeks"
                     text: root.editRepeat
                     onTextEdited: { root.editRepeat = text; root.formDirty = true }
                     Keys.onUpPressed: function(event) { root.formStep(-1); event.accepted = true }
                     Keys.onDownPressed: function(event) { root.formStep(1); event.accepted = true }
                   }

                   Text {
                     width: parent.width
                     textFormat: Text.PlainText
                     wrapMode: Text.WordWrap
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                     text: "repeatable: finishing it brings the next one back; "
                         + "with a defer date and a deadline it walks both forward"
                   }
                 }

                  Column {
                    width: parent.width
                    spacing: Style.spacing.xxs

                    Text {
                      textFormat: Text.PlainText
                      text: "link"
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                         }

Row {
                     width: parent.width
                     spacing: Style.spacing.xs

                     TextField {
                       id: linkField
                       width: parent.width - openLinkButton.width - parent.spacing
                       foreground: root.textColor
                        placeholderText: "link, https://… or a file path"
                       text: root.editLink
                       onTextEdited: { root.editLink = text; root.formDirty = true }
                       Keys.onUpPressed: function(event) { root.formStep(-1); event.accepted = true }
                       Keys.onDownPressed: function(event) { root.formStep(1); event.accepted = true }
                      }

                     Button {
                       id: openLinkButton
                       text: "open"
                       bordered: true
                       fontFamily: root.fontFamily
                       fontSize: Style.font.caption
                       foreground: root.textColor
                       visible: (root.editLink || "").trim() !== ""
                       enabled: !root.busy
                       onClicked: root.openAttachment()
                     }
                   }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "a URL or file path, shown read-only above the editor"
                  }
                }

                Column {
                   width: parent.width
                   spacing: Style.spacing.xxs

                   Text {
                     textFormat: Text.PlainText
                     text: "attachment"
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                             }

                   Row {
                     width: parent.width
                     spacing: Style.spacing.md

                     Text {
                       width: parent.width - attachButtons.width - parent.spacing
                       height: attachButtons.height
                       verticalAlignment: Text.AlignVCenter
                       elide: Text.ElideRight
                       textFormat: Text.PlainText
                       font.family: root.fontFamily
                       font.pixelSize: Style.font.caption
                       color: root.editAttachments ? root.textColor : root.dim
                      text: {
                        if (!root.editAttachments) return "no file attached"
                        if (root.attachmentIsLink()) return root.editAttachments
                        return root.editAttachments.split("/").pop()
                      }
                    }

                    Row {
                      id: attachButtons
                      spacing: Style.spacing.xs

                      Button {
                        text: "attach"
                        bordered: true
                        fontFamily: root.fontFamily
                        fontSize: Style.font.caption
                        foreground: root.textColor
                        enabled: !root.busy
                        onClicked: attachDialog.open()
                      }

                      Button {
                        text: "copy location"
                        bordered: true
                        fontFamily: root.fontFamily
                        fontSize: Style.font.caption
                        foreground: root.textColor
                        visible: !root.attachmentIsLink()
                        enabled: !root.busy && root.editAttachments !== ""
                        onClicked: root.copyAttachmentLocation()
                      }

                      Button {
                        text: "open"
                        bordered: true
                        fontFamily: root.fontFamily
                        fontSize: Style.font.caption
                        foreground: root.textColor
                        visible: root.attachmentIsLink()
                        enabled: !root.busy && root.editAttachments !== ""
                        onClicked: root.openAttachment()
                      }

                      Button {
                        text: "remove"
                        bordered: true
                        fontFamily: root.fontFamily
                        fontSize: Style.font.caption
                        foreground: root.textColor
                        enabled: !root.busy && root.editAttachments !== ""
                        onClicked: { root.editAttachments = ""; root.formDirty = true }
                      }
                    }
                  }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "one file, copied into the store and kept with the task"
                  }
                }

                 Column {
                   width: parent.width
                   spacing: Style.spacing.xxs

                  Text {
                     textFormat: Text.PlainText
                     text: "notes"
                     color: root.dim
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.caption
                            }

                   // Free text, so it wraps and grows with what is written. It
                   // starts at a couple of lines so an empty one still reads as
                   // a place to write; Up and Down still leave for another field.
                   TextArea {
                     id: notesField
                     width: parent.width
                     height: Math.max(Style.space(64), implicitHeight)
                     wrapMode: TextArea.Wrap
                     textFormat: TextArea.PlainText
                     color: root.textColor
                     font.family: root.fontFamily
                     font.pixelSize: Style.font.body
                     placeholderText: "notes"
                     placeholderTextColor: Qt.darker(root.textColor, 1.6)
                     selectionColor: Style.selectionFillFor(root.textColor, root.accent)
                     selectedTextColor: root.textColor
                     readonly property var _spec: Border.controlSpec(
                       activeFocus ? "focus" : (hovered ? "hover-cursor" : "normal"),
                       root.textColor, root.accent)
                     leftPadding: Style.spacing.controlPaddingX + Border.left(_spec)
                     rightPadding: Style.spacing.controlPaddingX + Border.right(_spec)
                     topPadding: Style.spacing.inputPaddingY + Border.top(_spec)
                     bottomPadding: Style.spacing.inputPaddingY + Border.bottom(_spec)
                     text: root.editNotes
                     onTextEdited: { root.editNotes = text; root.formDirty = true }
                     Keys.onUpPressed: function(event) { root.formStep(-1); event.accepted = true }
                     Keys.onDownPressed: function(event) { root.formStep(1); event.accepted = true }
                     background: BorderSurface {
                       color: Style.controlFill(notesField.activeFocus, notesField.hovered, root.textColor, root.accent)
                       borderSpec: notesField._spec
                       radius: Style.cornerRadius
                     }
                   }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: "the body of the task; saved and read back here "
                        + "when you reopen it"
                  }
                }

                Flow {
                  width: parent.width
                  spacing: Style.spacing.xs

                  Button {
                    text: "save"
                    bordered: true
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    foreground: root.textColor
                    enabled: !root.busy
                    onClicked: root.saveEdits()
                  }

                  Button {
                    text: "undone"
                    bordered: true
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    foreground: root.textColor
                    enabled: !root.busy
                    onClicked: root.act("undone")
                  }

                  Button {
                    text: "done"
                    bordered: true
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    foreground: root.textColor
                    onClicked: root.act("done")
                  }

                  Button {
                    text: root.isArchived(root.selectedItem()
                      && root.selectedItem().raw) ? "unarchive" : "archive"
                    bordered: true
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    foreground: root.textColor
                    enabled: !root.busy
                    onClicked: {
                      var at = root.selectedItem()
                      if (at && root.isArchived(at.raw)) root.act("unarchive")
                      else root.act("archive")
                    }
                  }

                  Button {
                    text: "flag"
                    bordered: true
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    foreground: root.textColor
                    onClicked: root.toggleFlag()
                  }

                }
              }
            }
          }
        }

        ScrollBar {
          id: detailScroll
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          height: parent.height
          size: detailFlick.contentHeight > 0
            ? Math.min(1, detailFlick.height / detailFlick.contentHeight) : 1
          position: detailFlick.contentHeight > detailFlick.height
            ? detailFlick.contentY / (detailFlick.contentHeight - detailFlick.height) : 0
          visible: size < 1
          policy: ScrollBar.AsNeeded
        }
      }
    }

    // ---- Footer
    Item {
      id: footer
      width: page.width
      height: Style.space(20)

      Text {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: root.status !== "" ? root.status
          : "← → column  ↑↓ move  tab row/view  enter pick/save  c capture  d done  a archive  f flag  w wait  i inbox  t start  p postpone  o open  z undo  u undone  x drop  s settings  esc close"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      Text {
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: root.busy ? "working…" : (root.stale ? "stale" : "")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }
  }

  // ---- Close confirmation
  // A fullscreen Escape asks before the shell closes the view. The scrim
  // swallows pointer input, the card sits centred, and the FocusScope below
  // owns the keys: Escape is inert, Space takes the highlighted button, and
  // the arrows or Tab move between Cancel and Confirm (Confirm is the default).
  Item {
    id: closeConfirm
    anchors.fill: parent
    visible: root.confirmCloseOpen
    z: 100

    Rectangle {
      anchors.fill: parent
      color: Util.alpha(root.surfaceBackground, 0.7)
    }

    // Swallow clicks on the scrim so the screen behind cannot be reached.
    MouseArea { anchors.fill: parent }

    Rectangle {
      id: closeCard
      width: Math.min(parent.width - Style.space(32), Style.space(400))
      height: closeBody.implicitHeight + Style.space(36)
      anchors.centerIn: parent
      color: root.surfaceBackground
      border.width: Style.normalBorderWidth
      border.color: root.accent
      radius: Style.cornerRadius

      Column {
        id: closeBody
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Style.space(18)
        spacing: Style.spacing.md

        Text {
          width: parent.width
          textFormat: Text.PlainText
          wrapMode: Text.WordWrap
          text: "Close OmaGTD?"
          color: root.textColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.title
          font.bold: true
        }

        Text {
          width: parent.width
          textFormat: Text.PlainText
          wrapMode: Text.WordWrap
          text: "This closes the full-screen view. Press space to confirm, "
                + "or use the arrows or tab to choose."
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        Row {
          anchors.right: parent.right
          spacing: Style.space(10)

          Repeater {
            model: [ { label: "Cancel", confirm: false },
                     { label: "Confirm", confirm: true } ]

            Button {
              required property var modelData
              readonly property int slot: modelData.confirm ? 1 : 0
              text: modelData.label
              bordered: true
              selected: root.confirmCloseIndex === slot
              foreground: root.textColor
              accent: modelData.confirm ? root.urgent : root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              onClicked: {
                root.confirmCloseIndex = slot
                root.confirmCloseActivate()
              }
              onHovered: function(hovered) {
                if (hovered) root.confirmCloseIndex = slot
              }
            }
          }
        }
      }
    }

    FocusScope {
      id: confirmCloseFocus
      anchors.fill: parent
      focus: root.confirmCloseOpen

      Keys.onPressed: function(event) {
        // Escape must not close the app — the user just pressed it to get
        // here, so a second press is not the confirmation.
        if (event.key === Qt.Key_Escape) { event.accepted = true; return }
        if (event.key === Qt.Key_Left || event.key === Qt.Key_Right
            || event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
          root.confirmCloseIndex = root.confirmCloseIndex === 1 ? 0 : 1
          event.accepted = true
          return
        }
        if (event.key === Qt.Key_Space || event.key === Qt.Key_Return
            || event.key === Qt.Key_Enter) {
          root.confirmCloseActivate()
          event.accepted = true
          return
        }
      }
    }
  }
}
