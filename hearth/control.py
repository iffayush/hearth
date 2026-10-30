"""The local control channel: how a one-shot CLI command talks to a running node.

While `run` holds the data-dir lock, nothing else may open the database or the
key files. So `post`, `feed` and friends first try the lock. If it's free they
do the work themselves; if it's held they send their request here and the
running node does it. One writer per log, no second passphrase prompt.

Transport:
- POSIX: a Unix socket at `<data-dir>/control.sock` (the folder is 0700).
- Windows: a TCP port on 127.0.0.1 (asyncio has no Unix sockets there).

Either way, the node writes a random 32-byte token (and, on Windows, the port)
to `<data-dir>/control.token` with mode 0600, and every request must carry the
token, compared in constant time. A request and its reply are one JSON line
each, parsed with the same strict parser as network messages.

Joke: the node's intercom. Please state your token and the nature of your post.
"""

import asyncio
import hmac
import logging
import os
import secrets
import socket
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hearth import canonical, config
from hearth.actions import ActionError
from hearth.keyfile import write_private_file

logger = logging.getLogger(__name__)

ControlHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
# The token file is tiny; refuse to read anything bigger.
MAX_TOKEN_FILE_BYTES = 1024
# The OS's limit on a Unix socket's path: 104 bytes on macOS, 108 on Linux.
MAX_SOCKET_PATH_BYTES = 104


class ControlError(Exception):
    """A control request failed. The message is shown to the user as-is."""


def uses_unix_socket() -> bool:
    return sys.platform != "win32"


@dataclass
class ControlServer:
    """A running control channel. close() stops it and removes its files."""

    server: asyncio.Server
    data_dir: Path

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        (self.data_dir / config.CONTROL_INFO_FILE).unlink(missing_ok=True)
        if uses_unix_socket():
            (self.data_dir / config.CONTROL_SOCKET_FILE).unlink(missing_ok=True)


async def start_control_server(
    data_dir: Path, handler: ControlHandler, timeout: float = config.CONTROL_TIMEOUT
) -> ControlServer:
    """Start listening for control requests and publish the token (and port) for the CLI."""
    token = secrets.token_hex(32)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await serve_one_request(reader, writer, token, handler, timeout)

    port = None
    if uses_unix_socket():
        socket_path = data_dir / config.CONTROL_SOCKET_FILE
        # We hold the data-dir lock, so a leftover socket file is from a crashed run.
        socket_path.unlink(missing_ok=True)
        try:
            server = await asyncio.start_unix_server(
                serve, path=str(socket_path), limit=config.MAX_CONTROL_REQUEST_BYTES
            )
        except OSError as error:
            if len(os.fsencode(socket_path)) < MAX_SOCKET_PATH_BYTES:
                raise
            raise ControlError(
                f"{data_dir} is too long a path for this OS's control socket; use a shorter --data-dir"
            ) from error
        os.chmod(socket_path, 0o600)
    else:
        server = await asyncio.start_server(
            serve, "127.0.0.1", 0, family=socket.AF_INET, limit=config.MAX_CONTROL_REQUEST_BYTES
        )
        port = server.sockets[0].getsockname()[1]
    write_private_file(data_dir / config.CONTROL_INFO_FILE, canonical.dumps({"port": port, "token": token}))
    return ControlServer(server, data_dir)


async def serve_one_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    token: str,
    handler: ControlHandler,
    timeout: float,
) -> None:
    """Read one request line, check the token, run it, write one reply line, hang up."""
    try:
        async with asyncio.timeout(timeout):
            request = parse_request(await read_request_line(reader), token)
            reply = {"ok": True, "result": await handler(request)}
    except (ControlError, ActionError) as error:
        reply = {"ok": False, "error": str(error)}
    except TimeoutError:
        reply = {"ok": False, "error": "control request timed out"}
    except Exception:
        logger.exception("Control request failed")
        reply = {"ok": False, "error": "internal error; see the node's log"}
    try:
        writer.write(canonical.dumps(reply) + b"\n")
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def read_request_line(reader: asyncio.StreamReader) -> bytes:
    try:
        return await reader.readline()
    except ValueError as error:
        # readline() raises ValueError when a line is longer than the stream's limit.
        raise ControlError("control request is too large") from error


def parse_request(line: bytes, expected_token: str) -> dict[str, Any]:
    """Strictly parse a request line and check its token. Returns it without the token."""
    try:
        request = canonical.loads(line)
    except canonical.CanonicalError as error:
        raise ControlError(f"control request is not strict JSON: {error}") from error
    if not isinstance(request, dict) or not isinstance(request.get("token"), str):
        raise ControlError("control request needs a token")
    if not hmac.compare_digest(request.pop("token"), expected_token):
        logger.warning("Refused a control request with a wrong token")
        raise ControlError("wrong control token")
    return request


async def send_request(
    data_dir: Path, request: dict[str, Any], timeout: float = config.CONTROL_TIMEOUT, token: str | None = None
) -> dict[str, Any]:
    """Send one request to the node running in `data_dir` and return its result.

    `token` overrides the one in control.token (only tests do that).
    """
    port, file_token = read_token_file(data_dir)
    message = {**request, "token": token if token is not None else file_token}
    try:
        async with asyncio.timeout(timeout):
            if port is None:
                reader, writer = await asyncio.open_unix_connection(
                    str(data_dir / config.CONTROL_SOCKET_FILE), limit=config.MAX_CONTROL_REPLY_BYTES
                )
            else:
                reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=config.MAX_CONTROL_REPLY_BYTES)
            writer.write(canonical.dumps(message) + b"\n")
            await writer.drain()
            line = await reader.readline()
            writer.close()
    except (OSError, TimeoutError) as error:
        raise ControlError(f"couldn't reach the running node: {error or 'timed out'}") from error
    try:
        reply = canonical.loads(line)
    except canonical.CanonicalError as error:
        raise ControlError("the running node hung up without a proper reply") from error
    if not isinstance(reply, dict):
        raise ControlError("bad reply from the running node")
    if reply.get("ok") is not True or not isinstance(reply.get("result"), dict):
        raise ControlError(str(reply.get("error", "bad reply from the running node")))
    return reply["result"]


def read_token_file(data_dir: Path) -> tuple[int | None, str]:
    """Return (port, token) from control.token: port is None for a Unix socket."""
    path = data_dir / config.CONTROL_INFO_FILE
    try:
        if path.stat().st_size > MAX_TOKEN_FILE_BYTES:
            raise ControlError(f"{path} is too large to be a control token file")
        info = canonical.loads(path.read_bytes())
    except FileNotFoundError as error:
        raise ControlError(
            "a node is running on this data directory but its control channel isn't up "
            "(still starting, or waiting for its passphrase?). Try again in a moment."
        ) from error
    except canonical.CanonicalError as error:
        raise ControlError(f"{path} is damaged: {error}") from error
    if not isinstance(info, dict):
        raise ControlError(f"{path} is damaged")
    port, token = info.get("port"), info.get("token")
    if not isinstance(token, str) or not (port is None or canonical.is_int(port)):
        raise ControlError(f"{path} is damaged")
    return port, token
