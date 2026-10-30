"""Queries for the store's side tables: chunks, outbox and invites.

`store.py` owns the database, the schema and the log itself (entries, meta,
members). These three small tables each get a class here, built on the same
single connection and the same event-loop thread (see store.py's docstring).
The Store creates one of each: `store.chunk_table`, `store.outbox_table` and
`store.invites`.

Every query is parameterised; nothing here builds SQL from strings.

Joke: the side tables. Not where the main conversation happens, but somebody
has to keep track of the luggage and the guest list.
"""

import sqlite3
from dataclasses import dataclass


class ChunkTable:
    """Which image chunks this node holds, and how big they are (for the quota)."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def add(self, chunk_id: str, size: int) -> None:
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO chunks (chunk_id, size) VALUES (?, ?)", (chunk_id, size))

    def usage(self) -> int:
        """Total bytes of chunks we hold: what the home node's quota counts."""
        return self._db.execute("SELECT COALESCE(SUM(size), 0) FROM chunks").fetchone()[0]

    def ids(self) -> list[str]:
        """Every chunk we hold, for `debug verify` to re-hash."""
        return [row[0] for row in self._db.execute("SELECT chunk_id FROM chunks ORDER BY chunk_id")]


class OutboxTable:
    """Image posts waiting for the home node to hold every chunk (see outbox.py)."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def add(self, created: int, box: str, chunk_ids: str) -> int:
        """Queue a post (its box already encrypted). `chunk_ids` is a JSON list."""
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO outbox (created, plaintext_box, chunk_ids) VALUES (?, ?, ?)",
                (created, box, chunk_ids),
            )
        return cursor.lastrowid

    def rows(self) -> list[tuple[int, int, str, str, str]]:
        """(id, created, box, chunk_ids JSON, acked_ids JSON) for every waiting post, oldest first."""
        return self._db.execute(
            "SELECT id, created, plaintext_box, chunk_ids, acked_ids FROM outbox ORDER BY id"
        ).fetchall()

    def set_acked(self, row_id: int, acked_ids: str) -> None:
        with self._db:
            self._db.execute("UPDATE outbox SET acked_ids = ? WHERE id = ?", (acked_ids, row_id))

    def delete(self, row_id: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))


@dataclass
class InviteRow:
    """One invite. Only the token's hash is ever stored, never the token."""

    token_hash: str
    created: int
    expires: int
    used_by: str | None
    sealed_key: str | None


class InviteTable:
    """Invites created on the home node. Redeeming one is Store.redeem_invite (one transaction)."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def add(self, token_hash: str, created: int, expires: int) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO invites (token_hash, created, expires) VALUES (?, ?, ?)",
                (token_hash, created, expires),
            )

    def get(self, token_hash: str) -> InviteRow | None:
        row = self._db.execute(
            "SELECT token_hash, created, expires, used_by, sealed_key FROM invites WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        return None if row is None else InviteRow(*row)

    def all(self) -> list[InviteRow]:
        rows = self._db.execute(
            "SELECT token_hash, created, expires, used_by, sealed_key FROM invites ORDER BY created"
        ).fetchall()
        return [InviteRow(*row) for row in rows]

    def mark_used(self, token_hash: str, used_by: str) -> bool:
        """Claim an unused invite for `used_by`. False if it was already used. Caller holds the transaction."""
        cursor = self._db.execute(
            "UPDATE invites SET used_by = ? WHERE token_hash = ? AND used_by IS NULL", (used_by, token_hash)
        )
        return cursor.rowcount == 1

    def save_sealed_key(self, token_hash: str, sealed_key: str) -> None:
        """Keep the sealed group key with the invite. Caller holds the transaction."""
        self._db.execute("UPDATE invites SET sealed_key = ? WHERE token_hash = ?", (sealed_key, token_hash))
