"""Batched reads that keep the engine from asking the database once per row.

This layer is deliberately thin and dependency-free: it takes a
:class:`gtd.db.Store` and returns plain dicts, so both :mod:`gtd.core` and
:mod:`gtd.cli` can use it without a cycle. The single-item helpers in
:mod:`gtd.core` remain the source of truth for one item; these answer the same
questions for a batch.
"""

from __future__ import annotations

from .items import paths_for, tags_for
from .stats import counts

__all__ = ["paths_for", "tags_for", "counts"]
