"""CLI tests with a `run` process kept going: two real nodes talking, images
uploaded through the home node, invites redeemed. Helpers here are shared with
test_home_node.py and test_backup.py.

Joke: integration tests are a dinner party. Somebody always arrives late.
"""

import asyncio
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hearth import config

from conftest import make_image
from test_cli import init_alice, keyed, post, run_hearth
from test_node import free_port


# --- run ---------------------------------------------------------------------------


def init_profile(tmp_path: Path, name: str, passphrase_file: Path) -> tuple[Path, str]:
    """`init` a profile and return its data dir and full pubkey."""
    data_dir = tmp_path / name
    result = run_hearth("init", "--profile", name, "--data-dir", str(data_dir),
                        "--passphrase-file", str(passphrase_file), "--kdf", "interactive")
    assert result.returncode == 0, result.stderr
    pubkey = result.stdout.split("Your account ID: ")[1].split()[0]
    return data_dir, pubkey


async def start_run(data_dir: Path, passphrase_file: Path, *flags: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "hearth", "run", "--data-dir", str(data_dir),
        "--passphrase-file", str(passphrase_file), "--headless", *flags,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )


async def wait_for_log_line(process: asyncio.subprocess.Process, needle: str) -> str:
    """Read the process's log (stderr) line by line until one contains `needle`."""
    assert process.stderr is not None
    async with asyncio.timeout(30):
        while True:
            line = (await process.stderr.readline()).decode("utf-8")
            if not line:
                pytest.fail(f"process exited before logging {needle!r}")
            if needle in line:
                return line


async def test_two_run_processes_handshake_and_ping(tmp_path: Path, passphrase_file: Path) -> None:
    alice_dir, alice_key, alice, bob_dir, bob_key = await home_and_member(tmp_path, passphrase_file)
    # bob dials the home node named in his config.json (from the invite link), pinned.
    bob = await start_run(bob_dir, passphrase_file)
    try:
        assert alice_key[:8] in await wait_for_log_line(bob, "PONG from")
        assert bob_key[:8] in await wait_for_log_line(alice, "Handshake complete with")
    finally:
        for process in (alice, bob):
            process.kill()
            await process.wait()


def test_run_refuses_a_non_ipv4_listen_address(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = init_alice(tmp_path, passphrase_file)
    result = run_hearth(*keyed(["run", "--listen", "localhost:7001"], data_dir, passphrase_file))
    assert result.returncode == 1 and "IPv4" in result.stderr


async def running_home(tmp_path: Path, passphrase_file: Path) -> tuple[Path, str, Any, int]:
    """alice: creates a group, becomes the home node, and runs."""
    alice_dir, alice_key = init_profile(tmp_path, "alice", passphrase_file)
    assert run_hearth(*keyed(["create-group"], alice_dir, passphrase_file)).returncode == 0
    port = free_port()
    alice = await start_run(alice_dir, passphrase_file, "--role", "home", "--listen", f"127.0.0.1:{port}")
    await wait_for_log_line(alice, "Listening on")
    return alice_dir, alice_key, alice, port


async def invite_link(alice_dir: Path, passphrase_file: Path, port: int) -> str:
    invited = await asyncio.to_thread(
        run_hearth, *keyed(["invite", "--host", f"127.0.0.1:{port}"], alice_dir, passphrase_file))
    assert invited.returncode == 0, invited.stderr
    return next(line for line in invited.stdout.splitlines() if line.startswith("hearth://"))


async def join(tmp_path: Path, name: str, link: str, passphrase_file: Path) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        run_hearth, "join", link, "--profile", name, "--data-dir", str(tmp_path / name),
        "--passphrase-file", str(passphrase_file), "--kdf", "interactive", "--name", name.title())


async def home_and_member(tmp_path: Path, passphrase_file: Path) -> tuple[Path, str, Any, Path, str]:
    """alice runs the home node; bob joins with an invite link alice makes."""
    alice_dir, alice_key, alice, port = await running_home(tmp_path, passphrase_file)
    joined = await join(tmp_path, "bob", await invite_link(alice_dir, passphrase_file, port), passphrase_file)
    assert joined.returncode == 0, joined.stdout + joined.stderr
    bob_key = joined.stdout.split("Created your identity: ")[1].split()[0]
    return alice_dir, alice_key, alice, tmp_path / "bob", bob_key


