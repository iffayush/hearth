"""Tests for control.py and the CLI's one-shot routing (cli.one_shot)."""

import argparse
import asyncio
import os
from pathlib import Path

import pytest

from hearth import cli, config, control
from hearth.control import ControlError
from hearth.lockfile import DataDirLock
from hearth.node import Node
from hearth.store import Store

from conftest import NodeFactory, seed_group


async def running_admin(make_node: NodeFactory) -> Node:
    node = await make_node("alice", control=True)
    seed_group(node)
    return node


def cli_args(data_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(profile=None, data_dir=data_dir, passphrase_file=None)


async def test_requests_run_on_the_node(make_node: NodeFactory) -> None:
    node = await running_admin(make_node)
    result = await control.send_request(node.data_dir, {"op": "post", "text": "via the intercom", "images": []})
    assert result["seq"] == 2
    feed = await control.send_request(node.data_dir, {"op": "feed", "limit": 5})
    assert feed["posts"][0]["text"] == "via the intercom"


async def test_wrong_token_is_refused_and_nothing_happens(make_node: NodeFactory) -> None:
    node = await running_admin(make_node)
    with pytest.raises(ControlError, match="wrong control token"):
        await control.send_request(node.data_dir, {"op": "post", "text": "sneaky", "images": []}, token="00" * 32)
    assert node.store.entry_count() == 1


async def test_malformed_requests_get_a_clear_error(make_node: NodeFactory) -> None:
    node = await running_admin(make_node)
    with pytest.raises(ControlError, match="unknown request"):
        await control.send_request(node.data_dir, {"op": "rm -rf"})
    with pytest.raises(ControlError, match="invalid text"):
        await control.send_request(node.data_dir, {"op": "post", "text": "x" * (config.MAX_POST_TEXT_CHARS + 1), "images": []})
    with pytest.raises(ControlError, match="wrong fields"):
        await control.send_request(node.data_dir, {"op": "feed", "limit": 5, "extra": True})


async def test_garbage_line_and_oversized_line(make_node: NodeFactory) -> None:
    node = await running_admin(make_node)
    for line in (b"not json at all\n", b'{"token":"' + b"x" * (config.MAX_CONTROL_REQUEST_BYTES + 10) + b'"}\n'):
        reply = await raw_control_exchange(node.data_dir, line)
        assert b'"ok":false' in reply


async def raw_control_exchange(data_dir: Path, line: bytes) -> bytes:
    port, _ = control.read_token_file(data_dir)
    if port is None:
        reader, writer = await asyncio.open_unix_connection(str(data_dir / config.CONTROL_SOCKET_FILE))
    else:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(line)
    await writer.drain()
    reply = await reader.readline()
    writer.close()
    return reply


async def test_one_shot_against_a_running_node_never_opens_the_database(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = await running_admin(make_node)

    def must_not_open(*args: object, **kwargs: object) -> Store:
        raise AssertionError("the CLI opened the database while a node owns it")

    def must_not_prompt(*args: object, **kwargs: object) -> str:
        raise AssertionError("the CLI asked for a passphrase while a node owns the keys")

    monkeypatch.setattr(Store, "open", must_not_open)
    monkeypatch.setattr(cli.getpass, "getpass", must_not_prompt)
    # Hold the lock the way `run` does, so the CLI sees a running node.
    with DataDirLock(node.data_dir):
        request = {"op": "post", "text": "one writer per log", "images": []}
        # In a thread, because the CLI runs its own event loop for the request.
        result = await asyncio.to_thread(cli.one_shot, cli_args(node.data_dir), request)
    assert result["seq"] == 2
    assert node.store.get_entry(node.identity.pubkey, 2) is not None


@pytest.mark.skipif(not control.uses_unix_socket(), reason="only Unix sockets have a path limit")
async def test_a_data_dir_too_long_for_the_socket_says_what_to_do(make_node: NodeFactory) -> None:
    with pytest.raises(control.ControlError, match="use a shorter --data-dir"):
        await make_node("x" * 120, control=True)


async def test_held_lock_without_control_channel_is_explained(make_node: NodeFactory) -> None:
    node = await make_node("alice")  # no control channel
    with DataDirLock(node.data_dir):
        with pytest.raises(ControlError, match="control channel isn't up"):
            await asyncio.to_thread(cli.one_shot, cli_args(node.data_dir), {"op": "log"})


async def test_control_files_are_private_and_removed_on_stop(make_node: NodeFactory) -> None:
    node = await running_admin(make_node)
    token_file = node.data_dir / config.CONTROL_INFO_FILE
    assert token_file.exists()
    if os.name == "posix":
        assert token_file.stat().st_mode & 0o777 == 0o600
    await node.stop()
    assert not token_file.exists()
    assert not (node.data_dir / config.CONTROL_SOCKET_FILE).exists()
