"""The SQLite store: schema, and every query Hearth makes.

One file per node, `<data-dir>/hearth.db`. The `entries` table holds every
author's log; `meta` holds the group ID, the admin's pubkey and the list of
forked authors; `members` is the membership list built from the admin's
`group_create` and `member_add` entries. The side tables (chunks, outbox,
invites) have their queries in tables.py: `store.chunk_table`,
`store.outbox_table` and `store.invites`.

**One connection, one thread.** The Node owns a single `sqlite3` connection
and all access happens on the event-loop thread: no thread pool, no
`to_thread`. At 5 to 50 members every query takes microseconds, so blocking
the loop for one is cheaper than the locking a thread pool would need.

**Appending** (`append_entry`) is the only way entries get in. It looks up the
author's Head and the member set, calls the pure validators in `entry.py`, and
writes inside one transaction, so a crash never leaves an entry without its
membership change, or the reverse.

Joke: an append-only log is a diary with no eraser. Very healthy, emotionally.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from hearth import canonical
from hearth.entry import Head, Reason, check_chain, entry_hash, now_ms, parse_entry, validate_contents
from hearth.tables import ChunkTable, InviteTable, OutboxTable

# 2 since Phase 5: post bodies became encrypted boxes.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE meta    (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE entries (
  author TEXT NOT NULL, seq INTEGER NOT NULL,
  hash TEXT NOT NULL UNIQUE, prev TEXT, ts INTEGER NOT NULL,
  sort_ts INTEGER NOT NULL,
  kind TEXT NOT NULL, raw TEXT NOT NULL,
  PRIMARY KEY (author, seq));
CREATE INDEX entries_feed ON entries (kind, sort_ts DESC, author, seq);
CREATE TABLE members (pubkey TEXT PRIMARY KEY, added_seq INTEGER NOT NULL);
CREATE TABLE chunks  (chunk_id TEXT PRIMARY KEY, size INTEGER NOT NULL);
CREATE TABLE outbox  (id INTEGER PRIMARY KEY, created INTEGER NOT NULL,
                      plaintext_box TEXT NOT NULL, chunk_ids TEXT NOT NULL,
                      acked_ids TEXT NOT NULL DEFAULT '[]');
CREATE TABLE invites (token_hash TEXT PRIMARY KEY, created INTEGER NOT NULL,
                      expires INTEGER NOT NULL, used_by TEXT,
                      sealed_key TEXT);
"""

# Every query spells out its columns in this order (author, seq, hash, prev,
# ts, sort_ts, kind, raw), matching StoredEntry, so rows unpack straight into it.
# They are written out in full rather than formatted in, so no SQL string is
# ever built at runtime.

# What append_entry() did with an entry.
ADDED = "added"
DUPLICATE = "duplicate"
REJECTED = "rejected"
FORKED = "forked"


class StoreError(Exception):
    """The database can't be used: wrong schema version, or a group-setup mistake."""


@dataclass
class StoredEntry:
    """One row of the entries table. `raw` is the canonical JSON of the whole entry."""

    author: str
    seq: int
    hash: str
    prev: str | None
    ts: int
    sort_ts: int
    kind: str
    raw: str


@dataclass
class AppendResult:
    """The outcome of append_entry(): one of ADDED, DUPLICATE, REJECTED, FORKED."""

    status: str
    reason: Reason | None = None


