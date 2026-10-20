# OmaGTD

Getting Things Done for [Omarchy](https://omarchy.org/), modelled on OmniFocus.
A keyboard-driven GTD system that lives in the bar and the shell: capture into
an inbox, clarify, organize, reflect, and engage — projects/areas, contexts
(tags), waiting, someday/maybe, recurring actions, and an internal
calendar, backed by a sqlite3 database and scriptable through a `gtd` CLI.

![OmaGTD plugin preview panel](preview.png)

![OmaGTD plugin preview overlay](preview_overlay.png)

## Human-generated content

As this is an AI-generated program and content, for human-generated
infomation about this repository, see the [HUMANS.md](HUMANS.md) file.

## What's included

- **Bar pill** — inbox and next-action counts, with a compact glyph-only mode.
- **Panel** — a quick overlay for what is on today: what is due, plus deferred
  work whose window has opened. Driven entirely by the keyboard.
- **Fullscreen view** — the whole five-step workflow in one window (see
  *Standalone app* below).
- **`gtd` CLI** — every step is scriptable, so it can be bound to a hotkey.

## GTD in OmaGTD

OmaGTD is David Allen's five steps, each one a command you can run, script or
bind to a hotkey — and each one a view in the fullscreen app:

```
   capture ─▶ [ INBOX ] ─▶ clarify ─▶ organize ─▶ engage
                  ▲                        │
                  │                        │
                  └───── reflect ◀─────────┘
```

| Step | `gtd` command | Fullscreen view |
| --- | --- | --- |
| 1. Capture | `gtd capture` | the top bar field (`c` or `/` to focus) |
| 2. Clarify | `gtd clarify`, `gtd triage` | Inbox, and the Clarify queue |
| 3. Organize | `gtd edit`, `gtd project`, `gtd tag` | Projects / Contexts trees, drag-and-drop onto a project, a context or a day |
| 4. Reflect | `gtd review daily`, `gtd review weekly` | Review |
| 5. Engage | `gtd focus`, `gtd today`, `gtd engage` | Focus, Today, Overdue, Flagged, Upcoming |

**Step 1 — Capture.** Three doors, all into one inbox: `gtd capture` (also the
bar pill, the panel's capture field and the overlay's top field), `gtd import`
for OmniFocus exports plus `json`/`csv`/`zip`, and `gtd import-lines` for a
plain text file of one capture-style line per task. A bare capture *is* an
inbox item; give it a project, an area or a context and it becomes a next
action.

**Step 2 — Clarify.** `gtd clarify ID OUTCOME` decides what an item is, with
Allen's outcomes: `actionable` (leave the inbox), `do-now` (the two-minute
rule — done immediately), `defer` (real, but not now), `wait` (not yours to
do), `someday`, `reference`, `delete`. An `actionable` or `defer` item is
refused without a home — a project, an area or a context — because an
organized action with nowhere to live is the thing that makes people stop
trusting a system. `gtd triage` prints the queue with the decision each item
is waiting on.

**Step 3 — Organize.** Projects and areas are containers; contexts (`#tag`)
are the conditions a task can be done in, and both nest with `>`
(`Personal>Errands`, `#home>kitchen`). `gtd edit` sets them one at a time,
`gtd project`/`gtd tag` manage the containers themselves, and the fullscreen
view files a task by dragging it onto a project, a context or a calendar day.

**Step 4 — Reflect.** `gtd review daily` is "what is true right now" — the
inbox, today, overdue and waiting. `gtd review weekly` is the Weekly Review as
a checklist in Allen's order: empty the inbox, the coming week's calendar, who
you are waiting on, someday/maybe, stalled and empty projects, what is
flagged, what is overdue, what has not moved. Projects and contexts also carry
OmniFocus's review cadence — the "Review: 12" counter that says a container
has drifted out of sight.

**Step 5 — Engage.** `gtd focus` and `gtd today` are the working lists, and
`gtd engage` (with the short verbs `gtd done`, `gtd start`, `gtd flag`,
`gtd postpone`, …) moves a single item through its life. The bar pill is the
glanceable half of this step.

**The tickler.** A `defer` date is the tickler: an item with a future defer
date is kept out of Next and Today until its date comes round, and
`gtd list forecast` (the *Upcoming* view) is what is waiting to become
available. There is one defer date per action rather than a separate
filing-cabinet tickler, and it doubles as the start date that keeps a task
from surfacing before you can work on it.

An item parked as `waiting` that has not yet been given a project, an area or
a context still shows in the inbox — it was parked before it was organized, so
it is still waiting on a decision. It keeps its `waiting` status, so the
*Waiting* view shows it too; a waiting item with a home has been decided and
stays out of the inbox.

### Running each step from the CLI

