import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// GTD: the pill in the bar, host for the panel, and launcher of the app.
//
// This is the manifest entry point. It owns the pill, the IPC target and the
// quick panel (Panel.qml); the full view is the standalone application in
// app/App.qml, a real window in its own quickshell process that outlives this
// bar. The shell routes on this shape — it looks for open/close/opened on the
// widget mounted in the bar slot — so the pill must be the thing the bar sees.
BarWidget {
  id: root
  moduleName: "com.auino.omagtd"

  // Mirrors of the panel's state, so the pill has nothing to compute.
  readonly property var panel: panelLoader.item
  readonly property int inbox: panel ? panel.inbox : 0
  readonly property int next: panel ? panel.next : 0
  readonly property int today: panel ? panel.today : 0
  readonly property int dueToday: panel ? panel.dueToday : 0
  readonly property int dueTodayLate: panel ? panel.dueTodayLate : 0
  readonly property int overdue: panel ? panel.overdue : 0
  readonly property int archived: panel ? panel.archived : 0
  readonly   property int flagged: panel ? panel.flagged : 0
  readonly property int flaggedToday: panel ? panel.flaggedToday : 0

  // Settings.
  readonly property bool compact: root.settings && root.settings.compact === true
  readonly property bool showInbox: !root.settings || root.settings.showInboxCount !== false
  readonly property bool showNext: !root.settings || root.settings.showTodayCount !== false
  readonly property bool expandedPill: root.settings && root.settings.expandedPill === true
  readonly property int urgentAt: {
    var n = root.settings && typeof root.settings.urgentInboxThreshold === "number"
        ? root.settings.urgentInboxThreshold : 10
    return Math.max(1, n)
  }

  // The theme's orange, read the same way the panel reads it, so a flagged day
  // on the pill is the flag colour used everywhere else in OmaGTD.
  GtdTheme { id: gtdTheme }

  // ---- Panel lifecycle contract (shell.summon/hide/toggle routing).
  readonly property bool opened: panel ? panel.opened === true : false
  readonly property bool popoutSwitchClosing: panel ? panel.popoutSwitchClosing === true : false

  function open() { if (panel) panel.open() }
  function close() { if (panel) panel.close() }
  function togglePanel() { if (panel) panel.toggle() }
  function closeForPopoutSwitch() { if (panel) panel.closeForPopoutSwitch() }
  function refresh() { if (panel) panel.refresh() }

  // The full view is the standalone app now, not a second surface of this
  // widget; the panel hands off to it rather than living on screen next to it.
  // Handing over drops the panel first, so the app is the only OmaGTD surface
  // the user is looking at.
  //
  // bin/omagtd-app starts it (single-instance by config) and forwards a
  // command over IPC.
  readonly property string appCli: Qt.resolvedUrl("bin/omagtd-app").toString().replace("file://", "")

  function launchApp(args) {
    try { Quickshell.execDetached([root.appCli].concat(args || [])) } catch (e) {}
  }

  function openFullscreen() { root.close(); root.launchApp(["focus"]) }
  function closeFullscreen() { root.launchApp(["quit"]) }
  function toggleFullscreen() { root.close(); root.launchApp(["focus"]) }

  function viewApp(name) { root.launchApp(["view", name]) }

  // Used by the panel's capture-and-edit: open the app with the detail editor
  // already on the given item.
  function openItem(id) {
    if (id) root.launchApp(["open-item", String(id)])
  }

  // The panel's Enter: hand one task to the full view, landing on Today with
  // the task selected and its editor showing. The panel drops first so the app
  // is the only surface on screen.
  function editItem(id) {
    if (!id) return
    root.close()
    root.launchApp(["edit-item", String(id)])
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  implicitWidth: layout.implicitWidth
  implicitHeight: layout.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  // The deadline watcher: the bar owns the poll, so a task announces itself
  // when its deadline passes even if neither surface is open. `notify` marks
  // what it sent, so each deadline fires once.
  readonly property string gtdCli: Qt.resolvedUrl("bin/gtd").toString().replace("file://", "")
  readonly property int notifySeconds: {
    var n = root.settings && typeof root.settings.pollIntervalSec === "number"
        ? root.settings.pollIntervalSec : 60
    return Math.max(15, Math.min(900, Math.round(n)))
  }

  Process {
    id: notifyProc
    command: [root.gtdCli, "notify"]
  }

  Timer {
    interval: root.notifySeconds * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: {
      notifyProc.running = false
      notifyProc.running = true
    }
  }

  // Single IPC target for the plugin; Panel.qml leaves manageIpc off.
  //   omarchy-shell shell toggle com.auino.omagtd
  //   omarchy-shell shell com.auino.omagtd fullscreen
  IpcHandler {
    target: "com.auino.omagtd"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.togglePanel() }
    function capture(): void { root.open(); if (root.panel) root.panel.focusCapture() }
    function refresh(): void { root.refresh() }
    // The full view lives in the standalone app: these open it, focused.
    function fullscreen(): void { root.openFullscreen() }
    function closeFullscreen(): void { root.closeFullscreen() }
    function toggleFullscreen(): void { root.toggleFullscreen() }
    function view(name: string): void { root.viewApp(name) }
  }

  // The pill reads as `GTD n/m`: today's expired count over everything
  // expiring today. Only today counts — the second number is the calendar day,
  // so a task stays counted after its clock time passes, and the first is the
  // slice of it that already has. Work left over from earlier days sits in
  // `overdue` and only shows in the tooltip.
  //
  // With `hideZeroCounts` on, a clear day collapses to plain "GTD": "0/0" is
  // noise that draws the eye to a number with nothing behind it. The tooltip
  // keeps the real figures, so nothing is actually hidden.
  readonly property bool hideZeroCounts: root.settings && root.settings.hideZeroCounts === true
  readonly property bool countsAreZero: root.dueTodayLate === 0 && root.dueToday === 0
  readonly property string pillText: (root.hideZeroCounts && root.countsAreZero)
    ? "GTD"
    : "GTD " + root.dueTodayLate + "/" + root.dueToday
  // One shared signal for "something is late", read in three steps: red when
  // anything has expired (today's time passed, or work left over from an
  // earlier day — leftovers do colour the pill now), orange when a task on
  // today's list is flagged and nothing has expired, plain otherwise. Every
  // flagged action sits in the `today` view, so the global flagged count is
  // exactly "at least one flagged task on today".
  readonly property bool pillRed: root.overdue > 0 || root.dueTodayLate > 0
  readonly property bool pillOrange: !root.pillRed && root.flaggedToday > 0 && root.dueToday > 0

  // The bar slot is just the pill: the counts open the quick panel, and the
  // full view is reached from the panel itself (its ↓ / capture-and-edit) or
  // by middle-clicking the pill.
  Row {
    id: layout

    WidgetButton {
      id: button
      bar: root.bar
      text: root.pillText
      labelVisible: true
      hasVisualContent: true
      horizontalMargin: 8.75
      verticalPadding: 8.75
      active: root.pillRed || root.pillOrange
      activeColor: root.pillRed ? (bar ? bar.urgent : Color.urgent) : gtdTheme.orange
      tooltipText: {
        var t = "OmaGTD"
        t += "\nGTD " + root.dueTodayLate + "/" + root.dueToday
          + "  " + root.dueTodayLate + " expired, " + root.dueToday + " due today"
        t += "\n" + root.overdue + " past their deadline"
        t += "\n" + root.inbox + " in the inbox"
        t += "\n" + root.next + " next action" + (root.next === 1 ? "" : "s")
        if (root.archived > 0) t += "\n" + root.archived + " archived"
        t += "\nClick for the panel · middle-click for the full view"
        return t
      }

      // Hovering is how people "check" the list, so make that a real refresh.
      onTooltipHoveredChanged: if (tooltipHovered) root.refresh()

      onPressed: function(b) {
        if (b === Qt.RightButton) root.refresh()
        else if (b === Qt.MiddleButton) root.toggleFullscreen()
        else root.togglePanel()
      }
    }
  }
}
