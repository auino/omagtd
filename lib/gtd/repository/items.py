"""Bulk item enrichment.

The per-row helpers in :mod:`gtd.core` (`tags_of`, `container_path`) are right
for a single item, but a list or a tree that calls them once per row is N+1
queries. Everything here answers the same questions for a whole batch in one
round trip, so a 200-row page costs two queries instead of four hundred.

SQLite caps the number of bound variables per statement, and the cap differs
between builds, so ids are chunked well under the common floor.
"""

from __future__ import annotations

from collections import defaultdict

from ..db import Store

# Comfortably below SQLite's historical 999-variable floor.
_CHUNK = 500


def _chunks(values: list[str], size: int = _CHUNK):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _marks(count: int) -> str:
    return ",".join("?" * count)


def tags_for(store: Store, item_ids) -> dict[str, list[str]]:
    """`item_id -> [context names]` for a whole batch, in one query per chunk.

    Names come back sorted the way `tags_of` sorts them, so a caller can swap
    one for the other without the order changing.
    """
    ids = list(dict.fromkeys(str(i) for i in item_ids if i))
    out: dict[str, list[str]] = {i: [] for i in ids}
    for chunk in _chunks(ids):
        rows = store.query(
            "SELECT it.item_id AS item_id, t.name AS name "
            "FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            f"WHERE it.item_id IN ({_marks(len(chunk))}) "
            "ORDER BY t.name COLLATE NOCASE",
            chunk,
        )
        for row in rows:
            out.setdefault(row["item_id"], []).append(row["name"])
    return out


# The parent chain a batch of items sits in, walked by the database once rather
# than by Python once per item. Depth is bounded so a malformed cycle cannot
# spin forever; `container_path` uses the same 64-hop ceiling.
_PATH_SQL = (
    "WITH RECURSIVE anc(start_id, node_id, title, parent_id, depth) AS ("
    "  SELECT id, id, title, parent_id, 0 FROM items WHERE id IN ({marks})"
    "  UNION ALL "
    "  SELECT a.start_id, p.id, p.title, p.parent_id, a.depth + 1 "
    "  FROM anc a JOIN items p ON p.id = a.parent_id WHERE a.depth < 63)"
    " SELECT start_id, title, depth FROM anc WHERE depth > 0 "
    " ORDER BY start_id, depth DESC"
)


def paths_for(store: Store, item_ids) -> dict[str, str]:
    """`item_id -> "Area>Project"` ancestry for a batch, one query per chunk.

    The path is the item's containers top-down and does not include the item
    itself, matching `core.container_path`. An item filed nowhere maps to "".
    """
    ids = list(dict.fromkeys(str(i) for i in item_ids if i))
    out: dict[str, str] = {i: "" for i in ids}
    for chunk in _chunks(ids):
        rows = store.query(_PATH_SQL.format(marks=_marks(len(chunk))), chunk)
        chains: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            chains[row["start_id"]].append(row["title"])
        for item_id, titles in chains.items():
            out[item_id] = ">".join(titles)
    return out
