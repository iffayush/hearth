# Contributing to Hearth

Hearth's guiding principle is **readability beats cleverness**. Anyone with intermediate Python should be able to understand every line. At 5 to 50 members, performance is never a reason to make code harder to read.

## Setup

Python 3.12 or newer:

```
python -m venv .venv
.venv\Scripts\activate                 (macOS/Linux: source .venv/bin/activate)
pip install -e ".[dev]"
```

## Checks

All of these must pass before a change is finished, and CI runs them on Linux, Windows and macOS:

```
python -m pytest
python -m bandit -q -r hearth
python -m pip_audit --skip-editable
```

Some tests are POSIX-only (the Unix-socket control channel, `fcntl` locks, `0600` permission checks), so Windows skips them and a Windows run doesn't cover them. On Windows, you can run the Linux checks with Docker Desktop, from PowerShell in the project folder:

```
docker run --rm -v "${PWD}:/src:ro" python:3.12-slim sh -c "cp -r /src /work && cd /work && rm -rf .venv && pip install -q --upgrade pip && pip install -q -e '.[dev]' && python -m pytest -q && python -m bandit -q -r hearth && python -m pip_audit --skip-editable"
```

Python 3.12 matters: it's the oldest supported version and what Ubuntu 24.04 ships. A Linux run has already caught a bug that a newer Python on Windows couldn't.

## Code rules

**Dependencies**

- Standard library first. The only third-party packages are `pynacl` and `aiohttp`, plus `pytest`, `pytest-asyncio`, `bandit` and `pip-audit` for development. Adding anything else needs discussion first.
- Versions are pinned in `pyproject.toml`.

**Structure**

- Small files with one job each: 100 to 300 lines, and split at 400.
- Functions under about 40 lines, named for what they do, with type hints everywhere.
- Plain data structures: dataclasses and dicts. No metaclasses, and no decorators beyond `@dataclass` and `@staticmethod`.
- No global mutable state except a single `Node` passed explicitly.
- Every timeout, interval and rate limit is a constructor argument defaulting to a constant in `config.py`, so tests can use tiny values.
- Use `logging`, never `print`, except in the CLI layer.

**Comments and docstrings**

- Every module starts with a docstring: what it does, how it fits in, and one joke.
- Comments explain why, not what.

**Security rules, which aren't optional**

- Only PyNaCl primitives. Never implement or combine raw ciphers by hand.
- Security randomness comes only from `nacl.utils.random` or `secrets`.
- Compare secrets with `hmac.compare_digest` or `sodium_memcmp`.
- Treat every byte from the network as hostile: validate against a schema in `protocol.py`, and parse JSON only through `canonical.py`.
- Never log keys, tokens, passphrases, post text or decrypted images.
- SQL only through parameterised queries.
- Each security rule has at least one test. See [`SECURITY.md`](SECURITY.md) and the security section of [`docs/spec.md`](docs/spec.md).

## Tests

- New behaviour needs a test, and a bug fix needs a test that failed before the fix.
- Don't use `time.sleep` to synchronise. Inject short timeouts, and wait on a condition with `eventually()` from `tests/conftest.py`.
- Never use real keys, addresses or passphrases in tests or fixtures.

## Decisions and docs

- Record every ambiguity you resolve in [`docs/decisions.md`](docs/decisions.md): one entry, with the date, the phase or area, and why.
- If the spec turns out to be wrong or unclear, raise it rather than improvising.
- Update [`PROGRESS.md`](PROGRESS.md) and [`CHANGELOG.md`](CHANGELOG.md) when something notable changes.
- A change to a stored format or the wire protocol must say so plainly, including what happens to existing data.

## Commits and pull requests

- Keep each commit to one change, with a subject line in the imperative that says what it does.
- Work on a branch and open a pull request against `main`. The template lists what's needed.
- **Check what you're staging.** Never commit passphrases, key files, backups, invite links or real addresses. `.gitignore` catches the common ones, but it can't catch everything.
