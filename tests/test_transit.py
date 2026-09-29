import json

import pytest

from logseq_mcp import transit


def test_plain_values_pass_through():
    assert transit.loads('[1, "abc", true, null]') == [1, "abc", True, None]


def test_map_keys_are_cached_and_referenced():
    doc = ["^ ", "~:block/title", "hello", "~:block/order", "a0"]
    assert transit.loads(json.dumps([doc, ["^ ", "^0", "bye", "^1", "a1"]])) == [
        {"~:block/title": "hello", "~:block/order": "a0"},
        {"~:block/title": "bye", "~:block/order": "a1"},
    ]


def test_key_is_cached_before_its_value():
    # The key "~:block/tags" goes to slot 0 and the keyword value "~:user.class/x"
    # to slot 1. Decoding the value first would swap them.
    doc = ["^ ", "~:block/tags", "~:user.class/x", "^1", "^0"]
    assert transit.loads(json.dumps(doc)) == {
        "~:block/tags": "~:user.class/x",
        "~:user.class/x": "~:block/tags",
    }


def test_short_strings_and_plain_values_are_not_cached():
    # "abc" (<= 3 chars) and "plain text" (a value without a tag) never enter the cache,
    # so the first cached entry is the key "~:block/title".
    doc = ["^ ", "abc", "plain text", "~:block/title", "x", "^0", "y"]
    assert transit.loads(json.dumps(doc)) == {"abc": "plain text", "~:block/title": "y"}


def test_two_char_cache_codes():
    keys = [f"~:attr/{i:03d}" for i in range(50)]
    doc = ["^ "]
    for k in keys:
        doc += [k, 1]
    # index 45 is encoded as "^" + chr(48 + 1) + chr(48 + 1)
    doc += ["^11", 2]
    assert transit.loads(json.dumps(doc))[keys[45]] == 2


def test_out_of_range_reference_fails_loudly():
    with pytest.raises(transit.TransitError):
        transit.loads('["^ ", "~:a/b", 1, "^5", 2]')


def test_each_document_has_its_own_cache():
    transit.loads('["^ ", "~:block/title", 1]')
    with pytest.raises(transit.TransitError):
        transit.loads('["^0"]')


def test_escaped_strings_are_unescaped():
    assert transit.loads('["~~home", "~^not a ref", "~`raw"]') == ["~home", "^not a ref", "`raw"]
