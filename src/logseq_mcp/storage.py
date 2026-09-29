"""Read the current state of a Logseq DB graph straight from its SQLite file.

Logseq's DB version keeps a datascript database serialized into a single table,
``kvs(addr, content, addresses)``. Each row is a node of a persistent B-tree:

* ``addr = 0`` is the root. It holds the address of each index (``:eavt``,
  ``:aevt``, ``:avet``) and, in ``:eavt-metadata``, how many datoms the index
  must contain.
* Internal nodes list their children in ``addresses``.
* Leaves hold the datoms, as ``[entity, attribute, value, tx]`` tuples.

The tree is *persistent*: writes add new nodes and leave old ones behind until
Logseq garbage-collects them. Reading every row mixes the current state with
stale intermediate states (half-typed blocks that no longer exist). The only
trustworthy state is what hangs from the root, and the root tells us how many
datoms that is, so we check it.

Nothing here ever writes to the graph. Logseq keeps the file locked while it
runs (``VACUUM INTO`` fails, the backup API blocks), so we copy the database
plus its WAL to a temp dir and read the copy.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import transit

DEFAULT_GRAPHS_DIR = Path("~/logseq/graphs").expanduser()
DB_FILE = "db.sqlite"


class FormatError(RuntimeError):
    """The graph does not look like what this reader understands.

    Raised instead of returning partial data. A Logseq update that changes the
    storage format should stop the reader, not produce a tree that looks
    complete and isn't.
    """


@dataclass(frozen=True)
class Datom:
    e: int
    a: str
    v: Any
    tx: int


def list_graphs(graphs_dir: Path = DEFAULT_GRAPHS_DIR) -> list[str]:
    if not graphs_dir.is_dir():
        return []
    return sorted(d.name for d in graphs_dir.iterdir() if (d / DB_FILE).is_file())


def resolve_graph(name: str | None = None, graphs_dir: Path = DEFAULT_GRAPHS_DIR) -> Path:
    """Pick a graph folder. With a single graph, the name is optional."""
    graphs = list_graphs(graphs_dir)
    if not graphs:
        raise FileNotFoundError(f"no Logseq DB graph (a folder with {DB_FILE}) under {graphs_dir}")
    if name:
        if name not in graphs:
            raise FileNotFoundError(f"graph {name!r} not found. Available: {', '.join(graphs)}")
        return graphs_dir / name
    if len(graphs) > 1:
        raise ValueError(f"several graphs found ({', '.join(graphs)}); choose one by name")
    return graphs_dir / graphs[0]


def fingerprint(graph_dir: Path) -> tuple:
    """Changes whenever Logseq writes: the WAL moves before the main file does."""
    out = []
    for suffix in ("", "-wal"):
        p = graph_dir / (DB_FILE + suffix)
        st = p.stat() if p.exists() else None
        out.append((st.st_mtime_ns, st.st_size) if st else None)
    return tuple(out)


def _snapshot_rows(graph_dir: Path) -> dict[int, tuple[str, str | None]]:
    tmp = Path(tempfile.mkdtemp(prefix="logseq-mcp-"))
    try:
        src, dst = graph_dir / DB_FILE, tmp / DB_FILE
        # The WAL is not optional: without it we would lose the latest edits.
        for suffix in ("", "-wal", "-shm"):
            if Path(str(src) + suffix).exists():
                shutil.copy2(str(src) + suffix, str(dst) + suffix)
        con = sqlite3.connect(dst)
        try:
            if con.execute("pragma integrity_check").fetchone()[0] != "ok":
                raise FormatError("inconsistent snapshot")
            return {a: (c, d) for a, c, d in con.execute("select addr, content, addresses from kvs")}
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rows(graph_dir: Path) -> dict[int, tuple[str, str | None]]:
    # A hot copy can catch Logseq mid-write. One retry, then give up loudly.
    try:
        return _snapshot_rows(graph_dir)
    except (FormatError, sqlite3.DatabaseError):
        try:
            return _snapshot_rows(graph_dir)
        except (FormatError, sqlite3.DatabaseError) as exc:
            raise FormatError(f"could not take a consistent snapshot of the graph: {exc}") from exc


def read_datoms(graph_dir: Path) -> list[Datom]:
    """Every datom of the current state of the graph, verified against the root."""
    rows = _rows(graph_dir)
    if 0 not in rows:
        raise FormatError("the graph has no root row (addr=0); cannot tell what is current")
    try:
        root = transit.loads(rows[0][0])
    except (ValueError, transit.TransitError) as exc:
        raise FormatError(f"cannot decode the root row: {exc}") from exc
    if not isinstance(root, dict) or "~:eavt" not in root:
        raise FormatError("the root row has no :eavt index")

    tuples: list[list] = []
    seen: set[int] = set()
    stack = [root["~:eavt"]]
    while stack:
        addr = stack.pop()
        if addr in seen:
            continue
        seen.add(addr)
        if addr not in rows:
            raise FormatError(f"the index points to a missing node ({addr})")
        content, addresses = rows[addr]
        children = json.loads(addresses) if addresses else []
        if children:
            # Internal nodes repeat their children's boundary keys; counting
            # them would double some datoms. Only leaves hold data.
            stack.extend(reversed(children))
            continue
        try:
            node = transit.loads(content)
        except (ValueError, transit.TransitError) as exc:
            raise FormatError(f"cannot decode index node {addr}: {exc}") from exc
        if not isinstance(node, dict) or "~:keys" not in node:
            raise FormatError(f"index node {addr} is not a leaf with :keys")
        tuples.extend(k for k in node["~:keys"] if isinstance(k, list) and len(k) >= 4)

    expected = root.get("~:eavt-metadata", {}).get("~:count")
    if expected is not None and len(tuples) != expected:
        raise FormatError(
            f"the index holds {len(tuples)} datoms but the root declares {expected}. "
            "The storage format probably changed; refusing to return partial data."
        )

    bad = sum(1 for t in tuples if not (isinstance(t[0], int) and isinstance(t[1], str) and t[1].startswith("~:")))
    if bad:
        raise FormatError(f"{bad} datoms have an invalid entity or attribute; the transit cache is misaligned")

    return [Datom(t[0], t[1], t[2], t[3]) for t in tuples]


def graph_stats(datoms: list[Datom]) -> dict[str, int]:
    return {"datoms": len(datoms), "entities": len({d.e for d in datoms})}
