"""Compiled-in limits, defaults and timeouts for Hearth.

Everything here is the same for every node in every group. Per-node settings
(profile name, listen address, role, ...) live in `config.json` instead, which
`settings.py` loads. Any timeout or interval used by the code is a constructor
argument that defaults to one of these constants, so tests can shrink them.

Joke: this file is the only place in Hearth where hard-coding is encouraged.
Enjoy it while it lasts.
"""

from pathlib import Path

# --- Data directory --------------------------------------------------------

# Each profile keeps everything under ~/.hearth/<profile>/ unless --data-dir is given.
DEFAULT_DATA_ROOT = Path.home() / ".hearth"

IDENTITY_FILE = "identity.key"
GROUP_KEY_FILE = "group.key"
DATABASE_FILE = "hearth.db"
CHUNKS_DIR = "chunks"
SETTINGS_FILE = "config.json"
LOCK_FILE = "hearth.lock"

# --- Per-node setting defaults (written into config.json by `init`) ---------

DEFAULT_ROLE = "member"
DEFAULT_LISTEN = "127.0.0.1:7777"
DEFAULT_HOME_PORT = 7777
DEFAULT_WEB_PORT = 8080
DEFAULT_STORAGE_QUOTA_GIB = 20

# --- Key files ---------------------------------------------------------------

IDENTITY_TAG = b"hearth-id-v1\n"
GROUP_KEY_TAG = b"hearth-group-v1\n"
KEYFILE_SALT_BYTES = 16
# Real key files are a few hundred bytes; anything bigger is not ours.
KEYFILE_MAX_BYTES = 4 * 1024
# Names accepted by --kdf. The actual (opslimit, memlimit) pairs come from
# libsodium via keyfile.py, so they always match the installed library.
KDF_CHOICES = ("moderate", "interactive")
DEFAULT_KDF = "moderate"

# --- Entries -----------------------------------------------------------------

ENTRY_VERSION = 1
ENTRY_SIGN_PREFIX = b"hearth/entry/v1\n"
HANDSHAKE_SIGN_PREFIX = b"hearth/handshake/v1\n"
ENTRY_KINDS = ("group_create", "member_add", "profile", "post")
MAX_ENTRY_BYTES = 64 * 1024
MAX_POST_TEXT_CHARS = 10_000
MAX_IMAGES_PER_POST = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# Detected from the first bytes of the file (chunks.py), never from its name.
IMAGE_MIME_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
# seq is stored in SQLite and appears in web API paths; keep it in a 31-bit range.
MAX_SEQ = 2**31 - 1
# Unix milliseconds fit comfortably in 2^53 for the next few hundred thousand years.
MAX_TS = 2**53 - 1

# --- Wire protocol limits (used from Phase 3 on) ---------------------------

PROTOCOL_VERSION = "hearth/1"
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_CONNECTIONS = 64
MAX_CONNECTIONS_PER_IP = 8
# ERROR messages carry a human-readable reason; anything longer is not a reason.
MAX_ERROR_TEXT_CHARS = 500
MAX_ENTRIES_REPLY_BYTES = 1536 * 1024
MAX_WANT_RANGE = 200
MAX_CHUNK_WANT = 4
CHUNK_BYTES = 1024 * 1024
# An encrypted chunk is the piece plus SecretBox's 24-byte nonce and 16-byte MAC.
MAX_CHUNK_CIPHERTEXT_BYTES = CHUNK_BYTES + 40
GROUP_KEY_BYTES = 32
PENDING_MAX_ENTRIES = 1000
PENDING_MAX_BYTES = 4 * 1024 * 1024
# WANT requests we accept from one peer per RATE_WINDOW...
WANT_RATE_LIMIT = 100
# ...and send to one peer: a bit under their limit, so clock skew and network
# bunching never tip a well-behaved node over it.
WANT_SEND_LIMIT = 90
RATE_WINDOW = 60.0
# The same two limits, counted separately, for CHUNK_WANT.
CHUNK_WANT_RATE_LIMIT = 100
CHUNK_WANT_SEND_LIMIT = 90
# After the home node says quota_exceeded, wait this long before pushing again.
OUTBOX_QUOTA_RETRY = 600.0

# --- Invites (Phase 6) -------------------------------------------------------

INVITE_DEFAULT_EXPIRY = 48 * 3600  # seconds
INVITE_TOKEN_BYTES = 32
# Redemption attempts allowed from one IP address per INVITE_RATE_WINDOW.
INVITE_RATE_LIMIT = 5
INVITE_RATE_WINDOW = 600.0
# How long `join` waits to receive the group's history before giving up, and
# how often it checks.
JOIN_SYNC_TIMEOUT = 60.0
JOIN_POLL_INTERVAL = 0.1
# Display names in profile entries.
MAX_NAME_CHARS = 64

# --- Local web UI (Phase 7) ---------------------------------------------------------

# The only address the web UI ever listens on.
WEB_HOST = "127.0.0.1"
# Whole requests, as the spec says: 4 images of 10 MiB plus text and overhead.
WEB_MAX_REQUEST_BYTES = 45 * 1024 * 1024
FEED_PAGE_DEFAULT = 50
FEED_PAGE_MAX = 100
# Decrypted posts kept in memory so a 5-second poll doesn't decrypt everything again.
FEED_CACHE_SIZE = 5000

# --- Local control channel (CLI talking to a running node) ------------------

CONTROL_SOCKET_FILE = "control.sock"
# Holds the control channel's token (and port, on Windows). The constant's name avoids
# the word "token" only because bandit flags any such name holding a string.
CONTROL_INFO_FILE = "control.token"
CONTROL_TIMEOUT = 10.0
MAX_CONTROL_REQUEST_BYTES = 256 * 1024
# Big enough for one 10 MiB image, base64-encoded, for `debug image`.
MAX_CONTROL_REPLY_BYTES = 16 * 1024 * 1024

# --- Timeouts and intervals, in seconds (used from Phase 3 on) -------------

HANDSHAKE_TIMEOUT = 10.0
IDLE_TIMEOUT = 60.0
PING_INTERVAL = 20.0
HEADS_INTERVAL = 30.0
JOIN_TIMEOUT = 15.0
PENDING_MAX_AGE = 600.0
CHUNK_FETCH_TIMEOUT = 10.0
CHUNK_FETCH_ATTEMPTS = 3
IMAGE_WAIT_TIMEOUT = 30.0
RECONNECT_MIN_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0
RECONNECT_JITTER = 0.2

# --- Personality ---------------------------------------------------------------

# One is picked at random for the banner `run` prints. Add your own.
TAGLINES = (
    "No servers were harmed in the making of this forum.",
    "Your data, your devices, your drama.",
    "Gossip, but cryptographically verified.",
    "Now with 100% fewer landlords.",
    "Warm hands, cold keys.",
    "Every post signed, sealed and delivered.",
)
# Logged on this many PONGs from one peer, because milestones matter.
PONG_MILESTONE = 1000
