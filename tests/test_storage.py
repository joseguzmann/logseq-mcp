import sqlite3

import pytest

from graphbuilder import GraphBuilder, sample_graph
from logseq_mcp import storage


def test_reads_only_the_current_state(tmp_path):
    graph_dir, ids = sample_graph(tmp_path)
    datoms = storage.read_datoms(graph_dir)
    titles = {d.v for d in datoms if d.a == "~:block/title"}
    assert "web-1" in titles
    # Stale rows left behind by earlier writes must not leak in.
    assert "ghost block" not in titles
    assert "web-1 half typ" not in titles


def test_count_mismatch_refuses_to_return_partial_data(tmp_path):
    graph_dir, _ = sample_graph(tmp_path, declared_count=3)
    with pytest.raises(storage.FormatError, match="declares 3"):
        storage.read_datoms(graph_dir)


def test_missing_root_is_a_format_error(tmp_path):
    graph_dir, _ = sample_graph(tmp_path)
    con = sqlite3.connect(graph_dir / "db.sqlite")
    con.execute("delete from kvs where addr = 0")
    con.commit()
    con.close()
    with pytest.raises(storage.FormatError, match="root"):
        storage.read_datoms(graph_dir)


def test_dangling_index_pointer_is_a_format_error(tmp_path):
    graph_dir, _ = sample_graph(tmp_path)
    con = sqlite3.connect(graph_dir / "db.sqlite")
    con.execute("delete from kvs where addr = 1000001")
    con.commit()
    con.close()
    with pytest.raises(storage.FormatError, match="missing node"):
        storage.read_datoms(graph_dir)


def test_invalid_attribute_means_misaligned_cache(tmp_path):
    g = GraphBuilder()
    g.entity({"~:block/title": "ok"})
    g.datoms.append([g._next, "not-a-keyword", 1, 1])
    graph_dir = g.write(tmp_path / "bad")
    with pytest.raises(storage.FormatError, match="misaligned"):
        storage.read_datoms(graph_dir)


def test_never_writes_to_the_graph(tmp_path):
    graph_dir, _ = sample_graph(tmp_path)
    before = (graph_dir / "db.sqlite").read_bytes()
    storage.read_datoms(graph_dir)
    assert (graph_dir / "db.sqlite").read_bytes() == before
    assert sorted(p.name for p in graph_dir.iterdir()) == ["assets", "db.sqlite"]


def test_resolve_graph(tmp_path):
    with pytest.raises(FileNotFoundError):
        storage.resolve_graph(graphs_dir=tmp_path)
    sample_graph(tmp_path, "work")
    assert storage.resolve_graph(graphs_dir=tmp_path).name == "work"
    sample_graph(tmp_path, "personal")
    with pytest.raises(ValueError, match="several graphs"):
        storage.resolve_graph(graphs_dir=tmp_path)
    assert storage.resolve_graph("personal", tmp_path).name == "personal"
    with pytest.raises(FileNotFoundError, match="Available: personal, work"):
        storage.resolve_graph("nope", tmp_path)


def test_fingerprint_changes_on_write(tmp_path):
    graph_dir, _ = sample_graph(tmp_path)
    before = storage.fingerprint(graph_dir)
    sample_graph(tmp_path, leaf_size=3)
    assert storage.fingerprint(graph_dir) != before
