import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// GTD: the pill in the bar, host for the panel, and launcher of the overlay.
//
// This is the manifest entry point. It owns the pill, the IPC target and the
// quick panel (Panel.qml); the full view is a shell-managed overlay. The shell
// routes on this shape — it looks for open/close/opened on the
// widget mounted in the bar slot — so the pill must be the thing the bar sees.
BarWidget {
  id: root
  moduleName: "com.auino.omagtd"
  // The bar injects a capability-scoped shell facade for this plugin.
  function pluginShell() { return root.bar ? root.bar.shell : null }

  // Mirrors of the panel's state, so the pill has nothing to compute.
  readonly property var panel: panelLoader.item
  readonly property int inbox: panel ? panel.inbox : 0
  readonly property int next: panel ? panel.next : 0
  readonly property int today: panel ? panel.today : 0
  readonly property int dueToday: panel ? panel.dueToday : 0
  readonly property int dueTodayLate: panel ? panel.dueTodayLate : 0
  readonly property int overdue: panel ? panel.overdue : 0
  readonly property int archived: panel ? panel.archived : 0
  readonly property int flagged: panel ? panel.flagged : 0
  readonly property int flaggedToday: panel ? panel.flaggedToday : 0
  // The list the panel is showing: its open-row count and the colour those
  // rows add up to, so the pill names the same number and wears the same
  // colour as the panel header.
  readonly property color todayTone: panel ? panel.listTone : "transparent"
  readonly property string todayToneKey: panel ? panel.listToneKey : "plain"

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

  // ---- Panel lifecycle contract (shell.summon/hide/toggle routing).
  readonly property bool opened: panel ? panel.opened === true : false
  readonly property bool popoutSwitchClosing: panel ? panel.popoutSwitchClosing === true : false

  function open() { if (panel) panel.open() }
  function close() { if (panel) panel.close() }
  function togglePanel() { if (panel) panel.toggle() }
  function closeForPopoutSwitch() { if (panel) panel.closeForPopoutSwitch() }
  function refresh() { if (panel) panel.refresh() }

  // The shell owns the fullscreen surface. A small JSON payload lets the
  // overlay open the requested view or item after the shell loads it.
  function summonFullscreen(payload) {
    var api = root.pluginShell()
    if (api && typeof api.summon === "function")
      api.summon("com.auino.omagtd", JSON.stringify(payload || {}))
  }

  function openFullscreen() { root.close(); root.summonFullscreen({ action: "open" }) }
  function closeFullscreen() {
    var api = root.pluginShell()
    if (api && typeof api.hide === "function") api.hide("com.auino.omagtd")
  }
  function toggleFullscreen() {
    root.close()
    var api = root.pluginShell()
    if (api && typeof api.toggle === "function")
      api.toggle("com.auino.omagtd", JSON.stringify({ action: "open" }))
  }

  function viewApp(name) {
    root.close()
    root.summonFullscreen({ action: "view", name: name })
  }

  // Used by the panel's capture-and-edit: open the app with the detail editor
  // already on the given item.
  function openItem(id) {
    if (!id) return
    root.close()
    root.summonFullscreen({ action: "openItem", id: String(id) })
  }

  // The panel's Enter: hand one task to the full view, landing on Today with
  // the task selected and its editor showing. The panel drops first so the app
  // is the only surface on screen.
  function editItem(id) {
    if (!id) return
    root.close()
    root.summonFullscreen({ action: "editItem", id: String(id) })
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
    // The full view is managed as a shell overlay.
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
  // The pill's colour is the panel's: the most urgent open state among the
  // rows the panel is showing, from the same ladder the rows walk. Only a loud
  // state tints it — red, purple, pink, blue or orange. Grey (deferred) and a
  // plainly-open list keep the bar's own text colour, so the pill never fades
  // into the bar. The `n/m` numbers keep their own meaning — expired today over
  // everything due today — which the tooltip spells out.
  readonly property var loudTones: ["red", "purple", "pink", "blue", "orange"]
  readonly property bool pillLit: root.loudTones.indexOf(root.todayToneKey) >= 0
  readonly property color pillColor: root.pillLit ? root.todayTone : "transparent"

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
      active: root.pillLit
      activeColor: root.pillColor
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
