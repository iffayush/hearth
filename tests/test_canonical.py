"""Tests for canonical.py: deterministic bytes and a parser that refuses ambiguity."""

import pytest

from hearth import canonical
from hearth.canonical import CanonicalError


def test_keys_are_sorted_and_spaces_removed() -> None:
    assert canonical.dumps({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'


def test_unicode_is_kept_as_utf8_not_escaped() -> None:
    assert canonical.dumps({"t": "chai ☕"}) == '{"t":"chai ☕"}'.encode("utf-8")


def test_same_value_always_gives_same_bytes() -> None:
    first = {"z": {"y": 2, "x": 1}, "a": "hello"}
    second = {"a": "hello", "z": {"x": 1, "y": 2}}
    assert canonical.dumps(first) == canonical.dumps(second)


def test_round_trip() -> None:
    value = {"seq": 3, "prev": None, "body": {"text": "hi"}, "ok": False}
    assert canonical.loads(canonical.dumps(value)) == value


@pytest.mark.parametrize("text", ['{"x":1.5}', '{"x":1e3}', "[0.0]"])
def test_floats_are_rejected(text: str) -> None:
    with pytest.raises(CanonicalError, match="floats"):
        canonical.loads(text)


@pytest.mark.parametrize("text", ['{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}'])
def test_nan_and_infinity_are_rejected(text: str) -> None:
    with pytest.raises(CanonicalError, match="not allowed"):
        canonical.loads(text)


def test_duplicate_keys_are_rejected() -> None:
    with pytest.raises(CanonicalError, match="duplicate key 'seq'"):
        canonical.loads('{"seq":1,"seq":2}')


def test_nested_duplicate_keys_are_rejected() -> None:
    with pytest.raises(CanonicalError, match="duplicate"):
        canonical.loads('{"body":{"a":1,"a":1}}')


def test_true_is_not_an_int() -> None:
    parsed = canonical.loads('{"seq":true}')
    # Parsing is fine (true is valid JSON); using it where an int belongs is not.
    assert canonical.is_int(parsed["seq"]) is False
    assert canonical.is_int(1) is True


def test_lone_surrogate_is_rejected_with_clear_reason() -> None:
    with pytest.raises(CanonicalError, match="lone surrogate"):
        canonical.loads('{"t":"\\ud800"}')


def test_lone_surrogate_in_a_key_is_rejected() -> None:
    with pytest.raises(CanonicalError, match="lone surrogate"):
        canonical.loads('{"\\udfff":1}')


def test_dumps_refuses_lone_surrogates_and_floats() -> None:
    with pytest.raises(CanonicalError):
        canonical.dumps({"t": "\ud800"})
    with pytest.raises(CanonicalError):
        canonical.dumps({"x": 0.1})


def test_invalid_utf8_bytes_are_rejected() -> None:
    with pytest.raises(CanonicalError, match="UTF-8"):
        canonical.loads(b'{"t":"\xff"}')


def test_garbage_is_rejected_not_crashed() -> None:
    with pytest.raises(CanonicalError, match="not valid JSON"):
        canonical.loads(b"{this is not json")


def test_absurdly_deep_nesting_is_rejected() -> None:
    with pytest.raises(CanonicalError):
        canonical.loads("[" * 100_000 + "]" * 100_000)
