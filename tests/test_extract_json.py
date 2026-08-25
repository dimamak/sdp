import pytest

from server.pipeline.claude_cli import extract_json


def test_plain_object():
    assert extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_fenced_json_block():
    text = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
    assert extract_json(text) == {"a": 1}


def test_fenced_block_no_language_tag():
    text = '```\n{"a": 1}\n```'
    assert extract_json(text) == {"a": 1}


def test_prose_wrapped_object():
    text = 'Sure, the result is {"a": 1, "nested": {"b": 2}} — let me know.'
    assert extract_json(text) == {"a": 1, "nested": {"b": 2}}


def test_nested_braces_balanced_correctly():
    text = 'prefix {"a": {"b": {"c": 1}}, "d": 2} suffix'
    assert extract_json(text) == {"a": {"b": {"c": 1}}, "d": 2}


def test_no_json_object_raises_value_error():
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json("just some prose, no object here")


def test_unbalanced_json_raises_value_error():
    with pytest.raises(ValueError, match="unbalanced JSON"):
        extract_json('prefix {"a": 1, "b": {"c": 2}')
