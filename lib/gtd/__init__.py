"""GTD for Omarchy — a sqlite3 task system modelled on OmniFocus.

The public surface is :mod:`gtd.cli`; everything else is the engine.

Layout:

* :mod:`gtd.db`      connections, schema, ids, timestamps
* :mod:`gtd.dates`   human/relative date parsing and formatting
* :mod:`gtd.quick`   OmniFocus-style quick-entry syntax
* :mod:`gtd.core`    the workflow: capture, clarify, organize, engage
* :mod:`gtd.review`  daily and weekly reflection, review counters
* :mod:`gtd.importer` OmniFocus / CSV / JSON import
"""

__version__ = "1.0.0"
