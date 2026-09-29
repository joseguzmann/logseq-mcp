# logseq-mcp

Lets your AI agent read your **Logseq notes, screenshots included**, straight from
the graph on disk. No running app, no HTTP API, no token, and no way to modify a
single note: it is read-only by construction.

You say *"do web-12 from yesterday's call"* and the agent opens the note itself:

```
get_task("web-12", date="2026-09-28")

- web-12 [Doing]  ‹3f9c21d7›
  - The hero image is blurry on mobile
    - [image 1] this one
  - Contact form sends twice
[image 1] ← the screenshot itself, as an image the model can see
```

---

## Why it exists

I take notes in Logseq all day. In client meetings each request becomes a block
named after the project (`web-12`), with screenshots pasted underneath and
captions like *"this one"* or *"same here"*. Those notes are the real spec: the
screenshot says **which** button, the text says what is wrong with it.

Then I open my coding agent and explain it all over again, and the detail is
lost in the retelling. I wanted the agent to read the note itself, and above all
to **look at the screenshots**, because the caption alone means nothing. A
sentence that sounds perfectly clear, "not the one on top", is ambiguous on a
screen with three similar controls. The screenshot settles it.

Logseq's new DB version no longer stores Markdown files, so there is nothing to
grep. The existing Logseq MCP servers go through the app's local HTTP API, which
means keeping Logseq open, the API server started and a token configured. I
wanted something that works from any terminal, with Logseq closed, and that
cannot touch my notes by accident.

## What it does

- **Journals, tasks, pages and blocks** as indented Markdown outlines, in the
  order you wrote them.
- **Screenshots come attached** as image content, numbered where they appear in
  the outline, so "this one" keeps pointing at the right picture.
- **Tasks** by status, by tag (on the task or any parent block) or by day. The
  same task name on several days is expected, not an error.
- **Reads while Logseq is running**, including the notes from the last minutes.
- **Read-only.** It never opens the real database for writing: it reads a copy.
- **No network.** The graph never leaves the machine.

## How it works

```
  db.sqlite + WAL ──► snapshot ──► kvs B-tree from the root ──► transit ──► datoms
   (locked by Logseq)   (copy)       (current state only)        decode        │
                                                                               ▼
                                             MCP tools ◄── outline + images ◄── graph
```

