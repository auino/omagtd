import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons

// The palette and status colours the fullscreen app paints itself in.
//
// Two jobs live here. The status colours — red for what is late, orange for a
// flag, blue for waiting, purple for waiting past its date, green for done,
// and the theme's own dimmed foreground for a task whose defer date has not
// come round — are read from the active Omarchy theme, so a flagged task is
// the same orange as the rest of the desktop.
//
// A preset may take over the fullscreen surface's local colors. This object
// never writes to the shell's shared Color or Style singletons.
QtObject {
  id: root

  // "omarchy"  follow the active desktop theme (the default)
  // "of-light" / "of-dark"  an OmniFocus-inspired palette of the app's own
  property string preset: "omarchy"

  readonly property var presets: ({
    "of-light": {
      background: "#ffffff", text: "#1c1c1e", dim: "#8a8a8e", line: "#d8d8dc",
      accent: "#6e5ae0", urgent: "#d64541",
      orange: "#e8a33d", blue: "#3e7bfa", purple: "#8e6fd8",
      green: "#5aa469", grey: "#9aa0a6",
      font: "Adwaita Sans", radius: 6
    },
    "of-dark": {
      background: "#1c1c1e", text: "#f2f2f7", dim: "#9a9aa0", line: "#3a3a3f",
      accent: "#9a86f0", urgent: "#d64541",
      orange: "#e8a33d", blue: "#4a7cc7", purple: "#a98ce0",
      green: "#5aa469", grey: "#8a8a8e",
      font: "Adwaita Sans", radius: 6
    }
  })

  readonly property bool themed: root.preset !== "omarchy"
  readonly property var p: root.presets[root.preset] || null

  // ---- Surface colours ----------------------------------------------------

  // The desktop theme's, or the preset's. These are what every paint in the
  // fullscreen view reads, through FullScreenContent's root properties.
  readonly property color text: root.themed ? root.p.text : Color.popups.text
  readonly property color background: root.themed ? root.p.background : Color.background
  readonly property color accent: root.themed ? root.p.accent : Color.accent
  readonly property color urgent: root.themed ? root.p.urgent : Color.urgent
  readonly property color line: root.themed ? root.p.line : Util.alpha(Color.popups.text, 0.18)
  // Text set back a step. Darkening is the wrong direction on a light
  // background, so a preset names its own dim; the desktop theme keeps the
  // old darkening.
  readonly property color dim: root.themed ? root.p.dim : Qt.darker(Color.popups.text, 1.45)
  readonly property string fontFamily: root.themed ? root.p.font : "monospace"

  // ---- Status colours -----------------------------------------------------

  // The underscores are the file-backed (Omarchy) values, filled by apply();
  // the public names choose between them and the preset.
   property color _red: Color.urgent
   property color _orange: "#eb927b"
   property color _blue: Color.accent
   property color _purple: "#ad8ee6"
   property color _green: "#7cb87c"
   property color _grey: Qt.darker(Color.foreground, 1.7)

  readonly property color red: root.themed ? root.p.urgent : root._red
  readonly property color orange: root.themed ? root.p.orange : root._orange
  readonly property color blue: root.themed ? root.p.blue : root._blue
  readonly property color purple: root.themed ? root.p.purple : root._purple
  readonly property color green: root.themed ? root.p.green : root._green
  readonly property color grey: root.themed ? root.p.grey : root._grey

  readonly property var statusGlyphs: ({
    inbox: "·", waiting: "W", someday: "S", reference: "R", onhold: "‖",
    done: "✓", dropped: "×", trashed: "⌫", archived: "▣"
  })

  function apply(raw) {
    var lines = String(raw || "").split("\n")
    var foreground = ""
    var dimmed = ""
    for (var i = 0; i < lines.length; i++) {
      var match = lines[i].match(/^\s*([A-Za-z0-9_-]+)\s*=\s*["']?(#[0-9A-Fa-f]{6})/)
      if (!match) continue
      if (match[1] === "red") _red = match[2]
      else if (match[1] === "orange") _orange = match[2]
      else if (match[1] === "blue") _blue = match[2]
      else if (match[1] === "magenta") _purple = match[2]
      else if (match[1] === "dark_foreground") dimmed = match[2]
      else if (match[1] === "foreground") foreground = match[2]
    }
    // The theme's own dimmed foreground is exactly "text set back a step";
    // a theme without that key still gets a grey, taken from its text colour.
    if (dimmed) _grey = dimmed
    else if (foreground) _grey = Qt.darker(foreground, 1.7)
  }

  property FileView colorsFile: FileView {
    path: Color.currentThemePath + "/colors.toml"
    watchChanges: true
    printErrors: false
    onLoaded: root.apply(text())
    onFileChanged: root.refresh()
  }

  // The theme swap replaces the whole `current/theme` directory, so a watcher on
  // colors.toml does not reliably survive it. `theme.name` sits beside the
  // directory and is rewritten in place on every change, so watching it is the
  // dependable signal that the desktop palette moved.
  property FileView themeNameFile: FileView {
    path: Color.stateHome + "/omarchy/current/theme.name"
    watchChanges: true
    printErrors: false
    onFileChanged: root.refresh()
  }

  // Read the active theme again when following Omarchy. The shell owns the
  // shared Color and Style objects; this plugin only reads their current state.
  function refresh() {
    if (root.themed) return
    colorsFile.reload()
    Color.colorsFile.reload()
    Color.shellFile.reload()
    Style.refresh()
  }

  // ---- Local preset selection --------------------------------------------
  // Keep custom palette choices local to this plugin surface. Mutating Color
  // or Style here would change the rest of the user's Omarchy shell.
  function applyPreset(name) {
    root.preset = name || "omarchy"
  }

  // "YYYY-MM-DD HH:MM", the shape a due or defer date is kept in, so a
  // lexicographic compare is enough. Read fresh rather than held, because a
  // row can sit on screen for hours while its defer date comes round.
  function nowStamp() {
    var d = new Date()
    function two(n) { return (n < 10 ? "0" : "") + n }
    return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate())
      + " " + two(d.getHours()) + ":" + two(d.getMinutes())
  }

  // A task whose defer date has not come round, and which is not already
  // finished or thrown away.
  function isDeferred(item) {
    if (!item || !item.defer_at) return false
    var status = item.status || ""
    if (status === "done" || status === "dropped" || status === "trashed" || status === "archived") return false
    return String(item.defer_at).slice(0, 16) > nowStamp()
  }

  // A task sitting on today's list because its defer window has opened, not
  // because its deadline is today: the defer date has come round (or passed)
  // while the deadline still lies ahead. It wears the same defer mark the
  // calendar gives work scheduled for a day, so a deferred task due later does
  // not read as one merely due today.
  function scheduledIntoToday(item, todayStamp) {
    if (!item || !item.defer_at || !item.due_at) return false
    var today = String(todayStamp || nowStamp()).slice(0, 10)
    return String(item.defer_at).slice(0, 10) <= today
      && String(item.due_at).slice(0, 10) > today
  }

  // Expired waiting work has its own purple signal; all other expired work is
  // red. Waiting and future-defer states then take precedence over the flag.
  // A caller that has already decided whether the task is in its defer window
  // (the calendar shows due days as ordinary tasks) passes that decision in as
  // `deferred`; otherwise the future-date check is used.
  //
  // The decision is kept first as a name, because the calendar only needs to
  // know which colour a task wears — it counts them. Reading the name from the
  // same place the rows do is what keeps a purple row from sitting under a red
  // mark. "plain" is the one state with no colour of its own: it takes the
  // caller's own text colour instead.
  function colorKeyFor(item, late, deferred) {
     if (!item) return "plain"
     if (item.status === "done" || item.status === "archived") return "green"
     var waiting = item.status === "waiting"
     if (waiting && late) return "purple"
     if (late) return "red"
     if (waiting) return "blue"
     if (deferred === undefined ? isDeferred(item) : deferred) return "grey"
     if (item.priority > 0) return "orange"
     return "plain"
   }

  function colorFor(item, late, fallback, deferred) {
    var key = colorKeyFor(item, late, deferred)
    if (key === "plain") return fallback
    if (key === "red") return red
    if (key === "purple") return purple
    if (key === "blue") return blue
    if (key === "orange") return orange
    if (key === "green") return green
    return grey
  }

  // The mark one task earns on the calendar grid: its row's colour, with the
  // grid's neutral grey standing in for a plain task — a dot needs a colour,
  // and grey is the "nothing is wrong with this one" of the grid.
  function markKeyFor(item, late, deferred) {
    var key = colorKeyFor(item, late, deferred)
    return key === "plain" ? "grey" : key
  }

  // The mark in front of a title, built from what is true of the item right
  // now rather than picked from a fixed order, so every state that earns a
  // mark gets one and none is hidden behind another. A task stacks its status
  // mark, then its defer mark, then the flag: a flagged waiting task reads
  // "W!", a deferred one reading "D", a plain one keeping the middle dot. A
  // project and an area keep their own single mark. Finished work shows only
  // its end mark. The result is padded to a steady width so titles line up
  // down the list however many marks a row wears.
  function rowGlyph(item, deferred) {
    if (!item) return "  "
    if (item.kind === "project") return item.completed ? "✓ " : "▸ "
    if (item.kind === "area") return "▤ "
    var status = item.status || ""
    var finished = status === "done" || status === "dropped" || status === "trashed" || status === "archived"
    var parts = []
    var mark = statusGlyphs[status]
    // "inbox" maps to the same dot a plain task falls back to, so it is not
    // added twice.
    if (mark && status !== "inbox") parts.push(mark)
    if (!finished && (deferred === undefined ? isDeferred(item) : deferred)) parts.push("D")
    if (!finished && item.priority > 0) parts.push("!")
    var text = parts.join("")
    if (text === "") text = "·"
    while (text.length < 2) text += " "
    return text
  }
}
