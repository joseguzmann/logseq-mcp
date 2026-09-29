"""The Logseq graph as blocks, pages, journals, tasks and images.

Built from the datoms returned by :mod:`logseq_mcp.storage`. Attribute names
are kept as transit keywords (``"~:block/title"``); references to other
entities are plain integers.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .storage import Datom

TITLE = "~:block/title"
NAME = "~:block/name"
PARENT = "~:block/parent"
ORDER = "~:block/order"
PAGE = "~:block/page"
TAGS = "~:block/tags"
UUID = "~:block/uuid"
JOURNAL_DAY = "~:block/journal-day"
IDENT = "~:db/ident"
STATUS = "~:logseq.property/status"
ASSET_TYPE = "~:logseq.property.asset/type"
ASSET_WIDTH = "~:logseq.property.asset/width"
ASSET_HEIGHT = "~:logseq.property.asset/height"

BUILT_IN = "~:logseq.property/built-in?"

TASK_IDENT = "~:logseq.class/Task"
USER_TAG_PREFIX = "~:user.class/"

_REF = re.compile(r"\[\[([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]\]")


@dataclass(frozen=True)
class Asset:
    uuid: str
    title: str
    path: Path
    type: str
    width: int | None
    height: int | None

    @property
    def exists(self) -> bool:
        return self.path.is_file()


class Graph:
    def __init__(self, datoms: Iterable[Datom], graph_dir: Path) -> None:
        self.dir = graph_dir
        self.name = graph_dir.name
        # Some attributes (tags, refs) have many values, so every value is a list.
        self.e: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for d in datoms:
            values = self.e[d.e][d.a]
            if d.v not in values:
                values.append(d.v)
        self.e = {k: dict(v) for k, v in self.e.items()}

        self._children: dict[int, list[int]] = defaultdict(list)
        self._by_uuid: dict[str, int] = {}
        for eid, attrs in self.e.items():
            parent = attrs.get(PARENT, [None])[0]
            if isinstance(parent, int):
                self._children[parent].append(eid)
            u = attrs.get(UUID, [None])[0]
            if isinstance(u, str):
                self._by_uuid[u.removeprefix("~u")] = eid
        for kids in self._children.values():
            kids.sort(key=lambda k: str(self.v(k, ORDER, "")))

    # ── raw access ───────────────────────────────────────────────────────
    def v(self, eid: int, attr: str, default=None):
        values = self.e.get(eid, {}).get(attr)
        return values[0] if values else default

    def vs(self, eid: int, attr: str) -> list:
        return self.e.get(eid, {}).get(attr, [])

    # ── identity and text ────────────────────────────────────────────────
    def uuid(self, eid: int) -> str:
        return str(self.v(eid, UUID, "")).removeprefix("~u")

    def by_uuid(self, uuid_or_prefix: str) -> int | None:
        """Accept a full uuid or a unique prefix (8+ chars), as shown in outlines."""
        key = uuid_or_prefix.strip().lower()
        if key in self._by_uuid:
            return self._by_uuid[key]
        if len(key) < 8:
            return None
        matches = [e for u, e in self._by_uuid.items() if u.startswith(key)]
        return matches[0] if len(matches) == 1 else None

    def raw_title(self, eid: int) -> str:
        t = self.v(eid, TITLE, "")
        return t if isinstance(t, str) else ""

    def title(self, eid: int) -> str:
        """The block text, with ``[[uuid]]`` references replaced by the page title."""

        def resolve(m: re.Match) -> str:
            target = self._by_uuid.get(m.group(1))
            return f"[[{self.raw_title(target)}]]" if target is not None else m.group(0)

        return _REF.sub(resolve, self.raw_title(eid))

    def summary(self, eid: int, width: int = 120) -> str:
        """The first line of the block, for one-line listings. Blocks can be empty."""
        first = self.title(eid).strip().split("\n", 1)[0]
        return (first[: width - 1] + "…" if len(first) > width else first) or "(untitled)"

    # ── structure ────────────────────────────────────────────────────────
    def children(self, eid: int) -> list[int]:
        return self._children.get(eid, [])

    def parent(self, eid: int) -> int | None:
        p = self.v(eid, PARENT)
        return p if isinstance(p, int) else None

    def page_of(self, eid: int) -> int | None:
        p = self.v(eid, PAGE)
        return p if isinstance(p, int) else None

    def is_page(self, eid: int) -> bool:
        return NAME in self.e.get(eid, {}) and PAGE not in self.e.get(eid, {})

    def journal_day(self, eid: int) -> dt.date | None:
        """The day of a journal page, or of the journal page a block lives in."""
        page = eid if self.is_page(eid) else self.page_of(eid)
        day = self.v(page, JOURNAL_DAY) if page is not None else None
        return _int_to_date(day) if day else None

    # ── tags, status, tasks ──────────────────────────────────────────────
    def tags(self, eid: int) -> list[str]:
        return [self.raw_title(t) for t in self.vs(eid, TAGS) if t in self.e]

    def is_user_tag(self, eid: int) -> bool:
        return str(self.v(eid, IDENT, "")).startswith(USER_TAG_PREFIX)

    def context_tags(self, eid: int) -> list[str]:
        """User tags on the block or any ancestor, nearest first.

        People tag the parent ("Website relaunch #website") and write the
        tasks below it, so a task belongs to the tags of its ancestors too.
        """
        out: list[str] = []
        seen: set[int] = set()
        cur: int | None = eid
        while cur is not None and cur not in seen:
            seen.add(cur)
            for t in self.vs(cur, TAGS):
                if self.is_user_tag(t) and self.raw_title(t) not in out:
                    out.append(self.raw_title(t))
            cur = self.parent(cur)
        return out

    def status(self, eid: int) -> str | None:
        s = self.v(eid, STATUS)
        return self.raw_title(s) if isinstance(s, int) else None

    def is_task(self, eid: int, name_pattern: re.Pattern | None = None) -> bool:
        if any(self.v(t, IDENT) == TASK_IDENT for t in self.vs(eid, TAGS)):
            return True
        return bool(name_pattern and name_pattern.fullmatch(self.raw_title(eid).strip()))

    def tasks(
        self,
        *,
        name_pattern: re.Pattern | None = None,
        status: str | None = None,
        tag: str | None = None,
        day: dt.date | None = None,
    ) -> list[int]:
        out = []
        for eid in self.e:
            if self.is_page(eid) or not self.is_task(eid, name_pattern):
                continue
            if status and (self.status(eid) or "").lower() != status.lower():
                continue
            if tag and tag.lower().lstrip("#") not in (t.lower() for t in self.context_tags(eid)):
                continue
            if day and self.journal_day(eid) != day:
                continue
            out.append(eid)
        return sorted(out, key=lambda x: (self.journal_day(x) or dt.date.min, self.raw_title(x)))

    # ── lookups ──────────────────────────────────────────────────────────
    def journal(self, day: dt.date) -> int | None:
        key = int(day.strftime("%Y%m%d"))
        return next((e for e in self.e if self.v(e, JOURNAL_DAY) == key), None)

    def journal_days(self) -> list[dt.date]:
        return sorted(_int_to_date(d) for a in self.e.values() for d in a.get(JOURNAL_DAY, []))

    def page(self, title: str) -> int | None:
        key = title.strip().lower()
        return next((e for e in self.e if self.is_page(e) and self.v(e, NAME) == key), None)

    def blocks_titled(self, title: str) -> list[int]:
        key = title.strip()
        return [e for e in self.e if not self.is_page(e) and self.raw_title(e).strip() == key]

    def search(self, text: str, limit: int = 20) -> list[int]:
        q = text.lower()
        hits = [e for e in self.e if not self._is_schema(e) and q in self.title(e).lower()]
        hits.sort(key=lambda x: self.journal_day(x) or dt.date.min, reverse=True)
        return hits[:limit]

    def _is_schema(self, eid: int) -> bool:
        """Classes, properties and Logseq's own built-in pages: not user content."""
        attrs = self.e[eid]
        return IDENT in attrs or bool(attrs.get(BUILT_IN, [False])[0])

    # ── assets ───────────────────────────────────────────────────────────
    def asset(self, eid: int) -> Asset | None:
        kind = self.v(eid, ASSET_TYPE)
        if not kind:
            return None
        u = self.uuid(eid)
        return Asset(
            uuid=u,
            title=self.raw_title(eid),
            path=self.dir / "assets" / f"{u}.{kind}",
            type=str(kind),
            width=self.v(eid, ASSET_WIDTH),
            height=self.v(eid, ASSET_HEIGHT),
        )

    # ── rendering ────────────────────────────────────────────────────────
    def render(self, roots: Iterable[int], *, ids: bool = True) -> tuple[str, list[Asset]]:
        """An indented Markdown outline plus the images found in it, in order.

        Images appear inline as ``[image N]`` so the text around them ("this
        one", "same here") keeps pointing at the right screenshot.
        """
        lines: list[str] = []
        images: list[Asset] = []

        def walk(eid: int, depth: int) -> None:
            pad = "  " * depth
            asset = self.asset(eid)
            if asset:
                images.append(asset)
                missing = "" if asset.exists else "  ⚠ file missing"
                lines.append(f"{pad}- [image {len(images)}] {asset.title}{missing}")
            else:
                # Continuation lines of a multi-line block stay inside the bullet.
                text = self.title(eid).rstrip().replace("\n", "\n" + pad + "  ")
                status = self.status(eid)
                parts = [text] if text else []
                if status:
                    parts.append(f"[{status}]")
                parts += [f"#{t}" for t in self.tags(eid) if t != "Task"]
                if parts:
                    line = f"{pad}- {' '.join(parts)}"
                    if ids and (status or self.is_task(eid) or depth == 0):
                        line += f"  ‹{self.uuid(eid)[:8]}›"
                    lines.append(line)
                else:
                    depth -= 1  # empty container block: don't spend an indent level
            for child in self.children(eid):
                walk(child, depth + 1)

        for root in roots:
            walk(root, 0)
        return "\n".join(lines), images


def _int_to_date(n: int) -> dt.date:
    return dt.date(n // 10000, n // 100 % 100, n % 100)


def parse_date(text: str | None, today: dt.date | None = None) -> dt.date:
    """``today``, ``yesterday``, ``tomorrow``, ``YYYY-MM-DD`` or ``YYYYMMDD``."""
    today = today or dt.date.today()
    s = (text or "today").strip().lower()
    relative = {"today": 0, "hoy": 0, "yesterday": -1, "ayer": -1, "tomorrow": 1, "mañana": 1}
    if s in relative:
        return today + dt.timedelta(days=relative[s])
    m = re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})", s)
    if not m:
        raise ValueError(f"unrecognized date {text!r}; use YYYY-MM-DD, 'today' or 'yesterday'")
    return dt.date(*map(int, m.groups()))
