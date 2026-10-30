"""The Node: owns this member's identity, store, keys and every connection.

It can:

- listen on an IPv4 address and run a `Peer` (responder side) for each caller,
  refusing callers beyond 64 connections in total or 8 from one IP address (with
  `proxy_protocol`, the IP a front door's PROXY v2 header names: transport.py);
- dial other nodes and run a `Peer` (initiator side) for each, redialling with
  exponential backoff (1 s, 2 s, 4 s … capped at 60 s, ±20% jitter) whenever
  the connection drops or can't be made. Hostnames are resolved at every dial,
  so a dynamic-DNS change doesn't strand anyone; "tls://" targets go in TLS;
- sync logs over every ACTIVE connection (`sync.Syncer`), sending HEADS to
  all peers every 30 s as well as whenever something new arrives;
- move image chunks: a member pushes its outbox to the home node and appends
  each image post once every chunk is acknowledged (`outbox.py`), and fetches
  chunks from the home node on demand (`fetcher.py`); the home node accepts
  pushes up to its storage quota and serves chunks (`sync.py`);
- let only members in: after the handshake, `group.is_member` decides. On the
  home node an unknown peer goes to LIMITED, where the only thing it may do is
  redeem an invite (`_on_invite`, at most 5 tries per IP address per 10 min);
- join a group from an invite link (`join`), as a brand-new member;
- answer one-shot CLI commands over the local control channel (`control.py`)
  while `run` holds the data-dir lock.

A member's "home node" is the connected peer whose pubkey is the group admin's:
the home node is the admin, because it's the one that's always online.

Every timeout, interval and limit is a constructor argument defaulting to
`config.py`, so tests can run at 0.05 s speeds. `run` builds one Node and
keeps it running; nothing else is global.

Joke: a node is just a peer that has made friends with an event loop.
"""

import asyncio
import logging
import socket
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from hearth import actions, config, group, invite
from hearth.chunks import ChunkStore
from hearth.entry import now_ms
from hearth.control import ControlServer, start_control_server
from hearth.fetcher import ChunkFetcher
from hearth.identity import Identity
from hearth.network import AttemptLimiter, ConnectionLimits, DialTarget, jittered, next_delay
from hearth.outbox import Outbox, OutboxPost
from hearth.peer import ACTIVE, Peer
from hearth.session import PeerClosed
from hearth.store import Store
from hearth.sync import Syncer
from hearth.transport import open_stream, remote_address

logger = logging.getLogger(__name__)