```bash
# 1. capture — a bare capture lands in the inbox
gtd capture "Pay the electricity bill"      # → Inbox (7f3a2c)

# 2. clarify — see what is waiting, then decide what it is
gtd list inbox
gtd triage
gtd clarify 7f3a2c actionable --project "Home admin" --due friday
#   other outcomes: do-now | defer (needs --defer) | wait | someday | reference | delete

# 3. organize — file it, or set its context
gtd tag add home                            # create a context
gtd edit 7f3a2c --tag home                  # attach it

# 4. reflect
gtd review daily                            # the two-minute pass
gtd review weekly                           # the Weekly Review checklist

# 5. engage — work the lists
gtd focus                                   # next actions you could do right now
gtd today
gtd flag 7f3a2c                             # these short verbs are `gtd engage ID …`
gtd done 7f3a2c
```

`actionable` and `defer` require `--project`, `--area` or `--tag`; the other
outcomes do not. A capture that already names a context or a date skips the
inbox entirely, so the first two steps collapse into one:

```bash
gtd capture "Pay the electricity bill #home >friday"
```

`ID` is an item id, a unique prefix, or `last` for the item touched most
recently. Prefer the id you captured with (`gtd capture --json` prints it):
after a command that also creates a project, `last` points at that project,
not at the task.

## Install on any Omarchy PC

OmaGTD is a shell plugin, so install it with the plugin manager:

```bash
omarchy plugin add https://github.com/auino/omagtd.git --enable
```

