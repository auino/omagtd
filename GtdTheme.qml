import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons

// The colour a row wears for its status, read from the active theme so a
// flagged task here is the same orange as the rest of Omarchy.
//
// Five values carry every state the list distinguishes: red for what is late,
// orange for what is flagged, blue for what is waiting, purple for what has
// been waiting past its date, and the theme's own dimmed foreground for a
// task whose defer date has not come round. Anything else keeps the colour
// the surface it is painted in already uses.
QtObject {
  id: root

  // Before the theme file answers — or if it never does — fall back to what
  // Color has already loaded, plus Tokyo Night's own orange and purple.
   property color red: Color.urgent
   property color orange: "#eb927b"
   property color blue: Color.accent
   property color purple: "#ad8ee6"
   property color green: "#7cb87c"
   property color grey: Qt.darker(Color.foreground, 1.7)

  readonly property var statusGlyphs: ({
    inbox: "·", waiting: "W", someday: "S", reference: "R", onhold: "‖",
    done: "✓", dropped: "×", trashed: "⌫"
  })

  function apply(raw) {
    var lines = String(raw || "").split("\n")
    var foreground = ""
    var dimmed = ""
    for (var i = 0; i < lines.length; i++) {
      var match = lines[i].match(/^\s*([A-Za-z0-9_-]+)\s*=\s*["']?(#[0-9A-Fa-f]{6})/)
      if (!match) continue
      if (match[1] === "red") red = match[2]
      else if (match[1] === "orange") orange = match[2]
      else if (match[1] === "blue") blue = match[2]
      else if (match[1] === "magenta") purple = match[2]
      else if (match[1] === "dark_foreground") dimmed = match[2]
      else if (match[1] === "foreground") foreground = match[2]
    }
    // The theme's own dimmed foreground is exactly "text set back a step";
    // a theme without that key still gets a grey, taken from its text colour.
    if (dimmed) grey = dimmed
    else if (foreground) grey = Qt.darker(foreground, 1.7)
  }

  property FileView colorsFile: FileView {
    path: Color.currentThemePath + "/colors.toml"
    watchChanges: true
    printErrors: false
    onLoaded: root.apply(text())
    onFileChanged: reload()
  }

  // Read the file again. A theme switch rewrites it in place and the watcher
  // does not always follow the swap, so both surfaces re-read when they open:
  // the colours are right by the time anyone is looking at them.
  function refresh() { colorsFile.reload() }

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
    if (status === "done" || status === "dropped" || status === "trashed") return false
    return String(item.defer_at).slice(0, 16) > nowStamp()
  }

  // Expired waiting work has its own purple signal; all other expired work is
  // red. Waiting and future-defer states then take precedence over the flag.
  // A caller that has already decided whether the task is in its defer window
  // (the calendar shows due days as ordinary tasks) passes that decision in as
  // `deferred`; otherwise the future-date check is used.
  function colorFor(item, late, fallback, deferred) {
     if (!item) return fallback
     if (item.status === "done") return green
     var waiting = item.status === "waiting"
     if (waiting && late) return purple
     if (late) return red
     if (waiting) return blue
     if (deferred === undefined ? isDeferred(item) : deferred) return grey
     if (item.priority > 0) return orange
     return fallback
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
    var finished = status === "done" || status === "dropped" || status === "trashed"
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
