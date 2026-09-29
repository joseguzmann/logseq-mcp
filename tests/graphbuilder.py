"""Build synthetic Logseq DB graphs for tests.

Writes a ``db.sqlite`` with the same layout Logseq uses: a root row at
``addr=0`` pointing to an ``:eavt`` B-tree (one internal node, several leaves)
and declaring how many datoms it holds. It can also leave stale rows behind,
like Logseq does between garbage collections, and lie about the count, to test
that the reader notices.

No transit cache is emitted: an uncached document is valid transit and the
decoder's cache handling is covered by its own tests.
"""

from __future__ import annotations

import json
import sqlite3
import uuid as uuidlib
from pathlib import Path

TX = 536870913
LEAF_BASE = 1000001

# 1x1 transparent PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082"
)


class GraphBuilder:
    def __init__(self) -> None:
        self.datoms: list[list] = []
        self._next = 100
        self.assets: dict[str, bytes] = {}

    def entity(self, attrs: dict) -> int:
        e = self._next
        self._next += 1
        attrs = {"~:block/uuid": "~u" + str(uuidlib.uuid4()), **attrs}
        for a, v in attrs.items():
            for value in v if isinstance(v, list) else [v]:
                self.datoms.append([e, a, value, TX])
        return e

    def uuid(self, e: int) -> str:
        return next(d[2] for d in self.datoms if d[0] == e and d[1] == "~:block/uuid")[2:]

    # ── Logseq-shaped helpers ────────────────────────────────────────────
    def builtin_class(self, name: str) -> int:
        return self.entity(
            {"~:db/ident": f"~:logseq.class/{name}", "~:block/title": name, "~:block/name": name.lower()}
        )

    def user_tag(self, name: str) -> int:
        return self.entity({"~:db/ident": f"~:user.class/{name}-Xy12Ab34", "~:block/title": name, "~:block/name": name})

    def status_value(self, name: str) -> int:
        slug = name.lower().replace(" ", "-")
        return self.entity({"~:db/ident": f"~:logseq.property/status.{slug}", "~:block/title": name})

    def journal(self, day: int, title: str) -> int:
        return self.entity({"~:block/journal-day": day, "~:block/title": title, "~:block/name": title.lower()})

    def page(self, title: str) -> int:
        return self.entity({"~:block/title": title, "~:block/name": title.lower()})

    def block(self, parent: int, page: int, title: str, order: str, tags=(), status=None, **extra) -> int:
        attrs = {"~:block/parent": parent, "~:block/page": page, "~:block/title": title, "~:block/order": order}
        if tags:
            attrs["~:block/tags"] = list(tags)
        if status:
            attrs["~:logseq.property/status"] = status
        attrs.update(extra)
        return self.entity(attrs)

    def image(self, parent: int, page: int, title: str, order: str, present: bool = True) -> int:
        e = self.block(
            parent,
            page,
            title,
            order,
            **{
                "~:logseq.property.asset/type": "png",
                "~:logseq.property.asset/width": 800,
                "~:logseq.property.asset/height": 600,
            },
        )
        if present:
            self.assets[self.uuid(e)] = PNG
        return e

    # ── serialization ────────────────────────────────────────────────────
    def write(self, graph_dir: Path, *, leaf_size: int = 7, stale: list[list] | None = None, declared_count=None):
        graph_dir.mkdir(parents=True, exist_ok=True)
        (graph_dir / "assets").mkdir(exist_ok=True)
        for u, data in self.assets.items():
            (graph_dir / "assets" / f"{u}.png").write_bytes(data)

        datoms = sorted(self.datoms, key=lambda d: (d[0], d[1]))
        rows = []
        leaves = []
        for i in range(0, len(datoms), leaf_size):
            addr = LEAF_BASE + len(leaves)
            leaves.append(addr)
            rows.append((addr, json.dumps(["^ ", "~:keys", datoms[i : i + leaf_size]]), None))
        internal = LEAF_BASE + len(leaves)
        boundary = [datoms[i] for i in range(0, len(datoms), leaf_size)]
        rows.append((internal, json.dumps(["^ ", "~:keys", boundary]), json.dumps(leaves)))

        # Stale nodes: written by earlier transactions, no longer reachable from the root.
        if stale:
            rows.append((internal + 1, json.dumps(["^ ", "~:keys", stale]), None))

        count = len(datoms) if declared_count is None else declared_count
        root = ["^ ", "~:eavt", internal, "~:eavt-metadata", ["^ ", "~:count", count, "~:shift", 3]]
        rows.append((0, json.dumps(root), None))

        db = graph_dir / "db.sqlite"
        db.unlink(missing_ok=True)
        con = sqlite3.connect(db)
        con.execute("create table kvs (addr INTEGER primary key, content TEXT, addresses JSON)")
        con.executemany("insert into kvs values (?, ?, ?)", rows)
        con.commit()
        con.close()
        return graph_dir


def sample_graph(graphs_dir: Path, name: str = "sample", **write_kwargs) -> tuple[Path, dict]:
    """A small graph covering what the reader has to get right.

    Returns the graph folder and a dict of named entity ids for assertions.
    """
    g = GraphBuilder()
    ids: dict = {}
    task = g.builtin_class("Task")
    g.builtin_class("Journal")
    website = g.user_tag("website")
    garden = g.user_tag("garden")
    todo, doing, done = (g.status_value(s) for s in ("Todo", "Doing", "Done"))

    reading = g.page("Reading list")
    g.block(reading, reading, "Designing Data-Intensive Applications", "a0")
    g.block(reading, reading, "The Pragmatic Programmer", "a1")
    ids["reading"] = reading

    d1 = g.journal(20260920, "Sep 20th, 2026")
    relaunch = g.block(d1, d1, "Website relaunch", "a0", tags=[website])
    web1 = g.block(relaunch, d1, "web-1", "a0", tags=[task], status=doing)
    blurry = g.block(web1, d1, "Hero image is blurry on mobile", "a0")
    ids["image"] = g.image(blurry, d1, "this one", "a0")
    g.block(web1, d1, "Contact form sends twice\nonly on Safari", "a1")
    web2 = g.block(relaunch, d1, "web-2", "a1")  # task by name only, no #Task tag
    g.block(web2, d1, f"Check [[{g.uuid(reading)}]] before shipping", "a0")
    g.block(d1, d1, "Water the tomatoes", "a1", tags=[garden, task], status=done)
    # Logseq orders siblings with fractional-index strings: "a0" < "a0V" < "a1".
    g.block(d1, d1, "Buy seeds", "a0V", tags=[garden])
    g.block(d1, d1, "", "a2")  # empty trailing block, Logseq leaves these around
    g.block(d1, d1, "", "a3", tags=[task], status=todo)  # a task created but never written

    d2 = g.journal(20260921, "Sep 21st, 2026")
    followup = g.block(d2, d2, "Website follow-up", "a0", tags=[website])
    web1_again = g.block(followup, d2, "web-1", "a0", tags=[task], status=todo)
    ids["missing_image"] = g.image(web1_again, d2, "gone", "a0", present=False)

    ids.update(d1=d1, d2=d2, web1=web1, web1_again=web1_again, web2=web2, relaunch=relaunch)
    ids["uuid"] = g.uuid

    stale = [[web1, "~:block/title", "web-1 half typ", TX - 1], [9999, "~:block/title", "ghost block", TX - 1]]
    graph_dir = g.write(graphs_dir / name, stale=stale, **write_kwargs)
    return graph_dir, ids
