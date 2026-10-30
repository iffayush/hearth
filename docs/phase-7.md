# Phase 7: Web UI

## What was built

Hearth now has a face. `run` serves a page at `http://127.0.0.1:8080/?token=…`: write a post, attach photos, read the feed, set your name. It all happens on your own machine, talking only to your own node.

**The server** (`web.py`). `start_web()` refuses any address but 127.0.0.1. Every request goes through `guard()`:
1. the `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`, which blocks DNS rebinding;
2. every `/api/` call must carry `X-Hearth-Token`, a random per-launch token compared in constant time, so other websites in your browser can't call the API;
3. every response gets the Content-Security-Policy, `nosniff` and `no-referrer` headers.

The handlers validate every parameter (author: 64 lowercase hex, seq below 2^31, image index 0–3, the cursor, the limit) and cap bodies: multipart posts stream part by part, refusing anything over 10 MiB per image or 45 MiB in total.

**The feed** (`feed.py`). `page()` orders posts by the tuple (sort_ts, author, seq), newest first, and returns a cursor `<sort_ts>:<author>:<seq>` for the next page. `Store.posts()` fetches "strictly older than the cursor" with one row-value comparison. Since sort_ts is fixed on arrival, a future-dated post lands at its arrival time and never moves, and ties never skip or repeat. `FeedCache` keeps decrypted posts by entry hash, so the 5-second poll doesn't decrypt everything again.

**The page** (`static/`). `app.js` polls `/api/feed` and `/api/status` every 5 seconds and redraws only when something changed. Post text only ever goes in with `textContent`. Images are fetched with the token, shown via `blob:` URLs, and revoked once they're off screen. You also get:
- your own posts still waiting to upload ("home node is full" when that's the reason);
- a status pill: home node / connected to home / offline;
- a fork warning banner;
- a settings panel for your display name.

There are no inline scripts or styles, so the CSP holds.

**Found in the browser, fixed:** the browser kept a stale stylesheet after an update, so the page's files are now served with `Cache-Control: no-cache` (decrypted images get `no-store`). Also fixed: the `hidden` attribute losing to a CSS rule, and the header overflowing on a 375 px phone.

`log`/`verify` moved to `audit.py`, keeping `actions.py` under 400 lines. **Old data directories:** unchanged; Phase 6 folders work.

## Try it yourself

```
python -m pytest
```
Expect `439 passed, 2 skipped` (Windows) or `441 passed` (Linux).

With the Phase 6 setup (home and carol), start the home node as before. It now also prints `Open Hearth in your browser: http://127.0.0.1:8080/?token=…`. Start carol with a different web port: `python -m hearth run --profile carol --web-port 8081`. Open both links:
- Home's page shows the pill "you're the home node", and carol's shows "connected to home".
- Type in home's page and press **Post**; it appears in carol's page within 5 seconds, no refresh.
- Attach a photo in carol's page: it shows as "Waiting to upload: 0 of 1 pieces…", then appears in both feeds.
- **Settings** sets your display name, and it replaces the 8-character ID within a poll.

## Poke it

1. **Call the API without the token.** `curl http://127.0.0.1:8080/api/feed` returns `401 {"error": "missing or wrong token…"}`. The page itself (`/`) still loads: it needs to, to read the token from its URL. Remove `?token=…` from the address bar and the page tells you to use the printed link.
2. **Pretend to be another site.** `curl -H "Host: evil.example" -H "X-Hearth-Token: <token>" http://127.0.0.1:8080/api/me` returns `403 wrong Host header`, even with the right token. Then `curl -I http://127.0.0.1:8080/` shows the `Content-Security-Policy`, `X-Content-Type-Options` and `Referrer-Policy` headers.
3. **Watch the page from the inside.** Open the browser's developer tools on the page. The Network tab shows `/api/feed` every 5 s, each with the `X-Hearth-Token` header. Photos load as `blob:` URLs; when a post drops out of the loaded pages (say, 50 newer posts push it off the first page), its blob URL is revoked on the next redraw. The Console stays empty: no CSP violations.
