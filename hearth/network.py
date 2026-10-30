"""Addresses, dial targets and reconnect backoff: the plumbing under node.py.

- `parse_address("host:port")` for anything we dial (hostnames allowed, and
  resolved again at every dial so a dynamic-DNS change doesn't strand anyone);
- `parse_dial_address()`: the same, plus "tls://host:port" for a home node
  behind a front door that only lets TLS through (Tailscale Funnel; see
  transport.py);
- `parse_listen_address()` for what we listen on: IPv4 literals only (the MVP
  is IPv4-only);
- `next_delay()` and `jittered()`: exponential backoff, 1 s, 2 s, 4 s ...
  capped at 60 s, give or take 20%, so reconnecting nodes don't stampede;
- `ConnectionLimits`: at most 64 inbound connections in total and 8 from any
  one IP address, so one misbehaving machine can't hog the home node;
- `allow_everyone`: an authorizer that lets every authenticated peer in. Nodes
  use real membership; tests of the bare connection layer use this.

Joke: if at first you don't succeed, wait twice as long and try again. Repeat
until 60 seconds, then become serene.
"""

import ipaddress
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# How an address says "wrap me in TLS": config.json, --host, --dial and invite links.
TLS_PREFIX = "tls://"


@dataclass
class DialTarget:
    """A node to keep a connection to. `pubkey`, if set, is pinned: anyone else is refused."""

    host: str
    port: int
    pubkey: str | None = None
    # Wrap the connection in TLS (a "tls://" address): the front door needs it, Hearth doesn't.
    tls: bool = False

    def text(self) -> str:
        """The address as config.json and invite links write it."""
        return f"{TLS_PREFIX if self.tls else ''}{self.host}:{self.port}"


def allow_everyone(pubkey: str) -> bool:
    """Every authenticated peer is welcome. Only for tests of the bare connection layer."""
    return True


def parse_address(text: str) -> tuple[str, int]:
    """Split "host:port". Raises ValueError with a readable reason."""
    host, separator, port_text = text.rpartition(":")
    if not separator or not host:
        raise ValueError(f"{text!r} isn't in host:port form")
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ValueError(f"{port_text!r} isn't a port number (1-65535)")
    return host, int(port_text)


def parse_dial_address(text: str, pubkey: str | None = None) -> DialTarget:
    """Parse "host:port", or "tls://host:port" for a home node behind a TLS front door. Raises ValueError."""
    tls = text.startswith(TLS_PREFIX)
    host, port = parse_address(text.removeprefix(TLS_PREFIX))
    if "/" in host:
        raise ValueError(f"{text!r} isn't host:port or tls://host:port")
    return DialTarget(host, port, pubkey, tls)


def parse_listen_address(text: str) -> tuple[str, int]:
    """Like parse_address, but the host must be an IPv4 address (IPv4-only MVP)."""
    host, port = parse_address(text)
    try:
        ipaddress.IPv4Address(host)
    except ValueError as error:
        raise ValueError(f"listen address {host!r} must be an IPv4 address like 127.0.0.1 or 0.0.0.0") from error
    return host, port


def next_delay(current: float, maximum: float) -> float:
    """The backoff after `current`: double it, but never past `maximum`."""
    return min(current * 2, maximum)


def jittered(delay: float, jitter: float) -> float:
    """`delay` give or take `jitter` (0.2 = ±20%), so reconnecting nodes don't stampede in sync."""
    # Not a security decision, so the plain `random` module is fine here.
    return delay * random.uniform(1 - jitter, 1 + jitter)  # nosec B311


class ConnectionLimits:
    """Counts inbound connections per remote IP and refuses those over the limits."""

    def __init__(self, max_total: int, max_per_ip: int) -> None:
        self._max_total = max_total
        self._max_per_ip = max_per_ip
        self._per_ip: dict[str, int] = {}

    def open(self, ip: str) -> bool:
        """Count a new connection from `ip`, or refuse it (False) if it's over a limit."""
        if sum(self._per_ip.values()) >= self._max_total:
            logger.warning("Refused a connection from %s: already at %d connections", ip, self._max_total)
            return False
        if self._per_ip.get(ip, 0) >= self._max_per_ip:
            logger.warning("Refused a connection from %s: it already has %d. Sharing is caring.", ip, self._max_per_ip)
            return False
        self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
        return True

    def close(self, ip: str) -> None:
        """A connection counted by open() has ended."""
        self._per_ip[ip] -= 1
        if self._per_ip[ip] == 0:
            del self._per_ip[ip]


class AttemptLimiter:
    """At most `limit` attempts per key (an IP address) in any `window` seconds."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Count an attempt by `key`, or refuse it (False) if it has used up its window."""
        attempts = self._attempts.setdefault(key, deque())
        now = self._clock()
        while attempts and attempts[0] < now - self._window:
            attempts.popleft()
        if len(attempts) >= self._limit:
            return False
        attempts.append(now)
        return True
