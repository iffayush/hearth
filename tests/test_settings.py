"""Tests for settings.py: config.json round trip and validation."""

from pathlib import Path

import pytest

from hearth import config
from hearth.settings import NodeSettings, SettingsError, is_valid_profile, load_settings, save_settings


def test_round_trip_with_defaults(tmp_path: Path) -> None:
    save_settings(tmp_path, NodeSettings(profile="alice"))
    loaded = load_settings(tmp_path)
    assert loaded == NodeSettings(profile="alice")
    assert loaded.role == "member" and loaded.web_port == config.DEFAULT_WEB_PORT


def test_missing_file_has_helpful_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="hearth init"):
        load_settings(tmp_path)


@pytest.mark.parametrize(
    "text, message",
    [
        ('{"profile":"a","role":"overlord"}', "role"),
        ('{"profile":"a","web_port":true}', "web_port"),
        ('{"profile":"a","web_port":8080.0}', "floats"),
        ('{"profile":"a","colour":"blue"}', "unknown setting"),
        ('{"role":"home"}', "missing"),
        ('{"profile":"../../etc"}', "profile"),
        ("[1,2,3]", "JSON object"),
    ],
)
def test_bad_settings_are_rejected(tmp_path: Path, text: str, message: str) -> None:
    (tmp_path / config.SETTINGS_FILE).write_text(text, encoding="utf-8")
    with pytest.raises(SettingsError, match=message):
        load_settings(tmp_path)


@pytest.mark.parametrize("name, ok", [("alice", True), ("home_2", True), ("a.b", False), ("", False), ("x/y", False)])
def test_profile_names(name: str, ok: bool) -> None:
    assert is_valid_profile(name) is ok
