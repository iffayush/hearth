"""Phase 8: running a profile as the home node, the way systemd will.

`run --role home` (saved for the other commands), output that reaches a log
right away, a clean stop on SIGTERM, image posts written before a profile
became the home node, `status` with no node running, and the KDF benchmark.

Joke: the home node is the friend who never leaves the party. These tests
check it also tidies up when it finally does.
"""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from hearth import config, settings

from conftest import make_image
from test_cli import alice_with_group, keyed, run_hearth
from test_cli_run import home_and_member, start_run, wait_for_log_line
from test_node import free_port


async def start_run_piped(data_dir: Path, passphrase_file: Path, *flags: str) -> asyncio.subprocess.Process:
    """Like start_run, but stdout goes to a pipe we can read while the node runs."""
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "hearth", "run", "--data-dir", str(data_dir), "--passphrase-file", str(passphrase_file),
        "--headless", "--listen", f"127.0.0.1:{free_port()}", *flags,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )


async def read_until(stream: asyncio.StreamReader | None, needle: str) -> str:
    """Read lines from `stream` until one contains `needle`; fail if it never comes."""
    assert stream is not None
    async with asyncio.timeout(30):
        while True:
            line = (await stream.readline()).decode("utf-8")
            if not line:
                pytest.fail(f"stream closed before {needle!r}")
            if needle in line:
                return line


async def test_becoming_home_publishes_waiting_posts_and_is_remembered(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = alice_with_group(tmp_path, passphrase_file)
    picture = make_image(tmp_path, "cat.png", 5000)
    # Still a member (no --role yet), so an image post waits for a home node to upload to.
    waiting = run_hearth(*keyed(["post", "before", "--image", str(picture)], data_dir, passphrase_file))
    assert waiting.stdout.startswith("Waiting to upload"), waiting

    alice = await start_run(data_dir, passphrase_file, "--role", "home", "--listen", f"127.0.0.1:{free_port()}")
    try:
        # Its chunks are already on the home node's disk, so the post goes straight into the log.
        await wait_for_log_line(alice, "Image post uploaded; it's #2")
    finally:
        alice.kill()
        await alice.wait()
    assert settings.load_settings(data_dir).role == "home"

    # The saved role means a one-shot command with no node running agrees: no outbox now.
    posted = run_hearth(*keyed(["post", "after", "--image", str(picture)], data_dir, passphrase_file))
    assert posted.stdout.startswith("Posted as #3"), posted
    feed = run_hearth(*keyed(["feed"], data_dir, passphrase_file))
    assert "before" in feed.stdout and "after" in feed.stdout and "waiting" not in feed.stdout


async def test_a_member_cant_be_the_home_node(tmp_path: Path, passphrase_file: Path) -> None:
    _, _, alice, bob_dir, _ = await home_and_member(tmp_path, passphrase_file)
    try:
        result = await asyncio.to_thread(run_hearth, *keyed(["run", "--role", "home", "--headless"], bob_dir,
                                                            passphrase_file))
    finally:
        alice.kill()
        await alice.wait()
    assert result.returncode == 1 and "only the group's admin can be its home node" in result.stderr
    assert settings.load_settings(bob_dir).role == "member"


async def test_run_output_reaches_a_pipe_while_the_node_runs(tmp_path: Path, passphrase_file: Path) -> None:
    """Under systemd, stdout is the journal, not a terminal: the banner mustn't sit in a buffer."""
    data_dir = alice_with_group(tmp_path, passphrase_file)
    alice = await start_run_piped(data_dir, passphrase_file, "--role", "home")
    try:
        assert "You are" in await read_until(alice.stdout, "You are")
    finally:
        alice.kill()
        await alice.wait()


@pytest.mark.skipif(os.name != "posix", reason="systemd's SIGTERM: Windows stops processes differently")
async def test_sigterm_stops_the_node_cleanly(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = alice_with_group(tmp_path, passphrase_file)
    alice = await start_run_piped(data_dir, passphrase_file, "--role", "home")
    await read_until(alice.stderr, "Listening on")
    assert (data_dir / config.CONTROL_SOCKET_FILE).exists()
    alice.send_signal(signal.SIGTERM)
    stdout, _ = await asyncio.wait_for(alice.communicate(), timeout=30)
    assert alice.returncode == 0
    assert "Node stopped" in stdout.decode("utf-8")
    # The finally blocks ran: the control channel's files are gone.
    assert not (data_dir / config.CONTROL_SOCKET_FILE).exists()
    assert not (data_dir / config.CONTROL_INFO_FILE).exists()


def test_status_works_with_no_node_running(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = alice_with_group(tmp_path, passphrase_file)
    result = run_hearth(*keyed(["status"], data_dir, passphrase_file))
    assert result.returncode == 0, result.stderr
    assert "members: 1" in result.stdout and "connections: none" in result.stdout


def test_bench_kdf_times_both_settings() -> None:
    result = run_hearth("debug", "bench-kdf", "--rounds", "1")
    assert result.returncode == 0, result.stderr
    timings = {line.split()[0]: line.split()[1:3] for line in result.stdout.splitlines() if line.startswith("  ")}
    assert timings == {"interactive": ["64", "MiB"], "moderate": ["256", "MiB"]}


@pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="needs root to hand a folder to another user")
def test_commands_refuse_someone_elses_data_dir(tmp_path: Path, passphrase_file: Path) -> None:
    """`sudo hearth ...` on the home node would leave root-owned files the service can't open."""
    data_dir = alice_with_group(tmp_path, passphrase_file)
    subprocess.run(["chown", "-R", "65534", str(data_dir)], check=True)
    result = run_hearth("debug", "log", "--data-dir", str(data_dir))
    assert result.returncode == 1 and "belongs to another user" in result.stderr
