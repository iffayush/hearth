"""Load and save a node's own settings, `<data-dir>/config.json`.

These are per-node runtime choices (which profile, which address to listen on,
member or home role, which home node to dial). Limits and timeouts that are the
same for everyone live in `config.py` instead. The group ID and admin pubkey
are *not* here: from Phase 2 they live in the database's `meta` table.

Joke: config.py is the law of the land; config.json is your personal fridge
magnet collection.
"""

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hearth import canonical, config, keyfile

ROLES = ("member", "home")
# Profile names become folder names under ~/.hearth/, so no dots or slashes:
# "../../etc" is not a profile, it's a cry for help.
PROFILE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")


def is_valid_profile(name: str) -> bool:
    """True for names made of letters, digits, '_' and '-', up to 32 characters."""
    return PROFILE_PATTERN.fullmatch(name) is not None


class SettingsError(ValueError):
    """config.json is missing, unreadable, or has a bad value."""


@dataclass
class NodeSettings:
    """Everything in config.json.

    `home_host` ("host:port") and `home_pubkey` come from the invite link, so a
    member's `run` dials home pinned. `public_host` ("host:port") is the home
    node's own address as friends reach it, for the invite links it makes.
    Either can be "tls://host:port" when a front door only lets TLS through.
    """

    profile: str
    listen: str = config.DEFAULT_LISTEN
    role: str = config.DEFAULT_ROLE
    home_host: str | None = None
    home_pubkey: str | None = None
    public_host: str | None = None
    web_port: int = config.DEFAULT_WEB_PORT
    storage_quota_gib: int = config.DEFAULT_STORAGE_QUOTA_GIB


def save_settings(data_dir: Path, settings: NodeSettings) -> None:
    """Write config.json atomically (it holds no secrets, but a half-written file is no fun)."""
    _check_settings(settings)
    data = canonical.dumps(asdict(settings))
    keyfile.write_private_file(data_dir / config.SETTINGS_FILE, data)


def load_settings(data_dir: Path) -> NodeSettings:
    """Read and validate config.json."""
    path = data_dir / config.SETTINGS_FILE
    try:
        data = canonical.loads(path.read_bytes())
    except FileNotFoundError as error:
        raise SettingsError(f"{path} not found; run `python -m hearth init` first") from error
    except canonical.CanonicalError as error:
        raise SettingsError(f"{path} is not valid JSON: {error}") from error
    return _settings_from_dict(data, path)


def _settings_from_dict(data: Any, path: Path) -> NodeSettings:
    if not isinstance(data, dict):
        raise SettingsError(f"{path} must contain a JSON object")
    known = set(NodeSettings.__dataclass_fields__)
    unknown = sorted(set(data) - known)
    if unknown:
        raise SettingsError(f"{path} has unknown setting(s): {', '.join(unknown)}")
    try:
        settings = NodeSettings(**data)
    except TypeError as error:
        raise SettingsError(f"{path} is missing a required setting: {error}") from error
    _check_settings(settings)
    return settings


def _check_settings(settings: NodeSettings) -> None:
    if not isinstance(settings.profile, str) or not is_valid_profile(settings.profile):
        raise SettingsError("profile must be 1-32 letters, digits, '_' or '-'")
    if settings.role not in ROLES:
        raise SettingsError(f"role must be one of {ROLES}, not {settings.role!r}")
    if not isinstance(settings.listen, str):
        raise SettingsError("listen must be a 'host:port' string")
    for name in ("web_port", "storage_quota_gib"):
        value = getattr(settings, name)
        if not canonical.is_int(value) or value <= 0:
            raise SettingsError(f"{name} must be a positive integer")
    for name in ("home_host", "home_pubkey", "public_host"):
        value = getattr(settings, name)
        if value is not None and not isinstance(value, str):
            raise SettingsError(f"{name} must be a string or null")