class Store:
    """All database access. Create with Store.open(); close() when done."""

    def __init__(self, connection: sqlite3.Connection, clock: Callable[[], int] = now_ms) -> None:
        self._db = connection
        # Injected so tests can control "now" when checking sort_ts.
        self._clock = clock
        self.chunk_table = ChunkTable(connection)
        self.outbox_table = OutboxTable(connection)
        self.invites = InviteTable(connection)

    @staticmethod
    def open(path: Path, clock: Callable[[], int] = now_ms) -> "Store":
        """Open (creating if needed) the database at `path`."""
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        store = Store(connection, clock)
        store._create_or_check_schema()
        return store

    def close(self) -> None:
        self._db.close()

    def snapshot(self, path: Path) -> None:
        """Copy the whole database to `path` as one consistent snapshot (`backup` uses this).

        SQLite's online backup API reads every page inside one read transaction,
        so the copy is whole even if the node writes meanwhile. Copying the file
        itself could catch it mid-write, with half the change still in the WAL.
        """
        target = sqlite3.connect(path)
        try:
            self._db.backup(target)
        finally:
            target.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # --- Meta --------------------------------------------------------------

    def group_id(self) -> str | None:
        return self._get_meta("group_id")

    def admin(self) -> str | None:
        return self._get_meta("admin")

    def forked_authors(self) -> list[str]:
        return canonical.loads(self._get_meta("forked_authors") or "[]")

    def mark_forked(self, author: str) -> None:
        """Remember that two different histories exist for `author`. Idempotent."""
        forked = self.forked_authors()
        if author not in forked:
            forked.append(author)
            with self._db:
                self._set_meta("forked_authors", canonical.dumps(sorted(forked)).decode("utf-8"))

    # --- Reading entries ---------------------------------------------------

    def get_entry(self, author: str, seq: int) -> StoredEntry | None:
        row = self._db.execute(
            "SELECT author, seq, hash, prev, ts, sort_ts, kind, raw FROM entries "
            "WHERE author = ? AND seq = ?",
            (author, seq),
        ).fetchone()
        return None if row is None else StoredEntry(*row)

    def get_range(self, author: str, first: int, last: int) -> list[StoredEntry]:
        """One author's entries with first <= seq <= last, in seq order."""
        rows = self._db.execute(
            "SELECT author, seq, hash, prev, ts, sort_ts, kind, raw FROM entries "
            "WHERE author = ? AND seq BETWEEN ? AND ? ORDER BY seq",
            (author, first, last),
        ).fetchall()
        return [StoredEntry(*row) for row in rows]

    def head(self, author: str) -> Head | None:
        """The author's latest entry, or None if we hold nothing from them."""
        row = self._db.execute(
            "SELECT seq, hash FROM entries WHERE author = ? ORDER BY seq DESC LIMIT 1", (author,)
        ).fetchone()
        return None if row is None else Head(row[0], row[1])

    def posts(self, limit: int, before: tuple[int, str, int] | None = None) -> list[StoredEntry]:
        """Up to `limit` posts, newest first, strictly older than the cursor `before`.

        Order and cursor both use the tuple (sort_ts, author, seq), compared
        left to right. sort_ts never changes once stored and (author, seq) is
        unique, so paging never skips or repeats a post, ties included.
        """
        if before is None:
            before = (2**63 - 1, "", 0)  # "newer than anything": the first page
        rows = self._db.execute(
            "SELECT author, seq, hash, prev, ts, sort_ts, kind, raw FROM entries "
            "WHERE kind = 'post' AND (sort_ts, author, seq) < (?, ?, ?) "
            "ORDER BY sort_ts DESC, author DESC, seq DESC LIMIT ?",
            (*before, limit),
        ).fetchall()
        return [StoredEntry(*row) for row in rows]

    def latest_profiles(self) -> dict[str, str]:
        """Each author's newest profile entry (raw), for display names: the latest one wins."""
        rows = self._db.execute(
            "SELECT author, raw FROM entries e WHERE kind = 'profile' AND seq = "
            "(SELECT MAX(seq) FROM entries WHERE author = e.author AND kind = 'profile')"
        ).fetchall()
        return dict(rows)

    def entry_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def heads(self) -> dict[str, Head]:
        """Every author's Head. Logs have no gaps, so the max seq is the head."""
        rows = self._db.execute(
            "SELECT author, seq, hash FROM entries e WHERE seq = "
            "(SELECT MAX(seq) FROM entries WHERE author = e.author) ORDER BY author"
        ).fetchall()
        return {author: Head(seq, hash_) for author, seq, hash_ in rows}

    # --- Membership --------------------------------------------------------

    def members(self) -> set[str]:
        return {row[0] for row in self._db.execute("SELECT pubkey FROM members")}

    def add_member(self, pubkey: str, added_seq: int) -> None:
        """Record a member. Adding someone twice keeps the first `added_seq`."""
        with self._db:
            self._insert_member(pubkey, added_seq)

    # --- Writing entries ---------------------------------------------------

    def create_group(self, raw: bytes) -> AppendResult:
        """Start a group from the admin's signed `group_create` entry.

        Sets meta.group_id and meta.admin from the entry and appends it, all in
        one transaction. Refuses if this store already belongs to a group.
        """
        if self.group_id() is not None:
            raise StoreError(f"this node already belongs to group {self.group_id()[:8]}…")
        parsed = parse_entry(raw)
        if isinstance(parsed, Reason):
            return AppendResult(REJECTED, parsed)
        if not isinstance(parsed, dict) or parsed.get("kind") != "group_create":
            return AppendResult(REJECTED, Reason("bad_kind", "a group must start with a group_create entry"))
        # The entry vouches for its own group and admin; validation checks the
        # signature and that author == body.admin before we believe either.
        group_id, admin = parsed.get("group"), parsed.get("author")
        reason = validate_contents(parsed, set(), admin, group_id)  # type: ignore[arg-type]
        if reason is None:
            reason = check_chain(parsed, None)
        if reason is not None:
            return AppendResult(REJECTED, reason)
        with self._db:
            self._set_meta("group_id", group_id)
            self._set_meta("admin", admin)
            self._insert_entry(parsed)
        return AppendResult(ADDED)

    def join_group(self, group_id: str, admin: str) -> None:
        """Record which group this node belongs to and who its admin is, nothing more.

        The admin's entries arrive later through sync. (Phase 4's stand-in for
        invites; Phase 6 sets these from INVITE_OK instead.)
        """
        if self.group_id() is not None:
            raise StoreError(f"this node already belongs to group {self.group_id()[:8]}…")
        with self._db:
            self._set_meta("group_id", group_id)
            self._set_meta("admin", admin)

    def redeem_invite(self, token_hash: str, joiner: str, member_add_raw: bytes, sealed_key: str) -> None:
        """Claim an invite for `joiner`, append the admin's member_add, keep the sealed key: all or nothing.

        One transaction, so a crash anywhere in the middle leaves the invite
        unused and no member_add behind. Raises StoreError if the invite is
        already used or the member_add isn't the admin's valid next entry.
        """
        parsed = parse_entry(member_add_raw)
        group_id = self.group_id()
        if isinstance(parsed, Reason) or group_id is None:
            raise StoreError("the member_add entry can't be read, or there's no group")
        reason = validate_contents(parsed, self.members(), self.admin(), group_id)
        if reason is None:
            reason = check_chain(parsed, self.head(parsed["author"]))
        if reason is not None or parsed["kind"] != "member_add":
            raise StoreError(f"bad member_add for the invite: {reason.message if reason else parsed['kind']}")
        with self._db:
            if not self.invites.mark_used(token_hash, joiner):
                raise StoreError("that invite has already been used")
            self._insert_entry(parsed)
            self.invites.save_sealed_key(token_hash, sealed_key)

    def append_entry(self, raw: bytes) -> AppendResult:
        """Validate one raw entry and, if it's the author's next one, store it."""
        group_id = self.group_id()
        if group_id is None:
            return AppendResult(REJECTED, Reason("no_group", "this node isn't in a group yet"))
        parsed = parse_entry(raw)
        if isinstance(parsed, Reason):
            return AppendResult(REJECTED, parsed)
        reason = validate_contents(parsed, self.members(), self.admin(), group_id)
        if reason is not None:
            return AppendResult(REJECTED, reason)
        # From here on the entry is genuine: correctly signed by a member.
        head = self.head(parsed["author"])
        if head is not None and parsed["seq"] <= head.seq:
            return self._compare_with_stored(parsed)
        reason = check_chain(parsed, head)
        if reason is not None:
            if head is not None and reason.code == "bad_prev":
                # A genuine next entry that links to something we don't hold:
                # another copy of this identity has written a different history.
                return self._fork_result(parsed["author"], reason)
            return AppendResult(REJECTED, reason)
        with self._db:
            self._insert_entry(parsed)
        return AppendResult(ADDED)

    # --- Internals ---------------------------------------------------------

    def _compare_with_stored(self, parsed: dict[str, Any]) -> AppendResult:
        """A genuine entry for a seq we already hold: identical is fine, different is a fork."""
        stored = self.get_entry(parsed["author"], parsed["seq"])
        if stored is not None and stored.hash == entry_hash(parsed):
            return AppendResult(DUPLICATE)
        reason = Reason(
            "fork",
            f"entry #{parsed['seq']} by {parsed['author'][:8]} differs from the one we hold. "
            "Two copies of one identity are writing; that's a fork.",
        )
        return self._fork_result(parsed["author"], reason)

    def _fork_result(self, author: str, reason: Reason) -> AppendResult:
        self.mark_forked(author)
        return AppendResult(FORKED, reason)

    def _insert_entry(self, parsed: dict[str, Any]) -> None:
        """Write one validated entry plus its membership effect. Caller holds the transaction."""
        # sort_ts is fixed forever at first store: a future-dated entry shows
        # when it arrived, and the feed order never shifts between requests.
        sort_ts = min(parsed["ts"], self._clock())
        self._db.execute(
            "INSERT INTO entries (author, seq, hash, prev, ts, sort_ts, kind, raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                parsed["author"], parsed["seq"], entry_hash(parsed), parsed["prev"],
                parsed["ts"], sort_ts, parsed["kind"],
                # Re-encode instead of keeping the received bytes, so every
                # node stores (and relays) byte-identical entries.
                canonical.dumps(parsed).decode("utf-8"),
            ),
        )
        if parsed["kind"] == "group_create":
            self._insert_member(parsed["author"], parsed["seq"])
        elif parsed["kind"] == "member_add":
            self._insert_member(parsed["body"]["member"], parsed["seq"])

    def _insert_member(self, pubkey: str, added_seq: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO members (pubkey, added_seq) VALUES (?, ?)", (pubkey, added_seq)
        )

    def _get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _create_or_check_schema(self) -> None:
        has_meta = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if has_meta is None:
            with self._db:
                # executescript() would commit on its own; run each statement instead.
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        self._db.execute(statement)
                self._set_meta("schema_version", str(SCHEMA_VERSION))
                self._set_meta("forked_authors", "[]")
            return
        version = self._get_meta("schema_version")
        if version != str(SCHEMA_VERSION):
            raise StoreError(
                f"database schema version is {version}, this code needs {SCHEMA_VERSION}. "
                "Pre-MVP data isn't migrated: delete the data directory and start again."
            )

