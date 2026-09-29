import re

import pytest
from mcp import Client
from mcp.types import ImageContent, TextContent

from graphbuilder import sample_graph
from logseq_mcp.server import Config, create_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def graph(tmp_path):
    graph_dir, ids = sample_graph(tmp_path)
    config = Config(graphs_dir=tmp_path, task_pattern=re.compile(r"[a-z]+-\d+"))
    return create_server(config), ids, tmp_path


def texts(result):
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


def images(result):
    return [c for c in result.content if isinstance(c, ImageContent)]


@pytest.mark.anyio
async def test_all_tools_are_read_only(graph):
    server, _, _ = graph
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {
        "graph_info",
        "get_journal",
        "list_tasks",
        "get_task",
        "get_block",
        "get_page",
        "search",
        "get_image",
    }
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


@pytest.mark.anyio
async def test_journal_comes_with_its_screenshot(graph):
    server, _, _ = graph
    async with Client(server) as client:
        result = await client.call_tool("get_journal", {"date": "2026-09-20"})
    assert not result.is_error
    assert "Hero image is blurry on mobile" in texts(result)
    assert "[image 1] this one" in texts(result)
    [img] = images(result)
    assert img.mime_type == "image/png"


@pytest.mark.anyio
async def test_images_can_be_left_out(graph):
    server, _, _ = graph
    async with Client(server) as client:
        result = await client.call_tool("get_journal", {"date": "2026-09-20", "include_images": False})
    assert images(result) == []


@pytest.mark.anyio
async def test_reused_task_name_asks_which_one(graph):
    server, ids, _ = graph
    async with Client(server) as client:
        ambiguous = await client.call_tool("get_task", {"name": "web-1"})
        dated = await client.call_tool("get_task", {"name": "web-1", "date": "2026-09-21"})
        by_id = await client.call_tool("get_block", {"id": ids["uuid"](ids["web1"])[:8]})
    assert "2 blocks are called 'web-1'" in texts(ambiguous)
    assert "Todo" in texts(dated) and "journal 2026-09-21" in texts(dated)
    assert "Doing" in texts(by_id) and "#website" in texts(by_id)


@pytest.mark.anyio
async def test_list_tasks_uses_the_name_pattern(graph):
    server, _, _ = graph
    async with Client(server) as client:
        result = await client.call_tool("list_tasks", {"tag": "website"})
    assert texts(result).startswith("3 tasks")  # web-1 twice (tagged) + web-2 (by name)


@pytest.mark.anyio
async def test_a_task_without_text_does_not_break_the_listing(graph):
    server, _, _ = graph
    async with Client(server) as client:
        result = await client.call_tool("list_tasks", {"status": "Todo"})
    assert not result.is_error
    assert "(untitled)" in texts(result)


@pytest.mark.anyio
async def test_errors_are_readable(graph):
    server, _, _ = graph
    async with Client(server) as client:
        no_day = await client.call_tool("get_journal", {"date": "2020-01-01"})
        bad_date = await client.call_tool("get_journal", {"date": "next friday"})
        gone = await client.call_tool("get_image", {"id": "nope1234"})
    assert no_day.is_error and "Most recent: 2026-09-20, 2026-09-21" in texts(no_day)
    assert bad_date.is_error and "YYYY-MM-DD" in texts(bad_date)
    assert gone.is_error


@pytest.mark.anyio
async def test_missing_image_file_is_reported(graph):
    server, ids, _ = graph
    async with Client(server) as client:
        result = await client.call_tool("get_image", {"id": ids["uuid"](ids["missing_image"])})
    assert result.is_error and "not on disk" in texts(result)


@pytest.mark.anyio
async def test_reloads_when_logseq_writes(graph):
    server, _, tmp_path = graph
    async with Client(server) as client:
        before = await client.call_tool("search", {"query": "tomatoes"})
        # Logseq rewrites the file; a new block appears.
        from graphbuilder import GraphBuilder

        g = GraphBuilder()
        day = g.journal(20260920, "Sep 20th, 2026")
        g.block(day, day, "Harvest the tomatoes", "a0")
        g.write(tmp_path / "sample")
        after = await client.call_tool("search", {"query": "tomatoes"})
    assert "Water the tomatoes" in texts(before)
    assert "Harvest the tomatoes" in texts(after)
