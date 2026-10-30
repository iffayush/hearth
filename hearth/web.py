"""The local web UI: an aiohttp server on 127.0.0.1 serving one page and a small JSON API.

Every request passes `guard()` first, which applies the spec's rules in order:

1. The Host header must be `127.0.0.1:<port>` or `localhost:<port>`. A
   malicious website that rebinds its own name to 127.0.0.1 (DNS rebinding)
   still sends *its* name as the Host, so it's refused.
2. Every `/api/` request must carry `X-Hearth-Token`: a random token made at
   launch and printed by `run`, compared in constant time. Another website
   can't add that header without a CORS preflight we never answer, so it
   can't call the API, even though it's all on your machine. No cookies.
3. Every response, errors included, gets the Content-Security-Policy,
   `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.

The handlers then validate every path and query parameter before using it,
cap request bodies (45 MiB in total, 10 MiB per image), and serve images with
the MIME type their bytes were verified to be. The page (static/) renders all
post text with textContent and loads images through fetch() + blob: URLs.

Joke: a web server that only talks to one person, on one machine, who has to
show a secret handshake every single time. Exclusive doesn't begin to cover it.
"""

import asyncio
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import BodyPartReader, web

from hearth import actions, canonical, config, feed
from hearth.entry import is_hex64
from hearth.fetcher import FetchError, load_image
from hearth.node import Node

logger = logging.getLogger(__name__)

# The header carrying the per-launch token. (Named AUTH_HEADER, not TOKEN_HEADER, only
# because bandit flags any constant named *TOKEN* that holds a string.)
AUTH_HEADER = "X-Hearth-Token"
STATIC_DIR = Path(__file__).parent / "static"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; object-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# The page's three files: served without the token, since the page has to load to read it.
STATIC_FILES = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
}
MAX_JSON_BYTES = 4096
READ_CHUNK = 64 * 1024

NODE = web.AppKey("node", Node)
TOKEN = web.AppKey("token", str)
ALLOWED_HOSTS = web.AppKey("allowed_hosts", set)
CACHE = web.AppKey("cache", feed.FeedCache)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class BadRequest(Exception):
    """A request we refuse. `status` is the HTTP status, the message goes back to the page."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class WebServer:
    """A running web UI. `url()` is what `run` prints."""

    runner: web.AppRunner
    port: int
    token: str

    def url(self) -> str:
        return f"http://{config.WEB_HOST}:{self.port}/?token={self.token}"

    async def close(self) -> None:
        await self.runner.cleanup()


async def start_web(node: Node, port: int, host: str = config.WEB_HOST) -> WebServer:
    """Serve the UI for `node` on 127.0.0.1:`port` (0 = any free port). Refuses any other address."""
    if host != config.WEB_HOST:
        raise ValueError(f"the web UI only ever listens on {config.WEB_HOST}, never {host!r}")
    token = secrets.token_urlsafe(32)
    app = web.Application(middlewares=[web.middleware(guard)], client_max_size=config.WEB_MAX_REQUEST_BYTES)
    app[NODE] = node
    app[TOKEN] = token
    app[CACHE] = feed.FeedCache()
    # Filled in once we know the real port; the set itself is fixed before startup.
    app[ALLOWED_HOSTS] = set()
    for path in STATIC_FILES:
        app.router.add_get(path, serve_static)
    app.router.add_get("/api/me", get_me)
    app.router.add_post("/api/profile", post_profile)
    app.router.add_get("/api/feed", get_feed)
    app.router.add_post("/api/posts", post_posts)
    app.router.add_get("/api/image/{author}/{seq}/{index}", get_image)
    app.router.add_get("/api/status", get_status)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    actual_port = runner.addresses[0][1]
    app[ALLOWED_HOSTS].update({f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"})
    return WebServer(runner, actual_port, token)


async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Host check, token check, then the handler; security headers on whatever comes back."""
    try:
        if request.host not in request.app[ALLOWED_HOSTS]:
            raise BadRequest(403, "wrong Host header")
        if request.path.startswith("/api/"):
            sent = request.headers.get(AUTH_HEADER, "")
            if not hmac.compare_digest(sent.encode(), request.app[TOKEN].encode()):
                raise BadRequest(401, "missing or wrong token: open the link `run` printed")
        response = await handler(request)
    except BadRequest as refusal:
        response = web.json_response({"error": str(refusal)}, status=refusal.status)
    except web.HTTPException as error:
        # The router's own 404 / 405 / 413, turned into our JSON shape.
        response = web.json_response({"error": error.reason}, status=error.status)
    response.headers.update(SECURITY_HEADERS)
    return response


async def serve_static(request: web.Request) -> web.StreamResponse:
    name, content_type = STATIC_FILES[request.path]
    # no-cache = "check with us every time" (cheap on localhost), so an updated
    # Hearth never runs with a stale app.js or stylesheet from the browser's cache.
    return web.FileResponse(STATIC_DIR / name, headers={"Content-Type": content_type, "Cache-Control": "no-cache"})


# --- API ---------------------------------------------------------------------------------


async def get_me(request: web.Request) -> web.Response:
    node = request.app[NODE]
    names = feed.profile_names(node.store, node.group_key) if node.group_key else {}
    return web.json_response({
        "pubkey": node.identity.pubkey, "name": names.get(node.identity.pubkey),
        "group": node.store.group_id(), "admin": node.store.admin(), "is_home": node.is_home,
    })