async def test_post_to_running_alice_shows_up_in_bobs_feed(tmp_path: Path, passphrase_file: Path) -> None:
    """Phase 4's done-when, end to end: two real nodes, one-shot commands over the control channel."""
    alice_dir, alice_key, alice, bob_dir, bob_key = await home_and_member(tmp_path, passphrase_file)
    # bob's config.json now names the home node (from the link), pinned: no flags needed.
    bob = await start_run(bob_dir, passphrase_file)
    try:
        await wait_for_log_line(bob, "Handshake complete with")
        # alice's node is running, so this goes over her control channel.
        posted = await asyncio.to_thread(post, alice_dir, passphrase_file, "the kettle is on")
        assert posted.returncode == 0, posted.stderr
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            feed = await asyncio.to_thread(run_hearth, "feed", "--data-dir", str(bob_dir))
            assert feed.returncode == 0, feed.stderr
            if "the kettle is on" in feed.stdout:
                break
            assert asyncio.get_running_loop().time() < deadline, f"bob's feed never showed the post: {feed}"
        assert alice_key[:8] in feed.stdout
    finally:
        for process in (alice, bob):
            process.kill()
            await process.wait()


def test_run_on_a_busy_port_gives_a_clear_error(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = init_alice(tmp_path, passphrase_file)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        port = squatter.getsockname()[1]
        result = run_hearth(*keyed(["run", "--listen", f"127.0.0.1:{port}"], data_dir, passphrase_file))
    assert result.returncode == 1
    assert "Is another node already using that port?" in result.stderr
    assert "Traceback" not in result.stderr


# --- Images, end to end -------------------------------------------------------------


async def test_image_post_uploads_to_home_and_rebuilds_byte_for_byte(tmp_path: Path, passphrase_file: Path) -> None:
    """Phase 5's done-when through the real CLI: bob posts an image, it waits in his outbox until
    the home node (alice) has every chunk, then appears; alice rebuilds it byte for byte."""
    alice_dir, alice_key, alice, bob_dir, bob_key = await home_and_member(tmp_path, passphrase_file)
    picture = make_image(tmp_path, "cat.jpg", 2 * config.CHUNK_BYTES + 1234, kind="jpeg")
    bob = await start_run(bob_dir, passphrase_file)
    try:
        await wait_for_log_line(bob, "Handshake complete with")
        posted = await asyncio.to_thread(
            run_hearth, *keyed(["post", "my cat", "--image", str(picture)], bob_dir, passphrase_file))
        assert posted.returncode == 0 and posted.stdout.startswith("Waiting to upload"), posted
        await wait_for_log_line(bob, "Image post uploaded")
        saved = tmp_path / "rebuilt.jpg"
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            fetched = await asyncio.to_thread(
                run_hearth, *keyed(["debug", "image", bob_key, "2", "0", "--out", str(saved)], alice_dir, passphrase_file))
            if fetched.returncode == 0:
                break
            assert asyncio.get_running_loop().time() < deadline, fetched.stderr
        assert saved.read_bytes() == picture.read_bytes()
    finally:
        for process in (alice, bob):
            process.kill()
            await process.wait()
    # The database holds no readable text, and the chunk files hold no image bytes.
    assert b"my cat" not in (alice_dir / config.DATABASE_FILE).read_bytes()
    chunk_files = [path for path in (alice_dir / config.CHUNKS_DIR).rglob("*") if path.is_file()]
    assert len(chunk_files) == 3
    assert all(picture.read_bytes()[:4096] not in path.read_bytes() for path in chunk_files)



# --- Invites, joining, status ------------------------------------------------------


async def test_join_with_an_invite_then_status_and_reuse(tmp_path: Path, passphrase_file: Path) -> None:
    """Phase 6's done-when: carol joins with a link, gets the history and the key, writes her
    profile; the home node's status shows her; the same link refuses a second person."""
    alice_dir, alice_key, alice, port = await running_home(tmp_path, passphrase_file)
    try:
        await asyncio.to_thread(post, alice_dir, passphrase_file, "welcome, whoever you are")
        link = await invite_link(alice_dir, passphrase_file, port)
        joined = await join(tmp_path, "carol", link, passphrase_file)
        assert joined.returncode == 0, joined.stdout + joined.stderr
        assert "Welcome to the group, Carol!" in joined.stdout
        # Her history arrived (create-group, her member_add, the welcome post) plus her own profile.
        assert "the group's 4 entries" in joined.stdout

        carol_feed = run_hearth(*keyed(["feed"], tmp_path / "carol", passphrase_file))
        assert "welcome, whoever you are" in carol_feed.stdout
        status = await asyncio.to_thread(run_hearth, *keyed(["status"], alice_dir, passphrase_file))
        assert "members: 2" in status.stdout and "Carol" in status.stdout, status.stdout

        again = await join(tmp_path, "mallory", link, passphrase_file)
        assert again.returncode == 1
        assert "bad_invite" in again.stderr and "already been used" in again.stderr
    finally:
        alice.kill()
        await alice.wait()
