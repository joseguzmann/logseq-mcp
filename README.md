# logseq-mcp

**Give your AI coding agent your Logseq notes, screenshots included.**

A read-only [MCP](https://modelcontextprotocol.io) server for the **DB version of Logseq**.
It reads the graph's SQLite file directly: no running app, no HTTP API, no token.

```
you › fix web-12 from yesterday's call

agent › get_task("web-12", date="2026-09-28")
        # web-12
        #website · Doing · journal 2026-09-28

        - web-12 [Doing]  ‹3f9c21d7›
          - The hero image is blurry on mobile
            - [image 1] this one
          - Contact form sends twice
        [image 1] <the screenshot itself>
```

## Why I built it

I take notes in Logseq all day. In client meetings I jot down each request as a
block named after the project (`web-12`), and paste screenshots under it with
captions like *"this one"* or *"same here"*. Those notes are the real spec:
the screenshot says which button, the text says what's wrong with it.

Then I open my coding agent and have to explain it all over again, which loses
the detail. I wanted to say *"do web-12"* and have the agent read the note itself,
**and look at the screenshots**, because the caption alone means nothing.

Logseq's new DB version doesn't store Markdown files anymore, so there's nothing
to grep. Existing Logseq MCP servers talk to the app's local HTTP API, which means
keeping Logseq open with the API server started and a token configured.
I wanted something that just works, from any terminal, even with Logseq closed,
and that can't modify my notes by accident. So I read the storage format directly.

## How it works

Logseq DB keeps a [datascript](https://github.com/tonsky/datascript) database
serialized into a single SQLite table, `kvs(addr, content, addresses)`. There is
no public spec for that layout; this is what the reader relies on:

```
 kvs row addr=0  ── root: address of each index + how many datoms :eavt holds
      │
      ▼
 :eavt B-tree ─ internal nodes (children in `addresses`)
      │
      ▼
 leaves ─ ["^ ", "~:keys", [[entity, attribute, value, tx], ...]]   ← transit-json
```

The code is in layers, each usable on its own:

| Layer | File | What it does |
|---|---|---|
| Transit | [`transit.py`](src/logseq_mcp/transit.py) | A small transit-json decoder: the subset Logseq emits, including its string cache. |
| Storage | [`storage.py`](src/logseq_mcp/storage.py) | Snapshots the SQLite file (plus WAL), walks the index from the root and checks it against the datom count the root declares. |
| Graph | [`graph.py`](src/logseq_mcp/graph.py) | Turns datoms into pages, journals, block trees, tags, task status and image files. |
| Server | [`server.py`](src/logseq_mcp/server.py) | The MCP tools, with a cache that reloads only when Logseq has written. |

### Pitfalls this handles

A reader for an undocumented format mostly fails *quietly*: the output looks fine
and is wrong. Most of this code exists to turn quiet failures into loud ones.

- **Stale rows.** `kvs` is a persistent tree: every write leaves old nodes behind
  until Logseq garbage-collects them. Reading every row resurrects half-typed
  blocks that no longer exist. The reader only follows what hangs from the root,
  and **refuses to answer** if the number of datoms doesn't match the root's count.
- **Transit's string cache.** Repeated strings become back-references (`"^1"`),
  and map keys are cached before their values. Decode a pair in one Python
  expression (`out[k()] = v()`) and the right-hand side runs first: every later
  reference shifts by one and attribute names come out as values. There's a test
  for exactly that.
- **A locked, live database.** Logseq holds the file while it runs (`VACUUM INTO`
  fails, the backup API blocks). The reader copies `db.sqlite` **with its WAL**
  (without it you lose the last minutes of notes), checks the copy's integrity
  and retries once.
- **Order.** Siblings are ordered by fractional-index strings (`a0 < a0V < a1`),
  not by creation time.
- **Reused names.** The same task name shows up on several days. Asking for it by
  name returns the candidates instead of guessing.

## Tools

All tools are annotated read-only.

| Tool | Returns |
|---|---|
| `get_journal(date="today")` | A day's journal as an outline, with its screenshots attached. |
| `get_task(name, date?)` | A block by its exact text with its subtree. Asks which one if the name repeats. |
| `get_block(id)` | A block by the `‹id›` shown in any outline. |
| `list_tasks(status?, tag?, date?)` | Tasks, filtered by status, tag (on the task or any parent) or day. |
| `search(query)` | Blocks containing a text, most recent first. |
| `get_page(title)` | A regular page. |
| `get_image(id)` | One screenshot. |
| `graph_info()` | Which graph is being read and what's in it. |

Screenshots come back as MCP image content, so a multimodal model sees them
directly. At most 8 per call are attached; the rest can be fetched with `get_image`.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/joseguzmann/logseq-mcp.git
```

**Claude Code**

```bash
claude mcp add logseq -- uv run --directory /path/to/logseq-mcp logseq-mcp
```

**Claude Desktop, Cursor and other MCP clients**

```json
{
  "mcpServers": {
    "logseq": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/logseq-mcp", "logseq-mcp"]
    }
  }
}
```

### Configuration

| Flag | Env var | Default |
|---|---|---|
| `--graphs-dir` | `LOGSEQ_GRAPHS_DIR` | `~/logseq/graphs` |
| `--graph` | `LOGSEQ_GRAPH` | the only graph, if there is just one |
| `--task-pattern` | `LOGSEQ_TASK_PATTERN` | none: only blocks tagged `#Task` are tasks |
|  | `LOGSEQ_MAX_IMAGES` | `8` |

`--task-pattern` is for people who, like me, name tasks without always tagging
them: `--task-pattern '[a-z][a-z-]*-\d+'` treats `web-12` as a task too.

## Limitations

- **DB graphs only.** For the classic Markdown version, the files themselves are
  a better source.
- **Read-only, by design.** Writing to Logseq's internal format behind the app's
  back is how you corrupt a graph.
- **The format is internal** and can change with any Logseq release. When it does,
  the reader stops with a clear error instead of returning partial data.
  Tested against Logseq 2.0.1 on macOS.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

The tests don't need Logseq. [`tests/graphbuilder.py`](tests/graphbuilder.py)
writes synthetic graphs with the same on-disk layout, including stale rows and a
wrong declared count, and the server is exercised end to end through an MCP client.

## License

MIT
