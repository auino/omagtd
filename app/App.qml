import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// OmaGTD, as a real application window instead of a layer-shell overlay.
//
// The compositor sees an ordinary toplevel: it opens maximized on the monitor
// it lands on, stays in the background while the rest of the desktop is used,
// and closing the window leaves the app. The whole GTD system (FullScreen
// content) lives in FullScreenContent.qml next to this file, unchanged from
// its in-shell days except that the surface plumbing moved in here.
//
// Launching happens through bin/omagtd-app, which starts this config in its
// own quickshell instance (`-n` keeps it a singleton) and then talks to it
// over Quickshell's cross-instance IPC:
//
//   quickshell ipc -p <plugin>/app/App.qml call com.auino.omagtd view today
//   quickshell ipc -p <plugin>/app/App.qml call com.auino.omagtd openItem y8xq0pv7
Window {
  id: root

  visible: true
  title: "OmaGTD"
  color: Color.background
  minimumWidth: Style.space(720)
  minimumHeight: Style.space(480)
  visibility: Window.Maximized

  Loader {
    id: content
    anchors.fill: parent
    source: "FullScreenContent.qml"
    onLoaded: {
      var c = content.item
      if (!c) return
      if ("appMode" in c) c.appMode = true
      if (typeof c.quitRequested !== "undefined")
        c.quitRequested.connect(function() { Qt.quit() })
      // The content's opening straight away starts `gtd` child processes.
      // Process cannot start while the instance is still loading its
      // configuration, so the kick waits a beat for it to settle first.
      kick.start()
    }
  }

  Timer {
    id: kick
    interval: 800
    repeat: false
    running: false
    onTriggered: {
      var c = content.item
      if (c && typeof c.open === "function") c.open()
    }
  }

  // Target for bin/omagtd-app's `ipc call`; the plugin version of the name so
  // the same commands address either surface (the app replaces the old
  // in-shell fullscreen, which used com.auino.omagtd from BarWidget.qml).
  IpcHandler {
    target: "com.auino.omagtd"

    function ping(): void {
      console.log("omagtd app: ping")
    }

    function focus(): void {
      root.requestActivate()
      root.raise()
    }

    // Capture-and-edit: hand the freshly captured item to the editor.
    function openItem(id: string): void {
      var c = content.item
      if (c && typeof c.showItem === "function") c.showItem(id, true)
      root.requestActivate()
    }

    // The panel's Enter: land on the task in Today with its editor showing.
    function editItem(id: string): void {
      var c = content.item
      if (c && typeof c.showItemSelected === "function") c.showItemSelected(id)
      root.requestActivate()
    }

    function view(name: string): void {
      var c = content.item
      if (!c) return
      if (name === "search") c.focusSearch()
      else c.pickViewByKey(name)
      root.requestActivate()
    }

    function quit(): void {
      Qt.quit()
    }
  }

  // A compositor close is a quit: there is no second surface to fall back to.
  onClosing: function(event) {
    Qt.quit()
  }

  Component.onDestruction: Qt.quit()
}