pragma ComponentBehavior: Bound
import QtQuick
import Qt.labs.folderlistmodel
import Quickshell
import qs.Commons

// A modal file chooser that lives inside the window it is shown in.
//
// The native QtQuick.Dialogs FileDialog routes through the platform theme —
// on a Wayland session that is the XDG desktop portal, and from a layer-shell
// surface the GTK/gvfs path there aborts the whole shell. This browser instead
// reads directories with FolderListModel (pure QML, no portal, no GTK, no
// gvfs), so it can run safely inside the fullscreen overlay.
//
// It browses one directory at a time: folders open on activation, files are
// picked. `folderMode` turns it into a directory picker instead — the current
// directory is what gets accepted, folders are the only entries shown.
Item {
  id: root

  anchors.fill: parent
  z: 10000

  property bool opened: false
  property bool folderMode: false
  property string title: folderMode ? "Choose a folder" : "Choose a file"
  property string startFolder: ""
  // A comma/space separated shell glob list, FolderListModel's `nameFilters`
  // shape. Empty means "everything".
  property var nameFilters: []
  property string acceptText: folderMode ? "Choose" : "Attach"

  property color background: Color.popups.background
  property color foreground: Color.popups.text
  property color dim: Qt.darker(foreground, 1.5)
  property color accent: Color.accent
  property color scrim: Util.alpha(Color.background, 0.7)
  property string fontFamily: Style.font.family
  property int cornerRadius: Style.cornerRadius

  signal accepted(string path)
  signal canceled()

  readonly property string home: Quickshell.env("HOME") || "/"
  readonly property var entries: folderModel

  function begin(folder, filters) {
    if (filters !== undefined) root.nameFilters = filters || []
    var target = folder && String(folder) !== "" ? String(folder) : root.home
    folderModel.folder = Util.fileUrl(target)
    root.cursor = 0
    root.opened = true
    Qt.callLater(function() { keyFocus.forceActiveFocus() })
    Qt.callLater(function() { keyFocus.forceActiveFocus() })
  }

  function close() {
    if (root.opened) { root.opened = false }
  }

  function accept() {
    var entry = root.selected()
    if (!entry) { root.canceled(); return }
    if (entry.isDir) {
      root.enter(entry.path)
      return
    }
    root.accepted(entry.path)
    root.opened = false
  }

  function acceptFolder() {
    // Folder mode: the directory being browsed is the answer.
    root.accepted(root.localPath(folderModel.folder))
    root.opened = false
  }

  function selected() {
    var e = root.visibleEntries()
    if (root.cursor < 0 || root.cursor >= e.length) return null
    return e[root.cursor]
  }

  function visibleEntries() {
    var out = []
    for (var i = 0; i < folderModel.count; i++) {
      var dir = folderModel.get(i, "fileIsDir")
      if (root.folderMode && !dir) continue
      out.push({ name: folderModel.get(i, "fileName"),
                 path: folderModel.get(i, "filePath"),
                 isDir: dir })
    }
    return out
  }

  function currentFolder() {
    return root.localPath(folderModel.folder)
  }

  function parentFolder() {
    var f = root.currentFolder().replace(/\/+$/, "")
    if (f === "" || f === "/") return "/"
    var slash = f.lastIndexOf("/")
    return slash <= 0 ? "/" : f.slice(0, slash)
  }

  function localPath(url) {
    var p = String(url || "")
    if (p.indexOf("file://") === 0) p = p.slice(7)
    try { return decodeURIComponent(p) } catch (e) { return p }
  }

  function enter(path) {
    folderModel.folder = Util.fileUrl(path)
    root.cursor = 0
  }

  function up() {
    root.enter(root.parentFolder())
  }

  function step(delta) {
    var n = root.visibleEntries().length
    if (n === 0) return
    root.cursor = (root.cursor + delta + n) % n
  }

  function clampCursor() {
    var n = root.visibleEntries().length
    if (root.cursor >= n) root.cursor = Math.max(0, n - 1)
  }

  function handleKey(event) {
    if (!root.opened) return false
    if (event.key === Qt.Key_Escape) { root.canceled(); root.opened = false; return true }
    if (event.key === Qt.Key_Down) { root.step(1); return true }
    if (event.key === Qt.Key_Up) { root.step(-1); return true }
    if (event.key === Qt.Key_Backspace) { root.up(); return true }
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      if (root.folderMode) root.acceptFolder()
      else root.accept()
      return true
    }
    return false
  }

  property int cursor: 0

  visible: opened
  enabled: opened

  FolderListModel {
    id: folderModel
    showDirs: true
    showFiles: !root.folderMode
    showDotAndDotDot: false
    sortField: FolderListModel.Name
    nameFilters: root.nameFilters
    onCountChanged: root.clampCursor()
  }

  Rectangle {
    anchors.fill: parent
    color: root.scrim

    MouseArea { anchors.fill: parent; onClicked: root.canceled() }

    BorderSurface {
      id: card
      width: Math.min(parent.width - Style.space(32), Style.space(560))
      height: Math.min(parent.height - Style.space(32), Style.space(460))
      anchors.centerIn: parent
      color: root.background
      borderSpec: Border.flat(root.accent, Style.normalBorderWidth)
      padding: Style.space(18)
      radius: root.cornerRadius

      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset

        Column {
          anchors.fill: parent
          spacing: Style.spacing.md

          Text {
            id: titleText
            textFormat: Text.PlainText
            width: parent.width
            text: root.title
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
          }

          Row {
            id: rowBrowser
            width: parent.width
            spacing: Style.spacing.xs

            Button {
              id: upButton
              text: "up"
              bordered: true
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.foreground
              onClicked: root.up()
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width - upButton.width - parent.spacing
              height: Style.spacing.controlHeight
              verticalAlignment: Text.AlignVCenter
              elide: Text.ElideLeft
              text: root.currentFolder()
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Rectangle {
            width: parent.width
            height: Math.max(Style.space(120),
              parent.height - titleText.implicitHeight - rowBrowser.height
              - buttonsRow.height - parent.spacing * 3)
            color: "transparent"
            border.color: Util.alpha(root.foreground, 0.18)
            border.width: 1
            clip: true

            ListView {
              id: list
              anchors.fill: parent
              anchors.margins: 1
              model: root.entries
              currentIndex: root.cursor
              clip: true
              boundsBehavior: Flickable.StopAtBounds
              onCurrentIndexChanged: root.cursor = currentIndex

              delegate: Item {
                id: entryRow
                required property int index
                required property string fileName
                required property string filePath
                required property bool fileIsDir

                width: list.width
                height: Style.spacing.controlHeight

                Rectangle {
                  anchors.fill: parent
                  color: entryRow.index === root.cursor ? Util.alpha(root.accent, 0.22) : "transparent"
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.fill: parent
                  anchors.leftMargin: Style.spacing.sm
                  anchors.rightMargin: Style.spacing.sm
                  verticalAlignment: Text.AlignVCenter
                  elide: Text.ElideRight
                  text: (entryRow.fileIsDir ? "\u25B8 " : "") + entryRow.fileName
                  color: entryRow.fileIsDir ? root.accent : root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                MouseArea {
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onEntered: root.cursor = entryRow.index
                  onClicked: {
                    root.cursor = entryRow.index
                    if (entryRow.fileIsDir) root.enter(entryRow.filePath)
                    else if (root.folderMode) root.acceptFolder()
                    else { root.accepted(entryRow.filePath); root.opened = false }
                  }
                }
              }
            }

            Text {
              anchors.centerIn: parent
              visible: folderModel.count === 0
              textFormat: Text.PlainText
              text: "empty folder"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Row {
            id: buttonsRow
            width: parent.width
            spacing: Style.space(10)

            Item { width: parent.width - buttons.width - parent.spacing; height: 1 }

            Row {
              id: buttons
              spacing: Style.space(10)

              Button {
                text: "cancel"
                bordered: true
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.foreground
                onClicked: root.canceled()
              }

              Button {
                text: root.acceptText
                bordered: true
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.accent
                enabled: root.folderMode || root.selected() !== null
                onClicked: {
                  if (root.folderMode) root.acceptFolder()
                  else root.accept()
                }
              }
            }
          }
        }

        Item {
          id: keyFocus
          anchors.fill: parent
          focus: root.opened
          Keys.onPressed: function(event) {
            if (root.handleKey(event)) { event.accepted = true; return }
          }
        }
      }
    }
  }
}
