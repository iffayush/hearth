"""The outer layer of a connection: plain TCP, or a front door that wants more.

Hearth's own handshake (session.py) authenticates and encrypts every
connection, so nothing here is about trust. It's about getting through a front
door such as Tailscale Funnel, which puts a VM that has no public address on
the internet (docs/deploy.md, path C):

- `open_stream(host, port, tls)`: dial, inside TLS for a "tls://" address.
  Funnel only lets TLS through (it routes by the name in the TLS hello). The
  certificate isn't checked, on purpose: the invite link pins the home node's
  key and the handshake inside refuses anyone else, so a check would add ways
  to fail (an old CA bundle, a skewed clock) and no safety.
- `remote_address(...)`: who is really calling. Behind Funnel every friend
  arrives from tailscaled on 127.0.0.1, and Hearth's per-IP limits (8
  connections, 5 invite tries per 10 minutes) would lump the whole group
  together. With `run --proxy-protocol`, the home node reads the PROXY v2
  header the front door sends first (`tailscale funnel --proxy-protocol=2`)
  and counts each friend by their own address.

A PROXY header is believed blindly, so `run` only accepts --proxy-protocol on
a loopback address that nothing but the front door can reach.

Joke: Hearth already has a secret handshake. TLS is just the coat it wears
past the bouncer.
"""

import asyncio
import ipaddress
import logging
import socket
import ssl
import struct

logger = logging.getLogger(__name__)

# The 12 bytes every PROXY v2 header starts with (HAProxy's proxy-protocol.txt, section 2.2).
PROXY_V2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"
# Then: version and command, address family and transport, and the length of what follows.
PROXY_V2_HEADER = struct.Struct("!BBH")
PROXY_COMMAND_LOCAL = 0x0
PROXY_COMMAND_PROXY = 0x1
PROXY_FAMILY_UNSPEC = 0x00
# TCP over IPv4 or IPv6: (bytes per address, bytes for both addresses and both ports).
PROXY_TCP_FAMILIES = {0x11: (4, 12), 0x21: (16, 36)}


class ProxyHeaderError(ValueError):
    """A connection didn't start with a PROXY v2 header we can use. The message says why."""


def client_tls_context() -> ssl.SSLContext:
    """TLS for getting through the front door. The handshake inside does the trusting."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Not checked, on purpose (see the module docstring): the home node's key is pinned instead.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def open_stream(host: str, port: int, tls: bool) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to host:port over IPv4, inside TLS if `tls`. The hostname is resolved again every time."""
    if not tls:
        return await asyncio.open_connection(host, port, family=socket.AF_INET)
    # server_hostname goes out as SNI: it's how Funnel knows which machine we mean.
    return await asyncio.open_connection(host, port, family=socket.AF_INET, ssl=client_tls_context(),
                                         server_hostname=host)


async def read_proxy_header(reader: asyncio.StreamReader) -> tuple[str, int] | None:
    """Read one PROXY v2 header. Returns the caller's (ip, port), or None when the
    front door is speaking for itself (LOCAL, such as a health check). Raises ProxyHeaderError.
    """
    try:
        start = await reader.readexactly(len(PROXY_V2_SIGNATURE))
        if start != PROXY_V2_SIGNATURE:
            if start.startswith(b"PROXY "):
                raise ProxyHeaderError("it sent a PROXY v1 header; Hearth reads v2 (--proxy-protocol=2)")
            raise ProxyHeaderError("it didn't start with a PROXY v2 header (is it the front door?)")
        version_command, family, length = PROXY_V2_HEADER.unpack(await reader.readexactly(PROXY_V2_HEADER.size))
        # Addresses, then optional TLVs (TLS details and such) that we read past.
        rest = await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise ProxyHeaderError("its PROXY header was cut short") from error
    if version_command >> 4 != 2:
        raise ProxyHeaderError(f"its PROXY header has version {version_command >> 4}, not 2")
    command = version_command & 0x0F
    if command == PROXY_COMMAND_LOCAL or (command == PROXY_COMMAND_PROXY and family == PROXY_FAMILY_UNSPEC):
        # The spec's rule for both: use the connection's own addresses.
        return None
    if command != PROXY_COMMAND_PROXY:
        raise ProxyHeaderError(f"its PROXY header has an unknown command ({command})")
    if family not in PROXY_TCP_FAMILIES:
        raise ProxyHeaderError(f"its PROXY header is for something other than TCP (family {family:#04x})")
    size, needed = PROXY_TCP_FAMILIES[family]
    if length < needed:
        raise ProxyHeaderError("its PROXY header's addresses are cut short")
    source = ipaddress.ip_address(rest[:size])
    if isinstance(source, ipaddress.IPv6Address) and source.ipv4_mapped is not None:
        # One friend, one key for the limits, whichever way the front door wrote their IPv4 address.
        source = source.ipv4_mapped
    (port,) = struct.unpack_from("!H", rest, 2 * size)
    return str(source), port


async def remote_address(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, proxy_protocol: bool, timeout: float
) -> tuple[str, int] | None:
    """The caller's (ip, port): from the PROXY header if `proxy_protocol`, else the socket.

    None means drop the connection: its PROXY header was missing, late or wrong (logged).
    """
    peername = writer.get_extra_info("peername")
    direct = (peername[0], peername[1]) if peername else ("unknown", 0)
    if not proxy_protocol:
        return direct
    try:
        async with asyncio.timeout(timeout):
            proxied = await read_proxy_header(reader)
    except (ProxyHeaderError, TimeoutError, ConnectionError) as error:
        logger.warning("Dropped a connection from %s:%d: %s", *direct, str(error) or "no PROXY header in time")
        return None
    return proxied or direct
