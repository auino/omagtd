import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons

// Quattro-managed fullscreen surface. The shell owns this PanelWindow and
// summons it inside its existing Quickshell process.
Item {
  id: root

  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null
  property bool opened: false
  property string pendingPayload: "{}"

  readonly property string pluginId: manifest && manifest.id
    ? String(manifest.id) : "com.auino.omagtd"

  function dismiss() {
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide(root.pluginId)
  }

  function close() {
    root.opened = false
    if (contentLoader.item) contentLoader.item.close()
  }

  function toggle(payloadJson) {
    if (root.opened) root.dismiss()
    else root.open(payloadJson)
  }

  function applyPayload(payloadJson) {
    var request = {}
    try { request = JSON.parse(String(payloadJson || "{}")) || {} } catch (e) { request = {} }
    var view = contentLoader.item
    if (!view) return

    if (request.action === "view" && request.name) view.pickViewByKey(String(request.name))
    else if (request.action === "openItem" && request.id) view.showItem(String(request.id), true)
    else if (request.action === "editItem" && request.id) view.showItemSelected(String(request.id))
  }

  function open(payloadJson) {
    root.opened = true
    root.pendingPayload = String(payloadJson || "{}")
    if (!contentLoader.item) return
    contentLoader.item.open()
    Qt.callLater(function() { root.applyPayload(root.pendingPayload) })
  }

  PanelWindow {
    id: surface

    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: contentLoader.item ? contentLoader.item.surfaceBackground : Color.background
    WlrLayershell.namespace: "com-auino-omagtd"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: root.opened ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    Loader {
      id: contentLoader
      anchors.fill: parent
      active: true
      source: Qt.resolvedUrl("app/FullScreenContent.qml")

      onLoaded: {
        if (!item) return
        item.fullscreenMode = true
        item.closeRequested.connect(root.dismiss)
        if (root.opened) root.open(root.pendingPayload)
      }
    }
  }
}