class Node:
    """One running Hearth node. `await start()`, then `await stop()` when done."""

    def __init__(
        self,
        identity: Identity,
        store: Store,
        chunks: ChunkStore,
        group_key: bytes | None = None,
        is_home: bool = False,
        storage_quota: int = config.DEFAULT_STORAGE_QUOTA_GIB * 2**30,
        listen: tuple[str, int] | None = None,
        proxy_protocol: bool = False,
        dial: list[DialTarget] | None = None,
        control_dir: Path | None = None,
        authorizer: Callable[[str], bool] | None = None,
        handshake_timeout: float = config.HANDSHAKE_TIMEOUT,
        idle_timeout: float = config.IDLE_TIMEOUT,
        ping_interval: float = config.PING_INTERVAL,
        heads_interval: float = config.HEADS_INTERVAL,
        reconnect_min_delay: float = config.RECONNECT_MIN_DELAY,
        reconnect_max_delay: float = config.RECONNECT_MAX_DELAY,
        reconnect_jitter: float = config.RECONNECT_JITTER,
        max_connections: int = config.MAX_CONNECTIONS,
        max_connections_per_ip: int = config.MAX_CONNECTIONS_PER_IP,
        want_rate_limit: int = config.WANT_RATE_LIMIT,
        pending_max_age: float = config.PENDING_MAX_AGE,
        chunk_fetch_timeout: float = config.CHUNK_FETCH_TIMEOUT,
        outbox_quota_retry: float = config.OUTBOX_QUOTA_RETRY,
        join_timeout: float = config.JOIN_TIMEOUT,
        invite_rate_limit: int = config.INVITE_RATE_LIMIT,
        invite_rate_window: float = config.INVITE_RATE_WINDOW,
    ) -> None:
        self.identity = identity
        self.store = store
        self.chunks = chunks
        # None until this node is in a group; posts and the feed need it.
        self.group_key = group_key
        self.is_home = is_home
        self.syncer = Syncer(
            store, chunks=chunks, is_home=is_home, storage_quota=storage_quota,
            want_rate_limit=want_rate_limit, pending_max_age=pending_max_age,
        )
        self.outbox = Outbox(store, chunks, quota_retry=outbox_quota_retry)
        self.fetcher = ChunkFetcher(chunks, self.home_peer, timeout=chunk_fetch_timeout)
        # Where to open the control channel (the data dir); None = no control channel.
        self._control_dir = control_dir
        self._heads_interval = heads_interval
        self._listen = listen
        # Only ever on a loopback listener (`run` checks): a PROXY header is believed blindly.
        self._proxy_protocol = proxy_protocol
        self._dial = dial or []
        # Members only, unless a test of the bare connection layer says otherwise.
        self._authorizer = authorizer or self.is_member
        self._join_timeout = join_timeout
        # Invite redemption attempts per remote IP: 5 per 10 minutes by default.
        self._invite_limiter = AttemptLimiter(invite_rate_limit, invite_rate_window)
        # Set by join(): saves the group key file once INVITE_OK checks out.
        self._save_group_key: Callable[[bytes], None] | None = None
        self._handshake_timeout = handshake_timeout
        self._idle_timeout = idle_timeout
        self._ping_interval = ping_interval
        self._reconnect_min_delay = reconnect_min_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._reconnect_jitter = reconnect_jitter
        self._limits = ConnectionLimits(max_connections, max_connections_per_ip)

        self.peers: set[Peer] = set()
        # When we last heard HEADS or ENTRIES from anyone (Unix ms), for the web UI's status.
        self.last_sync_ms: int | None = None
        self.listen_port: int | None = None
        self._server: asyncio.Server | None = None
        self._control: ControlServer | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    def active_peers(self) -> list[Peer]:
        return [peer for peer in self.peers if peer.state == ACTIVE]

    def is_member(self, pubkey: str) -> bool:
        return group.is_member(self.store, pubkey)

    def connections(self) -> list[dict[str, str]]:
        """Every live connection, for `status`."""
        return [{"pubkey": peer.peer_pubkey or "", "address": peer.address, "state": peer.state}
                for peer in self.peers]

    def home_peer(self) -> Peer | None:
        """Our ACTIVE connection to the home node (the admin), if we have one and aren't it."""
        if self.is_home:
            return None
        admin = self.store.admin()
        for peer in self.active_peers():
            if peer.peer_pubkey == admin:
                return peer
        return None

    def context(self) -> actions.Context:
        """What actions get to work with on this running node."""
        # The home node has nobody to fetch from: what it doesn't hold, nobody does.
        fetcher = None if self.is_home else self.fetcher
        return actions.Context(self.store, self.chunks, self.identity, self.group_key, self.is_home, fetcher,
                               connections=self.connections(), storage_quota=self.syncer.storage_quota,
                               data_dir=self._control_dir)

    # --- Invites -----------------------------------------------------------------

    async def join(self, link: invite.InviteLink, save_group_key: Callable[[bytes], None]) -> Peer:
        """Dial the home node in the link and redeem it. Returns the (running) connection.

        `save_group_key` writes group.key; it's called only once INVITE_OK checks out.
        """
        self._save_group_key = save_group_key
        async with asyncio.timeout(self._handshake_timeout):
            reader, writer = await open_stream(link.host, link.port, link.tls)
        peer = self._new_peer(reader, writer, initiator=True, expected_pubkey=link.home_pubkey, invite_link=link)
        self._spawn(self._run_peer(peer))
        return peer

    async def _on_joined(self, peer: Peer, group_id: str, admin: str, group_key: bytes) -> None:
        """Joiner: INVITE_OK checked out. Save the key and the group, then sync like any member."""
        if self._save_group_key is not None:
            self._save_group_key(group_key)
        self.store.join_group(group_id, admin)
        self.group_key = group_key
        logger.info("Joined group %s; fetching its history from %s", group_id[:8], peer.label())

    async def _on_invite(self, peer: Peer, message: dict[str, Any]) -> dict[str, Any]:
        """Home node: an unknown peer redeems an invite. Returns INVITE_OK, or raises PeerClosed."""
        if not self._invite_limiter.allow(peer.remote_ip):
            raise PeerClosed(f"too many invite attempts from {peer.remote_ip}", "rate_limited", suspicious=True)
        if self.group_key is None or peer.peer_pubkey is None:
            raise PeerClosed("this node can't accept invites (no group key)", "bad_invite")
        try:
            reply = invite.redeem(self.store, self.identity, self.group_key, message["token"],
                                  peer.peer_pubkey, message["enc_pubkey"], now_ms())
        except invite.InviteError as error:
            # The reason doubles as the ERROR text the joiner sees, so it's just the cause.
            raise PeerClosed(str(error), "bad_invite", suspicious=True) from error
        # The new member_add is news for everyone else, too.
        await self.syncer.announce(exclude=peer)
        return reply

    async def start(self) -> None:
        """Open the control channel, start listening (if asked) and begin dialling every target."""
        if self.is_home:
            # Image posts written while this profile was still a member: their chunks are home now.
            self._publish(self.outbox.held_here())
        # Control first: by the time "Listening on" is logged, CLI commands can reach us.
        if self._control_dir is not None:
            self._control = await start_control_server(self._control_dir, self.handle_control)
        if self._listen is not None:
            host, port = self._listen
            self._server = await asyncio.start_server(self._accept, host, port, family=socket.AF_INET)
            # Port 0 means "any free port" (tests use it); report the real one.
            self.listen_port = self._server.sockets[0].getsockname()[1]
            logger.info("Listening on %s:%d as %s", host, self.listen_port, self.identity.short_id)
        for target in self._dial:
            self._spawn(self._dial_forever(target))
        self._spawn(self._heads_loop())

    async def stop(self) -> None:
        """Stop listening, cancel every connection and dial loop, and wait for them."""
        if self._control is not None:
            await self._control.close()
        if self._server is not None:
            self._server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    async def handle_control(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run a one-shot CLI request on this node, then tell peers if anything changed."""
        result = await actions.perform(self.context(), request)
        if request["op"] in actions.WRITES_ENTRY:
            await self.after_local_post(result)
        return result

    async def after_local_post(self, result: dict[str, Any]) -> None:
        """A post was just written here (CLI or web): tell peers, or start uploading its images."""
        if not result.get("waiting"):
            await self.syncer.announce()
            return
        home = self.home_peer()
        if home is not None:
            self._spawn(self._push_outbox(home))

    # --- Peer hooks --------------------------------------------------------------

    async def _on_active(self, peer: Peer) -> None:
        await self.syncer.peer_active(peer)
        if peer is self.home_peer():
            self._spawn(self._push_outbox(peer))

    async def _on_message(self, peer: Peer, message: dict[str, Any]) -> None:
        kind = message["t"]
        if kind in ("HEADS", "ENTRIES"):
            self.last_sync_ms = now_ms()
        if kind in ("CHUNK", "CHUNK_MISSING"):
            self.fetcher.on_reply(message)
        elif kind == "CHUNK_ACK":
            await self._on_chunk_ack(peer, message["id"])
        else:
            await self.syncer.handle(peer, message)

    def _on_closed(self, peer: Peer) -> None:
        self.syncer.peer_closed(peer)
        if peer.error_received == "quota_exceeded":
            self.outbox.mark_home_full()

    # --- Outbox ------------------------------------------------------------------

    async def _push_outbox(self, home: Peer) -> None:
        try:
            sent = await self.outbox.push(home.send)
        except (ConnectionError, RuntimeError):
            return  # the connection dropped; the next one pushes again
        if sent:
            logger.info("Uploading %d chunk(s) of waiting image posts to the home node", sent)

    async def _on_chunk_ack(self, peer: Peer, chunk_id: str) -> None:
        """The home node has a chunk. Any post whose chunks are now all there joins our log."""
        if peer is not self.home_peer():
            logger.info("Dropped CHUNK_ACK from %s: not our home node", peer.label())
            return
        if self._publish(self.outbox.acknowledge(chunk_id)):
            await self.syncer.announce()

    def _publish(self, posts: list[OutboxPost]) -> int:
        """Sign image posts whose chunks are all home into our log. Returns how many made it."""
        appended = 0
        for post in posts:
            try:
                written = actions.append_own(self.store, self.identity, "post", {"box": post.box})
            except actions.ActionError as error:
                logger.error("An uploaded image post couldn't be added to the log: %s", error)
                continue
            self.outbox.remove(post)
            appended += 1
            logger.info("Image post uploaded; it's #%d in your log now", written.seq)
        return appended

    async def _heads_loop(self) -> None:
        """Every heads_interval, remind every peer how far we've got (and tidy the pending queue)."""
        while True:
            await asyncio.sleep(self._heads_interval)
            await self.syncer.resync_all()
            await self.syncer.announce()

    # --- Inbound -----------------------------------------------------------

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Called by asyncio for each new inbound connection, in its own task."""
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            remote = await remote_address(reader, writer, self._proxy_protocol, self._handshake_timeout)
            if remote is None or not self._limits.open(remote[0]):
                writer.close()
                return
            try:
                await self._run_peer(self._new_peer(reader, writer, initiator=False, expected_pubkey=None, remote=remote))
            finally:
                self._limits.close(remote[0])
        finally:
            if task is not None:
                self._tasks.discard(task)

    # --- Outbound ----------------------------------------------------------

    async def _dial_forever(self, target: DialTarget) -> None:
        """Keep one connection to `target` alive, backing off while it's unreachable."""
        delay = self._reconnect_min_delay
        while True:
            peer = await self._dial_once(target)
            if peer is not None and peer.became_active.is_set():
                # We had a real session, so this is a fresh drop: start the backoff over.
                delay = self._reconnect_min_delay
            wait = jittered(delay, self._reconnect_jitter)
            logger.info("Will try %s:%d again in %.1f s", target.host, target.port, wait)
            await asyncio.sleep(wait)
            delay = next_delay(delay, self._reconnect_max_delay)

    async def _dial_once(self, target: DialTarget) -> Peer | None:
        try:
            async with asyncio.timeout(self._handshake_timeout):
                # The hostname is resolved again on every dial, on purpose.
                reader, writer = await open_stream(target.host, target.port, target.tls)
        except (OSError, TimeoutError) as error:
            logger.info("Can't reach %s:%d (%s)", target.host, target.port, error or type(error).__name__)
            return None
        peer = self._new_peer(reader, writer, initiator=True, expected_pubkey=target.pubkey)
        await self._run_peer(peer)
        return peer

    # --- Shared ------------------------------------------------------------

    def _new_peer(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        initiator: bool,
        expected_pubkey: str | None,
        invite_link: invite.InviteLink | None = None,
        remote: tuple[str, int] | None = None,
    ) -> Peer:
        return Peer(
            reader,
            writer,
            self.identity,
            remote=remote,
            initiator=initiator,
            authorizer=self._authorizer,
            expected_pubkey=expected_pubkey,
            invite=invite_link,
            handshake_timeout=self._handshake_timeout,
            idle_timeout=self._idle_timeout,
            ping_interval=self._ping_interval,
            join_timeout=self._join_timeout,
            on_active=self._on_active,
            on_message=self._on_message,
            on_closed=self._on_closed,
            # Only the home node (the admin) takes invites; elsewhere strangers are just turned away.
            on_invite=self._on_invite if self.is_home else None,
            on_joined=self._on_joined,
        )

    async def _run_peer(self, peer: Peer) -> None:
        self.peers.add(peer)
        try:
            await peer.run()
        finally:
            self.peers.discard(peer)

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