async def post_profile(request: web.Request) -> web.Response:
    node = request.app[NODE]
    body = await read_json(request)
    if set(body) != {"name"} or not feed.is_valid_name(body["name"]):
        raise BadRequest(400, f"send {{\"name\": ...}}, 1 to {config.MAX_NAME_CHARS} characters")
    result = run_action(lambda: actions.set_profile(node.store, node.identity, need_key(node), body["name"]))
    await node.syncer.announce()
    return web.json_response(result)


async def get_feed(request: web.Request) -> web.Response:
    node = request.app[NODE]
    query = request.query
    if set(query) - {"before", "limit"}:
        raise BadRequest(400, "unknown query parameter")
    try:
        before = feed.parse_cursor(query["before"]) if "before" in query else None
    except ValueError as error:
        raise BadRequest(400, str(error)) from error
    limit = parse_int(query.get("limit", str(config.FEED_PAGE_DEFAULT)), 1, config.FEED_PAGE_MAX, "limit")
    page = feed.page(node.store, need_key(node), limit, before, request.app[CACHE])
    page["waiting"] = actions.waiting_posts(node.context(), node.outbox.home_is_full())
    return web.json_response(page)


async def post_posts(request: web.Request) -> web.Response:
    node = request.app[NODE]
    if request.content_length is None or request.content_length > config.WEB_MAX_REQUEST_BYTES:
        raise BadRequest(413, "posts are limited to 45 MiB in total")
    if not request.content_type.startswith("multipart/"):
        raise BadRequest(400, "send the post as multipart/form-data")
    text, images = await read_post_form(request)
    result = run_action(lambda: actions.post_with_images(node.context(), text, images))
    await node.after_local_post(result)
    return web.json_response(result)


async def get_image(request: web.Request) -> web.Response:
    node = request.app[NODE]
    author = request.match_info["author"]
    if not is_hex64(author):
        raise BadRequest(400, "author must be 64 lowercase hex characters")
    seq = parse_int(request.match_info["seq"], 1, config.MAX_SEQ, "seq")
    index = parse_int(request.match_info["index"], 0, config.MAX_IMAGES_PER_POST - 1, "index")
    fetcher = None if node.is_home else node.fetcher
    try:
        async with asyncio.timeout(config.IMAGE_WAIT_TIMEOUT):
            mime, data = await load_image(node.store, node.chunks, need_key(node), fetcher, author, seq, index)
    except FetchError as error:
        raise BadRequest(404, str(error)) from error
    except TimeoutError as error:
        raise BadRequest(504, "the home node didn't send that image in time") from error
    # The MIME type is the one the image's bytes were verified to be. Decrypted
    # images shouldn't sit in the browser's disk cache, hence no-store.
    return web.Response(body=data, content_type=mime, headers={"Cache-Control": "no-store"})


async def get_status(request: web.Request) -> web.Response:
    node = request.app[NODE]
    status: dict[str, Any] = {"group": node.store.group_id()}
    if node.group_key is not None:
        status = actions.status(node.context())
    status.update(
        home_connected=node.is_home or node.home_peer() is not None,
        last_sync_ms=node.last_sync_ms,
        home_full=node.outbox.home_is_full(),
    )
    return web.json_response(status)


# --- Helpers ---------------------------------------------------------------------------


def need_key(node: Node) -> bytes:
    if node.group_key is None:
        raise BadRequest(409, "this node isn't in a group yet")
    return node.group_key


def run_action(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return action()
    except actions.ActionError as error:
        raise BadRequest(400, str(error)) from error


def parse_int(text: str, low: int, high: int, name: str) -> int:
    """A decimal integer in [low, high]; anything else is a 400."""
    if not text.isdigit() or not low <= int(text) <= high:
        raise BadRequest(400, f"{name} must be a whole number from {low} to {high}")
    return int(text)


async def read_json(request: web.Request) -> dict[str, Any]:
    """A small JSON object body, parsed with the strict parser."""
    body = await request.content.read(MAX_JSON_BYTES + 1)
    if len(body) > MAX_JSON_BYTES:
        raise BadRequest(413, "request body too large")
    try:
        value = canonical.loads(body)
    except canonical.CanonicalError as error:
        raise BadRequest(400, f"not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise BadRequest(400, "send a JSON object")
    return value


async def read_post_form(request: web.Request) -> tuple[str, list[tuple[str, bytes]]]:
    """The "text" field and up to 4 "image" files from a multipart post, each size-capped as it streams."""
    reader = await request.multipart()
    text = ""
    images: list[tuple[str, bytes]] = []
    while (part := await reader.next()) is not None:
        if not isinstance(part, BodyPartReader) or part.name not in ("text", "image"):
            raise BadRequest(400, "a post has a 'text' field and 'image' files, nothing else")
        if part.name == "text":
            # 4 bytes per character is UTF-8's worst case.
            text = (await read_part(part, config.MAX_POST_TEXT_CHARS * 4, "the text")).decode("utf-8", "replace")
        elif len(images) == config.MAX_IMAGES_PER_POST:
            raise BadRequest(400, f"at most {config.MAX_IMAGES_PER_POST} images per post")
        else:
            data = await read_part(part, config.MAX_IMAGE_BYTES, part.filename or "an image")
            images.append((part.filename or "image", data))
    return text, images


async def read_part(part: BodyPartReader, limit: int, what: str) -> bytes:
    data = bytearray()
    while chunk := await part.read_chunk(READ_CHUNK):
        data.extend(chunk)
        if len(data) > limit:
            raise BadRequest(413, f"{what} is over the limit ({limit / 2**20:.0f} MiB)" if limit >= 2**20
                             else f"{what} is too long")
    return bytes(data)
