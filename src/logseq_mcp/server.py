"""MCP server exposing a Logseq DB graph, read-only, over stdio."""

from __future__ import annotations

import argparse
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, storage
from .graph import Asset, Graph, parse_date

INSTRUCTIONS = """\
Read-only access to the user's Logseq notes (a Logseq DB graph on this machine).

- Journals are daily pages; most notes live there. Start with get_journal.
- Outlines are indented Markdown. A trailing ‹abcd1234› is a block id: pass it
  to get_block to open that block with everything under it.
- Tasks are blocks tagged #Task (and, if configured, blocks whose name follows
  a pattern such as "web-12"). The same task name can appear on several days.
- Screenshots appear as "[image N]" in the outline and are attached after the
  text in the same order. The text next to an image is often just a caption
  ("this one", "same here"): look at the image before interpreting the note.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)


@dataclass
class Config:
    graphs_dir: Path = storage.DEFAULT_GRAPHS_DIR
    graph: str | None = None
    task_pattern: re.Pattern | None = None
    max_images: int = 8
    max_image_bytes: int = 5 * 1024 * 1024

    @classmethod
    def from_env(cls) -> Config:
        pattern = os.environ.get("LOGSEQ_TASK_PATTERN")
        return cls(
            graphs_dir=Path(os.environ.get("LOGSEQ_GRAPHS_DIR", storage.DEFAULT_GRAPHS_DIR)).expanduser(),
            graph=os.environ.get("LOGSEQ_GRAPH") or None,
            task_pattern=re.compile(pattern) if pattern else None,
            max_images=int(os.environ.get("LOGSEQ_MAX_IMAGES", 8)),
        )


class GraphCache:
    """Loads the graph once and reloads it only when Logseq has written since."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._graph: Graph | None = None
        self._fingerprint: tuple | None = None

    def get(self) -> Graph:
        with self._lock:
            try:
                graph_dir = storage.resolve_graph(self.config.graph, self.config.graphs_dir)
                fp = storage.fingerprint(graph_dir)
                if self._graph is None or fp != self._fingerprint or self._graph.dir != graph_dir:
                    self._graph = Graph(storage.read_datoms(graph_dir), graph_dir)
                    self._fingerprint = fp
            except (FileNotFoundError, ValueError, storage.FormatError) as exc:
                raise ToolError(str(exc)) from exc
            return self._graph