You will be asked to confirm the git URL (plugins run unsandboxed inside the
shell — [review the code](https://github.com/auino/omagtd) if you like) and to
choose the bar section. The bar widget defaults to the right section.

Update to the latest version with:

```bash
omarchy plugin update com.auino.omagtd
```

Remove it with:

```bash
omarchy plugin remove com.auino.omagtd
```

## Full workflow overlay

The full five-step workflow is a Quattro-managed fullscreen overlay inside the
existing Omarchy shell process. The bar widget summons it directly; no second
Quickshell process is started. The optional `omagtd-app` command is only a
convenience wrapper around the Omarchy shell IPC:

```bash
~/.config/omarchy/plugins/com.auino.omagtd/bin/omagtd-app            # open the overlay
~/.config/omarchy/plugins/com.auino.omagtd/bin/omagtd-app view today # open on a view
~/.config/omarchy/plugins/com.auino.omagtd/bin/omagtd-app quit       # close the overlay
```

To invoke it from the terminal, symlink the wrapper onto your PATH:

```bash
ln -s ~/.config/omarchy/plugins/com.auino.omagtd/bin/omagtd-app ~/.local/bin/omagtd-app
```

The window is a real toplevel, not a layer-shell overlay: it opens maximized on
the monitor it lands on, stays in the background, and closing the window quits
the app.

## The `gtd` CLI

Everything the UI does is available on the command line. The plugin ships a
`gtd` launcher in `bin/`; run it directly:

```bash
~/.config/omarchy/plugins/com.auino.omagtd/bin/gtd --help
```

or via the core module (the entry point used by tests):

```bash
python3 ~/.config/omarchy/plugins/com.auino.omagtd/lib/gtd/cli.py --help
```

Common verbs: `capture` (inbox), `clarify`/`triage`, `edit`, `defer`,
`postpone`/`roll`, `open` (open an item's link), `today`, `focus`, `engage`,
`review`, `tag`/`context`, `tree`, `calendar`/`cal`, `agenda`, `settings`,
`import`, `import-lines`, `export`, `export-day`, `db`.

An OmniFocus `import` reads any of its exports — a `.ofocus` document (the
folder of zipped transaction files OmniFocus writes for *File ▸ Export*),
a `.ofxml` backup, or the line-oriented `.ofocusmx` text — plus our own
`json`, `csv` and `zip`. The format is chosen by content unless `--format`
says otherwise.

`import-lines` (alias `import-txt`) reads a file nobody else wrote: one
quick-entry line per task, the syntax `capture` takes. `#errands` and
`#home>kitchen` give contexts, `/Garden fence/` a project, `/a/Home admin/` an
area, `<tomorrow` a defer window, `>friday` a deadline, `~every week` a repeat
rule, `*1h30m` an effort, `!` a flag. A project or an area the file names but
the system has never heard of is created with the tasks that belong to it — an
import file is deliberate about its containers. A line that cannot become a
task is reported with its line number and the rest still import, so one bad
line costs nothing but itself; `--dry-run` rehearses the file and undoes it,
and `--tag NAME` stamps every line with an extra context:

```bash
gtd import-lines errands.txt               # one task per line, containers and all
gtd import-lines errands.txt --dry-run     # report the plan, write nothing
gtd import-lines errands.txt --tag backlog # stamp every line with `backlog`
```

`export day` writes the commitments of a single date — what falls due that day,
whatever has been deferred on or before it, and what got finished — with the
moment the export was taken and the command line that produced it, to a file in
markdown, HTML, JSON, YAML or PDF. One
rule picks the tasks, and it is the same for today and for any other date: a
deadline on that date, or a defer window already open by it. The export of the
5th lists what is due on the 5th and everything available by the 5th, and
nothing else; work finished that day is written too, for today. `--no-overdue`
leaves the overdue list out of the export whole, so the day's own deadline is
all that is written — and of that, a deadline already passed, an earlier hour of
that same day included, is dropped too. Being a date's question it changes
nothing in a context export. Within each group the tasks are ordered
by deadline, with those carrying no deadline last. The day, the format and the
destination are all arguments; the format is taken from the file's suffix unless
`--format` says otherwise, and with no destination the text formats go to
standard out:

```bash
gtd export-day                          # today, markdown, to stdout
gtd export-day +3d --format pdf -o week.pdf
gtd export-day 2026-10-05 -o plan.yaml  # the suffix picks YAML
gtd export-day today --format html -o today.html  # pretty, self-contained page
gtd export-day --no-overdue -o today.md  # the day's deadlines, no overdue list
gtd export-day --tag work               # every task in the `work` context
gtd export-context work                 # every task in `work`, to stdout
gtd export-context work home -o both.html  # two contexts at once
```

An export may be scoped to one or more contexts with `--tag NAME` (or its alias
`--context NAME`), or with the `export-context NAME` command. A context-scoped
export is about the context rather than the date: it lists every open task
carrying those contexts, whatever their dates, and consults no date filter at
all — nothing is dropped or separated by when it falls due, and the whole list
sits in one Context group, ordered by deadline. Naming a parent context reaches
its subcontexts too, so `export-context home` covers `home>kitchen`; the `--tag`
flag may be repeated to keep the union of several contexts.

An OmniFocus `import` stamps every imported task with a dated context,
`omnifocus_import_<yyyymmdd>`, so a batch can be found and reviewed together.
Name your own with `--tag`, or pass `--tag ''` to add none.

### Export glyphs and layout

Every export shares one vocabulary of glyphs and styles:

- `[ ]` — the square bullet that opens each action, in markdown and PDF.
- `■` — the same square bullet as a literal glyph in HTML.
- `!` — a flagged (priority) task, set in red in HTML.
- Task names are **bold** throughout (a real bold face in the PDF, `<strong>` in
  HTML, `**…**` in markdown).
- Each action is followed by its details where present: its project
  (`in …`), area, contexts (`#tag`), duration estimate, link, its defer and due
  moments, and any notes.

Both the PDF and the HTML report open with the app's own icon centred above the
date line, so a printed or viewed page is clearly an OmaGTD report. The mark
travels inside the file: the HTML page carries it as a `data:` URI and the PDF
as an image object, neither needing anything beside it. The day's groups print
in order: *Due* first, then *Overdue* — the work the day's defer window swept
up — then *Completed*; a context export prints its one *Context* group, with
every task for the context listed together. The PDF uses the same square bullet, bold task
names and full details on every page; the HTML export renders the same document
as a single self-contained page with a small built-in stylesheet.

Both reports are dressed in the accent the app's palette setting names — the
active desktop theme's accent under `omarchy`, otherwise the preset's own — so
the wordmark, headings and bullets match the window they came from. Only the
accent travels: the page keeps its white paper and dark ink, staying readable
and printable whatever the desktop is wearing.

### Data location

Data lives in `~/.local/share/omagtd/omagtd.sqlite3` (a single sqlite3
database). Point the CLI at a different store with `--db`, or use `--profile`
for a named store under `~/.local/share/omagtd/profiles`.

### Settings

Key preferences are day-counts — empty projects and empty contexts are removed
automatically after their configured number of days, and completed tasks are
kept for a number of days:

```
gtd settings                   # show current values
gtd settings auto_remove_empty_projects 14   # keep empty projects 14 days
gtd settings auto_remove_empty_contexts 7
gtd settings done_retention_days 30
gtd settings theme of-light    # fullscreen palette: omarchy, of-light or of-dark
gtd settings fullscreen_default_view calendar   # view the app opens on: inbox, calendar, today
```

A value of `0` keeps things forever. The bar widget has its own preferences in
the plugin manager (inbox/next counts, compact mode, refresh interval).

The `theme` setting controls the fullscreen overlay palette. The default
`omarchy` follows the active desktop theme; `of-light` and `of-dark` select
OmniFocus-inspired light or dark palettes for the overlay. These palette
choices are local to OmaGTD and do not change Omarchy's shared theme. The bar
pill and quick panel always follow the desktop theme.

`fullscreen_default_view` picks the view the fullscreen app lands on when it
opens — `today` unless you ask for `inbox` or `calendar`. It only names the
starting point; the keyboard still moves anywhere else once the app is up.

## Development

```bash
python3 -m unittest tests.test_gtd   # from the plugin root
```

The bar widget, quick panel, and fullscreen overlay use the `qs.Commons` /
`qs.Ui` modules from the running Omarchy shell. The overlay loads the shared
fullscreen content in that same shell process.

`GtdTheme.qml` exists twice: the root copy is shared by the bar widget and
panel, while `app/GtdTheme.qml` supplies the fullscreen overlay's local palette
and extra preset colors. Keep the status colors and row glyphs aligned.

## License

[MIT](LICENSE)
