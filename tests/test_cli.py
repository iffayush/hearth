"""CLI tests: real `python -m hearth` processes (no prompts, --passphrase-file),
plus in-process checks of the passphrase prompt, which needs a real console otherwise.

Tests that keep a `run` process going (two nodes, images, invites) are in
test_cli_run.py; the Phase 8 home-node ones are in test_home_node.py."""

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hearth import canonical, cli, config

from test_lockfile import HOLD_LOCK_SCRIPT


def run_hearth(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hearth", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def init_alice(tmp_path: Path, passphrase_file: Path) -> Path:
    data_dir = tmp_path / "alice"
    result = run_hearth("init", "--profile", "alice", "--data-dir", str(data_dir),
                        "--passphrase-file", str(passphrase_file), "--kdf", "interactive")
    assert result.returncode == 0, result.stderr
    return data_dir


def keyed(command: list[str], data_dir: Path, passphrase_file: Path) -> list[str]:
    """Add the location and passphrase flags to a command that opens keys."""
    return [*command, "--data-dir", str(data_dir), "--passphrase-file", str(passphrase_file)]


def alice_with_group(tmp_path: Path, passphrase_file: Path) -> Path:
    data_dir = init_alice(tmp_path, passphrase_file)
    result = run_hearth(*keyed(["create-group"], data_dir, passphrase_file))
    assert result.returncode == 0, result.stderr
    return data_dir


def post(data_dir: Path, passphrase_file: Path, text: str) -> subprocess.CompletedProcess[str]:
    return run_hearth(*keyed(["post", text], data_dir, passphrase_file))


# --- init --------------------------------------------------------------------------


def test_init_creates_encrypted_identity_and_settings(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = init_alice(tmp_path, passphrase_file)
    key_bytes = (data_dir / config.IDENTITY_FILE).read_bytes()
    assert key_bytes.startswith(config.IDENTITY_TAG)
    settings = canonical.loads((data_dir / config.SETTINGS_FILE).read_bytes())
    assert settings["profile"] == "alice"


def test_init_twice_is_refused(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = init_alice(tmp_path, passphrase_file)
    again = run_hearth("init", "--data-dir", str(data_dir), "--passphrase-file", str(passphrase_file))
    assert again.returncode == 1
    assert "already exists" in again.stderr


# --- create-group, post, feed, log, verify -----------------------------------------


def test_create_group_makes_a_group_and_its_key_file(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = init_alice(tmp_path, passphrase_file)
    result = run_hearth(*keyed(["create-group"], data_dir, passphrase_file))
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Created group ")
    assert (data_dir / config.GROUP_KEY_FILE).read_bytes().startswith(config.GROUP_KEY_TAG)
    again = run_hearth(*keyed(["create-group"], data_dir, passphrase_file))
    assert again.returncode == 1 and "already has a group key" in again.stderr


def test_post_without_a_group_is_explained(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = init_alice(tmp_path, passphrase_file)
    result = post(data_dir, passphrase_file, "hello?")
    assert result.returncode == 1 and "create-group" in result.stderr


def test_post_appends_and_feed_shows_it(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = alice_with_group(tmp_path, passphrase_file)
    first = post(data_dir, passphrase_file, "chai time ☕")
    assert first.returncode == 0, first.stderr
    # #1 is the group_create, so the first post is #2.
    assert first.stdout.startswith("Posted as #2")
    assert post(data_dir, passphrase_file, "samosas").stdout.startswith("Posted as #3")
    feed = run_hearth(*keyed(["feed"], data_dir, passphrase_file))
    assert feed.returncode == 0, feed.stderr
    assert "samosas" in feed.stdout and "chai time ☕" in feed.stdout
    assert feed.stdout.index("samosas") < feed.stdout.index("chai time")


def test_log_lists_entries_after_a_restart(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = alice_with_group(tmp_path, passphrase_file)
    post(data_dir, passphrase_file, "first post!")
    # Every command is its own process, so this is already a restart.
    result = run_hearth("debug", "log", "--data-dir", str(data_dir))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "members 1" in lines[0]
    assert "#1" in lines[1] and "group_create" in lines[1]
    # No keys, no plaintext: the log only knows there's an encrypted post.
    assert "#2" in lines[2] and "encrypted post" in lines[2] and "first post" not in result.stdout


def test_log_without_a_group(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = init_alice(tmp_path, passphrase_file)
    result = run_hearth("debug", "log", "--data-dir", str(data_dir))
    assert result.returncode == 0 and "No group yet" in result.stdout


def test_verify_catches_a_byte_changed_in_the_database(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = alice_with_group(tmp_path, passphrase_file)
    post(data_dir, passphrase_file, "the cake is real")
    good = run_hearth("debug", "verify", "--data-dir", str(data_dir))
    assert good.returncode == 0 and "All 2 entries verified" in good.stdout

    db = sqlite3.connect(data_dir / config.DATABASE_FILE)
    # The stored text is ciphertext now, so flip a signed field instead: the seq.
    with db:
        db.execute("""UPDATE entries SET raw = replace(raw, '"seq":2', '"seq":7') WHERE seq = 2""")
    db.close()
    bad = run_hearth("debug", "verify", "--data-dir", str(data_dir))
    assert bad.returncode == 1
    assert "#2 (bad_signature)" in bad.stdout


# --- Failures ----------------------------------------------------------------------


def test_wrong_passphrase_fails_cleanly(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = alice_with_group(tmp_path, passphrase_file)
    wrong = tmp_path / "wrong.txt"
    wrong.write_text("hunter3", encoding="utf-8")
    wrong.chmod(0o600)
    result = post(data_dir, wrong, "hi")
    assert result.returncode == 1
    assert "wrong passphrase" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_world_readable_passphrase_file_is_refused(tmp_path: Path, passphrase_file: Path) -> None:
    passphrase_file.chmod(0o644)
    result = run_hearth("init", "--data-dir", str(tmp_path / "alice"), "--passphrase-file", str(passphrase_file))
    assert result.returncode == 1 and "chmod 600" in result.stderr
    assert not (tmp_path / "alice" / config.IDENTITY_FILE).exists()


def test_missing_profile_and_data_dir_is_explained() -> None:
    result = run_hearth("post", "hi")
    assert result.returncode == 1 and "--profile" in result.stderr


def test_commands_before_init_are_explained(tmp_path: Path) -> None:
    result = run_hearth("debug", "log", "--data-dir", str(tmp_path / "nobody"))
    assert result.returncode == 1 and "init" in result.stderr


def test_log_before_any_group_names_real_commands(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = init_alice(tmp_path, passphrase_file)
    result = run_hearth("debug", "log", "--data-dir", str(data_dir))
    assert result.returncode == 0 and "`create-group`" in result.stdout and "`join`" in result.stdout
    assert "debug create-group" not in result.stdout


def test_commands_while_another_process_holds_the_data_dir(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = alice_with_group(tmp_path, passphrase_file)
    holder = subprocess.Popen([sys.executable, "-c", HOLD_LOCK_SCRIPT, str(data_dir)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        # Commands that only ever work directly are refused outright...
        for command in (keyed(["run"], data_dir, passphrase_file),
                        ["init", "--data-dir", str(data_dir), "--passphrase-file", str(passphrase_file)]):
            result = run_hearth(*command)
            assert result.returncode == 1 and "already in use" in result.stderr
        for command in (keyed(["create-group"], data_dir, passphrase_file),
                        keyed(["join", "hearth://join?h=x:1&k=" + "a" * 64 + "&g=" + "b" * 64 + "&t=" + "c" * 43],
                              data_dir, passphrase_file)):
            result = run_hearth(*command)
            assert result.returncode == 1 and "stop this profile's running node first" in result.stderr
        # ...one-shot commands look for the holder's control channel (this holder has none).
        for command in (keyed(["post", "hi"], data_dir, passphrase_file), ["debug", "log", "--data-dir", str(data_dir)]):
            result = run_hearth(*command)
            assert result.returncode == 1 and "control channel isn't up" in result.stderr
    finally:
        holder.communicate(input="", timeout=10)


# --- Passphrase prompts (in-process: getpass needs a real console otherwise) --------


def prompt_answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Make getpass return `answers` in order; returns the list of prompts shown."""
    prompts: list[str] = []
    replies = iter(answers)

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    return prompts


def no_file() -> argparse.Namespace:
    return argparse.Namespace(passphrase_file=None)


def test_prompt_once_when_opening_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = prompt_answers(monkeypatch, "hunter2")
    assert cli.read_passphrase(no_file(), confirm=False) == "hunter2"
    assert prompts == ["Passphrase: "]


def test_prompt_twice_when_creating_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = prompt_answers(monkeypatch, "hunter2", "hunter2")
    assert cli.read_passphrase(no_file(), confirm=True) == "hunter2"
    assert len(prompts) == 2


def test_mismatched_confirmation_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    prompt_answers(monkeypatch, "hunter2", "hunter3")
    with pytest.raises(cli.CliError, match="don't match"):
        cli.read_passphrase(no_file(), confirm=True)


def test_empty_passphrase_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    prompt_answers(monkeypatch, "", "")
    with pytest.raises(cli.CliError, match="can't be empty"):
        cli.read_passphrase(no_file(), confirm=True)


def test_passphrase_file_loses_only_its_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "pass.txt"
    path.write_bytes(b"  spaces count  \r\n")
    path.chmod(0o600)
    assert cli.read_passphrase(argparse.Namespace(passphrase_file=path), confirm=False) == "  spaces count  "


def test_invite_needs_a_host(tmp_path: Path, passphrase_file: Path) -> None:
    data_dir = alice_with_group(tmp_path, passphrase_file)
    result = run_hearth(*keyed(["invite"], data_dir, passphrase_file))
    assert result.returncode == 1 and "public_host" in result.stderr


def test_join_refuses_a_garbled_link(tmp_path: Path, passphrase_file: Path) -> None:
    result = run_hearth("join", "https://example.com/not-an-invite", "--data-dir", str(tmp_path / "x"),
                        "--passphrase-file", str(passphrase_file))
    assert result.returncode == 1 and "isn't a Hearth invite link" in result.stderr
