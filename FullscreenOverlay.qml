import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons

// Quattro-managed fullscreen surfaces. The shell owns this item and summons it
// inside its existing Quickshell process, in one of two shapes:
//
//   overlay  a layer-shell surface over everything, for the bar and the hotkey
//   app      an ordinary maximized window (bar visible, in the window list),
//            for the desktop entry and for keeping the system beside the work
//
// Both load the same app/FullScreenContent.qml, so nothing about the workflow
// can disagree between them. The payload picks the shape: `{"app":true}` asks
// for the window, anything else (including an empty payload) for the overlay.
Item {
  id: root

  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null
  // Any surface open. The shell reads this for toggle() and isPluginOpen().
  property bool opened: false
  // Which shape the current open uses. Not persistent: every summon decides.
  property bool appMode: false
  // The app content is only built the first time the window is asked for, then
  // kept, so reopening it lands where the user left it.
  property bool appBuilt: false
  property string pendingPayload: "{}"

  readonly property string pluginId: manifest && manifest.id
    ? String(manifest.id) : "com.auino.omagtd"

  function dismiss() {
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide(root.pluginId)
  }

  function close() {
    root.opened = false
    root.appMode = false
    if (overlayContent.item) overlayContent.item.close()
    if (appContent.item) appContent.item.close()
  }

  function toggle(payloadJson) {
    if (root.opened) root.dismiss()
    else root.open(payloadJson)
  }

  // The overlay's `g`: hand the view it is showing to the window, which takes
  // the screen over from it. open() switches the shape (closing the overlay)
  // and reopens on the same view.
  function showApp() {
    var view = overlayContent.item
    var key = view && view.view ? String(view.view.key) : ""
    var request = { app: true }
    if (key) {
      request.action = "view"
      request.name = key
    }
    root.open(JSON.stringify(request))
  }

  function parsePayload(payloadJson) {
    var request = {}
    try { request = JSON.parse(String(payloadJson || "{}")) || {} } catch (e) { request = {} }
    return request
  }

  function applyPayload(payloadJson) {
    var request = root.parsePayload(payloadJson)
    var view = root.appMode ? appContent.item : overlayContent.item
    if (!view) return

    // An explicit destination wins over the settings' landing view, including
    // on a cold app open where that read lands after this call.
    if (request.action) view.pendingStartView = false

    if (request.action === "view" && request.name) view.pickViewByKey(String(request.name))
    else if (request.action === "openItem" && request.id) view.showItem(String(request.id), true)
    else if (request.action === "editItem" && request.id) view.showItemSelected(String(request.id))
  }

  function open(payloadJson) {
    var request = root.parsePayload(payloadJson)
    var wantApp = request.app === true

    // One shape at a time: the other view keeps its state, but stops reading
    // as open so it does not poll while out of sight.
    if (wantApp !== root.appMode) {
      if (wantApp && overlayContent.item) overlayContent.item.close()
      if (!wantApp && appContent.item) appContent.item.close()
      root.appMode = wantApp
    }

    root.opened = true
    root.pendingPayload = String(payloadJson || "{}")
    if (wantApp) root.appBuilt = true

    var view = wantApp ? appContent.item : overlayContent.item
    if (!view) return
    view.open()
    Qt.callLater(function() { root.applyPayload(root.pendingPayload) })
  }

  // ---- the overlay: a layer-shell surface over everything ------------------

  PanelWindow {
    id: surface

    visible: root.opened && !root.appMode
    anchors { top: true; bottom: true; left: true; right: true }
    color: overlayContent.item ? overlayContent.item.surfaceBackground : Color.background
    WlrLayershell.namespace: "com-auino-omagtd"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: (root.opened && !root.appMode)
      ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    Loader {
      id: overlayContent
      anchors.fill: parent
      active: true
      source: Qt.resolvedUrl("app/FullScreenContent.qml")

      onLoaded: {
        if (!item) return
        item.fullscreenMode = true
        item.closeRequested.connect(root.dismiss)
        item.appRequested.connect(root.showApp)
        if (root.opened && !root.appMode) root.open(root.pendingPayload)
      }
    }
  }

  // ---- the app: a real window, in the window list --------------------------

  FloatingWindow {
    id: appWindow

    visible: root.opened && root.appMode
    title: "OmaGTD"
    color: appContent.item ? appContent.item.surfaceBackground : Color.background
    implicitWidth: Style.space(1280)
    implicitHeight: Style.space(820)
    minimumSize: Qt.size(Style.space(720), Style.space(480))
    // A maximized tiled window: the bar stays visible, the window keeps its
    // border, and it sits in the workspace beside everything else. Hyprland
    // needs a window rule to honor this (see README), the hint below covers
    // compositors that read the xdg maximized state directly.
    maximized: true

    // The compositor closed the window: tell the shell, so its open state does
    // not outlive the surface it names.
    onClosed: root.dismiss()

    Loader {
      id: appContent
      anchors.fill: parent
      active: root.appBuilt
      source: Qt.resolvedUrl("app/FullScreenContent.qml")

      onLoaded: {
        if (!item) return
        item.fullscreenMode = true
        item.windowMode = true
        item.closeRequested.connect(root.dismiss)
        if (root.opened && root.appMode) root.open(root.pendingPayload)
      }
    }
  }
}
