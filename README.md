# OmaGTD

Getting Things Done for [Omarchy](https://omarchy.org/), modelled on OmniFocus.
A keyboard-driven GTD system that lives in the bar and the shell: capture into
an inbox, clarify, organize, reflect, and engage — projects/areas, contexts
(tags), waiting-for, someday/maybe, recurring actions, and an internal
calendar, backed by a sqlite3 database and scriptable through a `gtd` CLI.

## Human-generated content

TODO

## What's included

- **Bar pill** — inbox and next-action counts, with a compact glyph-only mode.
- **Panel** — a quick overlay for what is due today, driven entirely by the
  keyboard.
- **Fullscreen view** — the whole five-step workflow in one window (see
  *Standalone app* below).
- **`gtd` CLI** — every step is scriptable, so it can be bound to a hotkey.

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

## The standalone app

The full five-step workflow opens as a real window, independent of the bar. It
runs in its own quickshell process (so it survives shell restarts) and is
launched through `bin/omagtd-app` inside the plugin folder:

```bash
~/.config/omarchy/plugins/com.auino.omagtd/bin/omagtd-app            # open the app
~/.config/omarchy/plugins/com.auino.omagtd/bin/omagtd-app view today # open on a view
~/.config/omarchy/plugins/com.auino.omagtd/bin/omagtd-app quit       # quit the app
```

To launch it like a normal program, symlink it onto your PATH and install the
desktop entry:

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
`import`, `export`, `db`.

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
```

A value of `0` keeps things forever. The bar widget has its own preferences in
the plugin manager (inbox/next counts, compact mode, refresh interval).

The `theme` setting only affects the standalone fullscreen app, which paints
itself in its own palette. The default `omarchy` follows the active desktop
theme; `of-light` and `of-dark` pin it to an OmniFocus-inspired light or dark
palette (backgrounds, text, accents and status colours, plus a proportional
font and rounded corners). The bar pill and the panel always follow the desktop
theme.

## Development

```bash
python3 -m unittest tests.test_gtd   # from the plugin root
```

The UI follows Omarchy's shell module conventions: the bar widget and panel
use the `qs.Commons` / `qs.Ui` modules that Omarchy ships with the shell. The
standalone app vendors copies of those modules as plain directories
(`app/Commons` and `app/Ui`, with their `qmldir`s) so it can run in its own
quickshell process, where the shell-shipped modules are not loaded.

`GtdTheme.qml` exists twice: the root copy is shared by the bar widget and
panel, while `app/GtdTheme.qml` is the fullscreen app's own and carries the
extra preset machinery. Keep the two in step when changing the status colours
or the row glyphs.

## License

[MIT](LICENSE)
