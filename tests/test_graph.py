import datetime as dt
import re

import pytest

from graphbuilder import sample_graph
from logseq_mcp import storage
from logseq_mcp.graph import Graph, parse_date

SEP20 = dt.date(2026, 9, 20)
SEP21 = dt.date(2026, 9, 21)


@pytest.fixture
def sample(tmp_path):
    graph_dir, ids = sample_graph(tmp_path)
    return Graph(storage.read_datoms(graph_dir), graph_dir), ids


def test_journal_outline_keeps_structure_order_and_images(sample):
    g, ids = sample
    text, images = g.render(g.children(g.journal(SEP20)))
    lines = [re.sub(r"\s+‹\w+›", "", line) for line in text.splitlines()]
    assert lines == [
        "- Website relaunch #website",
        "  - web-1 [Doing]",
        "    - Hero image is blurry on mobile",
        "      - [image 1] this one",
        "    - Contact form sends twice",
        "      only on Safari",
        "  - web-2",
        "    - Check [[Reading list]] before shipping",
        "- Buy seeds #garden",
        "- Water the tomatoes [Done] #garden",
        "- [Todo]",
    ]
    assert [a.uuid for a in images] == [ids["uuid"](ids["image"])]
    assert images[0].exists and images[0].path.read_bytes().startswith(b"\x89PNG")


def test_task_ids_are_shown_so_the_model_can_follow_up(sample):
    g, ids = sample
    text, _ = g.render([ids["web1"]])
    assert f"‹{ids['uuid'](ids['web1'])[:8]}›" in text.splitlines()[0]
    assert g.by_uuid(ids["uuid"](ids["web1"])[:8]) == ids["web1"]
    assert g.by_uuid("abc") is None


def test_missing_image_is_flagged(sample):
    g, ids = sample
    text, images = g.render([ids["web1_again"]])
    assert "⚠ file missing" in text
    assert not images[0].exists


def test_tasks_by_tag_and_name(sample):
    g, ids = sample
    tagged = {g.raw_title(t) for t in g.tasks()}
    assert tagged == {"web-1", "Water the tomatoes", ""}
    by_name = g.tasks(name_pattern=re.compile(r"[a-z]+-\d+"))
    assert ids["web2"] in by_name


def test_task_filters(sample):
    g, ids = sample
    assert g.tasks(status="doing") == [ids["web1"]]
    assert g.tasks(tag="#website") == [ids["web1"], ids["web1_again"]]
    assert g.tasks(tag="website", day=SEP21) == [ids["web1_again"]]


def test_summary_never_fails_on_empty_blocks(sample):
    g, ids = sample
    assert g.summary(ids["web1"]) == "web-1"
    assert [g.summary(t) for t in g.tasks(status="todo") if not g.raw_title(t)] == ["(untitled)"]


def test_context_tags_come_from_ancestors(sample):
    g, ids = sample
    assert g.context_tags(ids["web1"]) == ["website"]


def test_same_name_on_different_days(sample):
    g, ids = sample
    assert sorted(g.blocks_titled("web-1")) == sorted([ids["web1"], ids["web1_again"]])
    assert {g.journal_day(e) for e in g.blocks_titled("web-1")} == {SEP20, SEP21}


def test_pages_and_search(sample):
    g, ids = sample
    assert g.page("reading LIST") == ids["reading"]
    assert [g.raw_title(e) for e in g.search("pragmatic")] == ["The Pragmatic Programmer"]
    assert g.journal_days() == [SEP20, SEP21]


def test_parse_date():
    today = dt.date(2026, 9, 29)
    assert parse_date("today", today) == today
    assert parse_date("ayer", today) == dt.date(2026, 9, 28)
    assert parse_date("2026-09-20") == parse_date("20260920") == SEP20
    with pytest.raises(ValueError):
        parse_date("next friday")
