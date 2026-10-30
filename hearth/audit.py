"""Looking at the raw log, no keys needed: `debug log` and `debug verify`.

Everything here works on what's visible without the group key: authors,
sequence numbers, kinds, times, hashes and signatures. Post and profile
contents stay sealed (`summarize` only says how big the ciphertext is).

`verify` re-checks every stored entry as if it had just arrived: strict
parsing, signature, membership, the hash chain, and the stored hash itself.
A byte changed in the database by anyone, for any reason, shows up here.
`verify_chunks` re-hashes every image chunk the database says we hold, so a
restored backup (or a tired disk) can be checked end to end.

Joke: an auditor who can't read your diary but can tell exactly which page
was torn out.
"""

from typing import Any

from hearth.chunks import ChunkStore
from hearth.entry import Head, Reason, check_chain, entry_hash, parse_entry, validate_contents
from hearth.store import Store, StoredEntry


def log(store: Store) -> dict[str, Any]:
    """Every stored entry, author by author, for `debug log`. No keys needed: content stays sealed."""
    entries = []
    for author, head in store.heads().items():
        for stored in store.get_range(author, 1, head.seq):
            entries.append({
                "author": stored.author, "seq": stored.seq, "kind": stored.kind,
                "sort_ts": stored.sort_ts, "hash": stored.hash, "summary": summarize(stored),
            })
    return {
        "group": store.group_id(), "admin": store.admin(), "members": len(store.members()),
        "forked": store.forked_authors(), "entries": entries,
    }


def summarize(stored: StoredEntry) -> str:
    """A one-line description of an entry that never needs the group key."""
    parsed = parse_entry(stored.raw.encode("utf-8"))
    body = parsed["body"] if isinstance(parsed, dict) else {}
    if stored.kind == "group_create":
        return "lit the hearth (created the group)"
    if stored.kind == "member_add":
        return f"added member {str(body.get('member'))[:8]}"
    box_size = len(str(body.get("box", "")))
    return f"(encrypted {stored.kind}, {box_size} characters of ciphertext)"


def verify(store: Store) -> dict[str, Any]:
    """Re-check every stored signature, chain link and hash, as if each entry had just arrived."""
    group_id, admin, members = store.group_id(), store.admin(), store.members()
    problems = []
    if group_id is not None:
        for author, head in store.heads().items():
            previous: Head | None = None
            for stored in store.get_range(author, 1, head.seq):
                reason = recheck(stored, previous, members, admin, group_id)
                if reason is not None:
                    problems.append({"author": author, "seq": stored.seq, "code": reason.code,
                                     "message": reason.message})
                previous = Head(stored.seq, stored.hash)
    return {"checked": store.entry_count(), "problems": problems}


def verify_chunks(chunks: ChunkStore) -> dict[str, Any]:
    """Re-hash every chunk the database lists; report each one that's missing or damaged."""
    chunk_ids = chunks.ids()
    problems = []
    for chunk_id in chunk_ids:
        # load() re-hashes the file and returns None unless it matches its ID.
        if chunks.load(chunk_id) is None:
            problem = "damaged" if chunks.has(chunk_id) else "missing"
            problems.append({"chunk": chunk_id, "problem": problem})
    return {"checked": len(chunk_ids), "problems": problems}


def recheck(
    stored: StoredEntry, previous: Head | None, members: set[str], admin: str | None, group_id: str
) -> Reason | None:
    """Validate one stored row as if it had just arrived after `previous`."""
    parsed = parse_entry(stored.raw.encode("utf-8"))
    if isinstance(parsed, Reason):
        return parsed
    reason = validate_contents(parsed, members, admin, group_id)
    if reason is None:
        reason = check_chain(parsed, previous)
    if reason is None and entry_hash(parsed) != stored.hash:
        reason = Reason("hash_mismatch", f"entry #{stored.seq}'s stored hash doesn't match its contents")
    return reason
