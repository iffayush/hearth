"""Strict JSON parsing and deterministic ("canonical") JSON bytes.

Signatures and hashes are computed over bytes, so every node must turn the same
entry into exactly the same bytes. `dumps` does that: sorted keys, no spaces,
UTF-8. `loads` is the only way Hearth parses JSON from disk or the network, and
it refuses anything that could be read two ways: floats, NaN/Infinity,
duplicate keys and lone surrogates.

Joke: JSON has two rules here. Rule one: no floats. Rule two: see rule one.
"""

import json
from typing import Any


class CanonicalError(ValueError):
    """Raised when data is not acceptable strict JSON. The message says why."""


def dumps(value: Any) -> bytes:
    """Encode `value` as canonical JSON bytes.

    Raises CanonicalError for floats or strings that can't be UTF-8 encoded.
    """
    _check_value(value)
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as error:
        # Only reachable through a lone surrogate, which _check_value already
        # rejects; kept so a future refactor can't silently produce bad bytes.
        raise CanonicalError(f"string is not valid UTF-8: {error.reason}") from error


def loads(data: bytes | str) -> Any:
    """Parse strict JSON. Raises CanonicalError with a clear reason on failure."""
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CanonicalError("input is not valid UTF-8") from error
    try:
        value = json.loads(
            data,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except CanonicalError:
        raise
    except json.JSONDecodeError as error:
        raise CanonicalError(f"not valid JSON: {error.msg} at position {error.pos}") from error
    except ValueError as error:
        # For example Python's guard against ints with thousands of digits.
        raise CanonicalError(f"not valid JSON: {error}") from error
    except RecursionError as error:
        raise CanonicalError("JSON is nested too deeply") from error
    _check_value(value)
    return value


def is_int(value: Any) -> bool:
    """True only for real ints. `isinstance(True, int)` is True in Python, so never use it here."""
    return type(value) is int


def _reject_float(text: str) -> Any:
    raise CanonicalError(f"floats are not allowed (got {text})")


def _reject_constant(text: str) -> Any:
    raise CanonicalError(f"{text} is not allowed")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _check_value(value: Any) -> None:
    """Walk a JSON value and reject anything that has no single canonical form."""
    if value is None or isinstance(value, bool) or is_int(value):
        return
    if isinstance(value, float):
        raise CanonicalError("floats are not allowed")
    if isinstance(value, str):
        _check_string(value)
        return
    if isinstance(value, list):
        for item in value:
            _check_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError("object keys must be strings")
            _check_string(key)
            _check_value(item)
        return
    raise CanonicalError(f"type {type(value).__name__} can't be stored as JSON")


def _check_string(text: str) -> None:
    # json.loads happily turns "\ud800" into a lone surrogate, which has no
    # UTF-8 encoding. Trying the encode is the simplest reliable test.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CanonicalError(
            f"string contains a lone surrogate at position {error.start}"
        ) from error