Logseq DB keeps a [datascript](https://github.com/tonsky/datascript) database
serialized into a single SQLite table, `kvs(addr, content, addresses)`. There is
no public spec for that layout. A reader for an undocumented format mostly fails
**quietly**: the output looks fine and is wrong. Most of this code exists to turn
quiet failures into loud ones.

### Only the current state

`kvs` is a persistent B-tree. Every write adds new nodes and leaves the old ones
behind until Logseq garbage-collects them. The obvious approach, decoding every
row, works on the first try and is wrong: it resurrects half-typed blocks that no
longer exist, mixed with the real ones, and nothing looks off.

The row at `addr = 0` is the root. It holds the address of each index and, in
`:eavt-metadata`, **how many datoms the index must contain**. The reader walks
`:eavt` from the root down to the leaves, ignores every row that is not
reachable, and counts. If the count does not match what the root declares, it
stops: a Logseq update changed the format, and a tree that looks complete and
isn't is worse than an error.

### Transit's string cache

Each row is a transit-json document. Transit replaces strings it has already seen
with back-references like `"^1"`, and map keys enter that cache **before** their
values. Decode a pair in a single Python expression, `out[key()] = value()`, and
the right-hand side runs first: the cache fills in the wrong order, every later
reference shifts by one, and attribute names come out where values should be.
The decoder does it in two statements and there is a test for exactly that case.
As a second guard, any datom whose attribute is not a keyword aborts the read.

### Reading a database that is still open

Logseq holds the file while it runs.

**`VACUUM INTO`** was the first attempt: `database is locked`.

**SQLite's backup API** was the second: it blocks, waiting for a lock that never
comes.

**What shipped: copy the files and read the copy.** It has to be `db.sqlite`
**together with its WAL**, because the last minutes of notes live in the WAL
until a checkpoint. Without it the reader returns a graph that is slightly in
the past, with no sign of it. A hot copy can catch Logseq mid-write, so the copy
goes through `integrity_check` and is retried once.

The server keeps the decoded graph in memory and reloads it only when the size
or modification time of the database or its WAL has changed.

### Screenshots are part of the note

Image blocks point to `assets/<uuid>.<ext>`. The outline marks them as
`[image N]` right where they sit, and the files are attached after the text in
the same order, so the model reads the caption and sees the picture together.
Up to 8 per call. The rest are one `get_image` away, with their ids in the
outline.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Tested on macOS with
Logseq 2.0.1.

```sh
git clone https://github.com/joseguzmann/logseq-mcp.git
```

In **Claude Code**:

```sh
claude mcp add logseq -- uv run --directory /path/to/logseq-mcp logseq-mcp
```

In **Claude Desktop, Cursor** or any other MCP client:

```json
"logseq": {
  "command": "uv",
  "args": ["run", "--directory", "/path/to/logseq-mcp", "logseq-mcp"]
}
```

| Flag | Env var | Default |
|---|---|---|
| `--graphs-dir` | `LOGSEQ_GRAPHS_DIR` | `~/logseq/graphs` |
| `--graph` | `LOGSEQ_GRAPH` | the only graph, if there is just one |
| `--task-pattern` | `LOGSEQ_TASK_PATTERN` | none: only blocks tagged `#Task` are tasks |
|  | `LOGSEQ_MAX_IMAGES` | `8` |

> **A trap worth knowing.** Not every task is tagged `#Task`. If you name tasks
> the way I do and only sometimes tag them, `--task-pattern '[a-z][a-z-]*-\d+'`
> makes `web-12` count as a task too. Without it, those tasks simply don't show up
> in `list_tasks`, and nothing says why.

## Tools

All of them are annotated read-only.

| Tool | Returns |
|---|---|
| `get_journal(date="today")` | A day's journal, with its screenshots. |
| `get_task(name, date?)` | A block by its exact text, with its subtree. If the name repeats, the candidates instead of a guess. |
| `get_block(id)` | A block by the `‹id›` shown in any outline. |
| `list_tasks(status?, tag?, date?)` | Tasks, filtered by status, tag or day. |
| `search(query)` | Blocks containing a text, most recent first. |
| `get_page(title)` | A regular page. |
| `get_image(id)` | One screenshot. |
| `graph_info()` | Which graph is being read and what is in it. |

## What to expect

Measured on my own graph: 16,354 datoms, 1,557 entities, 43 journals.

| Call | Time |
|---|---|
| First call (snapshot + decode + build) | ~60 ms |
| Listing or search, graph already loaded | < 10 ms |
| A day with 8 screenshots attached (~4 MB) | ~0.6 s |

Every journal in that graph was cross-checked against an independent reader,
block by block. The test suite itself runs on synthetic graphs, so it needs
neither Logseq nor anybody's notes.

## Layout

| File | What it solves |
|---|---|
| `transit.py` | Decoding transit-json, including the string cache and its ordering |
| `storage.py` | Snapshotting a locked database, walking the index, refusing partial data |
| `graph.py` | Blocks, pages, journals, inherited tags, task status, image files, outlines |
| `server.py` | The MCP tools, and reloading only when Logseq has written |
| `tests/graphbuilder.py` | Synthetic graphs with the real on-disk layout, stale rows and all |

## Limitations

- **DB graphs only.** For the classic Markdown version, the files are a better
  source than any server.
- **Read-only, on purpose.** Writing to Logseq's internal format behind the app's
  back is how you corrupt a graph.
- **The format is internal** and can change with any Logseq release. When it
  does, the reader stops with a clear error instead of returning partial data.
- **Block references** inside text are resolved to page titles; queries, embeds
  and properties other than status and tags are not rendered.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

## License

MIT.