def create_server(config: Config | None = None) -> MCPServer:
    config = config or Config.from_env()
    cache = GraphCache(config)
    server = MCPServer("logseq", instructions=INSTRUCTIONS, version=__version__)

    def with_images(text: str, assets: list[Asset], include: bool) -> list[str | Image]:
        out: list[str | Image] = [text]
        if not include:
            return out
        for n, asset in enumerate(assets, 1):
            if n > config.max_images:
                out.append(f"({len(assets) - config.max_images} more images not attached; use get_image)")
                break
            if not asset.exists:
                continue
            if asset.path.stat().st_size > config.max_image_bytes:
                out.append(f"[image {n}] is too large to attach ({asset.path.stat().st_size // 1024} KB)")
                continue
            out.append(f"[image {n}] {asset.title}  ‹{asset.uuid[:8]}›")
            out.append(Image(path=asset.path))
        return out

    def header(g: Graph, eid: int) -> str:
        parts = []
        if tags := g.context_tags(eid):
            parts.append(" ".join(f"#{t}" for t in tags))
        if status := g.status(eid):
            parts.append(status)
        if day := g.journal_day(eid):
            parts.append(f"journal {day.isoformat()}")
        return " · ".join(parts)

    @server.tool(annotations=READ_ONLY)
    def graph_info() -> str:
        """Which graph is being read, how big it is and which days have journals."""
        g = cache.get()
        days = g.journal_days()
        span = f"{days[0].isoformat()} → {days[-1].isoformat()}" if days else "none"
        return (
            f"graph: {g.name}\nentities: {len(g.e)}\njournals: {len(days)} ({span})\n"
            f"task name pattern: {config.task_pattern.pattern if config.task_pattern else 'none (only #Task)'}"
        )

    @server.tool(annotations=READ_ONLY)
    def get_journal(date: str = "today", include_images: bool = True) -> list[str | Image]:
        """The journal page of one day as an outline, with its screenshots.

        date: "today", "yesterday" or YYYY-MM-DD.
        """
        g = cache.get()
        try:
            day = parse_date(date)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        page = g.journal(day)
        if page is None:
            recent = ", ".join(d.isoformat() for d in g.journal_days()[-5:])
            raise ToolError(f"no journal for {day.isoformat()}. Most recent: {recent}")
        text, assets = g.render(g.children(page))
        return with_images(f"# Journal {day.isoformat()}\n\n{text or '(empty)'}", assets, include_images)

    @server.tool(annotations=READ_ONLY)
    def list_tasks(status: str | None = None, tag: str | None = None, date: str | None = None, limit: int = 50) -> str:
        """Tasks in the graph, oldest first. Filter by status (Todo, Doing, Done...),
        by tag (tags on the task or any parent block) or by journal day."""
        g = cache.get()
        try:
            day = parse_date(date) if date else None
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        found = g.tasks(name_pattern=config.task_pattern, status=status, tag=tag, day=day)
        if not found:
            return "No tasks match."
        lines = [f"{len(found)} tasks" + (f" (showing the last {limit})" if len(found) > limit else "")]
        for eid in found[-limit:]:
            lines.append(f"- {g.summary(eid)}  ‹{g.uuid(eid)[:8]}›  {header(g, eid)}")
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY)
    def get_task(name: str, date: str | None = None, include_images: bool = True) -> list[str | Image]:
        """A block by its exact text (e.g. a task called "web-12") with everything under it.

        Names are often reused on different days; pass date (YYYY-MM-DD) to pick one.
        """
        g = cache.get()
        found = g.blocks_titled(name)
        if date:
            try:
                day = parse_date(date)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            found = [e for e in found if g.journal_day(e) == day]
        if not found:
            raise ToolError(f"no block called {name!r}" + (f" on {date}" if date else ""))
        if len(found) > 1:
            options = "\n".join(f"- ‹{g.uuid(e)[:8]}›  {header(g, e)}" for e in found)
            return [f"{len(found)} blocks are called {name!r}. Pick one with get_block(id) or pass date:\n{options}"]
        return _block(g, found[0], include_images)

    @server.tool(annotations=READ_ONLY)
    def get_block(id: str, include_images: bool = True) -> list[str | Image]:
        """A block by id (the ‹abcd1234› shown in outlines, or a full uuid) with its subtree."""
        g = cache.get()
        eid = g.by_uuid(id.strip("‹› "))
        if eid is None:
            raise ToolError(f"no block with id {id!r}")
        return _block(g, eid, include_images)

    def _block(g: Graph, eid: int, include_images: bool) -> list[str | Image]:
        text, assets = g.render([eid])
        return with_images(f"# {g.summary(eid)}\n{header(g, eid)}\n\n{text}", assets, include_images)

    @server.tool(annotations=READ_ONLY)
    def get_page(title: str, include_images: bool = True) -> list[str | Image]:
        """A regular (non-journal) page by title, case-insensitive."""
        g = cache.get()
        page = g.page(title)
        if page is None:
            raise ToolError(f"no page called {title!r}")
        text, assets = g.render(g.children(page))
        return with_images(f"# {g.title(page)}\n\n{text or '(empty)'}", assets, include_images)

    @server.tool(annotations=READ_ONLY)
    def search(query: str, limit: int = 20) -> str:
        """Blocks whose text contains query (case-insensitive), most recent journals first."""
        g = cache.get()
        hits = g.search(query, limit)
        if not hits:
            return f"Nothing contains {query!r}."
        lines = []
        for eid in hits:
            where = header(g, eid) or (g.title(g.page_of(eid)) if g.page_of(eid) else "")
            lines.append(f"- {g.summary(eid, 150)}  ‹{g.uuid(eid)[:8]}›  {where}")
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY)
    def get_image(id: str) -> Image:
        """One screenshot by the id shown next to it."""
        g = cache.get()
        eid = g.by_uuid(id.strip("‹› "))
        asset = g.asset(eid) if eid is not None else None
        if asset is None:
            raise ToolError(f"no image with id {id!r}")
        if not asset.exists:
            raise ToolError(f"the block points to {asset.path.name}, which is not on disk")
        return Image(path=asset.path)

    return server


def main() -> None:
    p = argparse.ArgumentParser(prog="logseq-mcp", description="Read-only MCP server for a Logseq DB graph.")
    p.add_argument("--graph", help="graph name, if there is more than one (env LOGSEQ_GRAPH)")
    p.add_argument("--graphs-dir", type=Path, help="where the graphs live (env LOGSEQ_GRAPHS_DIR)")
    p.add_argument("--task-pattern", help=r"regex for blocks that are tasks by name, e.g. '[a-z]+-\d+'")
    args = p.parse_args()

    config = Config.from_env()
    if args.graph:
        config.graph = args.graph
    if args.graphs_dir:
        config.graphs_dir = args.graphs_dir.expanduser()
    if args.task_pattern:
        config.task_pattern = re.compile(args.task_pattern)
    create_server(config).run()


if __name__ == "__main__":
    main()
