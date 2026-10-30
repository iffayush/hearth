// Hearth's page. It talks only to the node on this machine, sending the token
// from the URL (printed by `python -m hearth run`) in an X-Hearth-Token header
// on every API call. Post text is only ever set with textContent, never as HTML.
// Images can't send a header from <img src>, so each one is fetched with the
// token, turned into a blob: URL, and that URL is revoked once it's off screen.
//
// Joke: a whole social network in one file, no framework, no build step. The
// JavaScript ecosystem would like a word.

"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const POLL_MS = 5000;
const PAGE_SIZE = 50;
const MAX_IMAGES = 4;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_TEXT_CHARS = 10000;

const state = {
  me: null,
  firstPage: [],     // the newest page, refreshed by every poll
  olderPosts: [],    // pages fetched with "Older posts"; they never change
  olderCursor: null, // where the next "Older posts" page starts
  shownKey: "",      // what's on screen, so an unchanged poll doesn't redraw
  names: {},
  pickedFiles: [],
  previewUrls: [],
};
// "author/seq/index" -> a promise of a blob: URL, so each image is fetched once while shown.
const imageUrls = new Map();

// --- Talking to the node -------------------------------------------------------------

async function api(path, options = {}) {
  const headers = Object.assign({ "X-Hearth-Token": TOKEN }, options.headers || {});
  const response = await fetch(path, Object.assign({}, options, { headers }));
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      message = (await response.json()).error || message;
    } catch (ignored) { /* not JSON: keep the status line */ }
    throw new Error(message);
  }
  return response;
}

async function apiJson(path, options) {
  return (await api(path, options)).json();
}

// --- Small helpers -----------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function $(id) {
  return document.getElementById(id);
}

function showNotice(message) {
  const notice = $("notice");
  notice.textContent = message;
  notice.hidden = !message;
}

function relativeTime(ms) {
  const seconds = Math.round((Date.now() - ms) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  if (seconds < 7 * 86400) return `${Math.round(seconds / 86400)} d ago`;
  return new Date(ms).toLocaleDateString();
}

function displayName(post) {
  return post.name || `${post.author.slice(0, 8)}…`;
}

// --- The feed ----------------------------------------------------------------------------

function imageKey(post, index) {
  return `${post.author}/${post.seq}/${index}`;
}

function loadImage(img, key) {
  if (!imageUrls.has(key)) {
    const pending = api(`/api/image/${key}`)
      .then((response) => response.blob())
      .then((blob) => URL.createObjectURL(blob));
    // A failed fetch is forgotten, so the next redraw can try again.
    pending.catch(() => imageUrls.delete(key));
    imageUrls.set(key, pending);
  }
  imageUrls.get(key)
    .then((url) => { img.src = url; img.classList.remove("loading"); })
    .catch((error) => { img.alt = `Couldn't load this image: ${error.message}`; img.classList.add("broken"); });
}

function renderPost(post) {
  const article = el("article", "post card");
  const header = el("header", "post-head");
  header.append(el("span", "author", displayName(post)));
  const time = el("time", "when", relativeTime(post.sort_ts));
  time.dateTime = new Date(post.sort_ts).toISOString();
  time.title = new Date(post.sort_ts).toLocaleString();
  header.append(time);
  article.append(header);

  if (post.problem) {
    article.append(el("p", "unreadable", `This post can't be shown: ${post.problem}`));
    return article;
  }
  if (post.text) article.append(el("p", "text", post.text));
  if (post.images > 0) {
    const gallery = el("div", `gallery count-${post.images}`);
    for (let index = 0; index < post.images; index += 1) {
      const img = el("img", "photo loading");
      img.alt = `Image ${index + 1} of ${post.images} from ${displayName(post)}`;
      gallery.append(img);
      loadImage(img, imageKey(post, index));
    }
    article.append(gallery);
  }
  return article;
}

function renderFeed() {
  const posts = state.firstPage.concat(state.olderPosts);
  const key = JSON.stringify(posts);
  if (key === state.shownKey) return;
  state.shownKey = key;
  $("feed").replaceChildren(...posts.map(renderPost));
  $("empty").hidden = posts.length > 0;
  // Revoke the blob: URLs of images no longer on screen, so memory doesn't pile up.
  const onScreen = new Set();
  posts.forEach((post) => { for (let i = 0; i < post.images; i += 1) onScreen.add(imageKey(post, i)); });
  for (const [imgKey, pending] of imageUrls) {
    if (!onScreen.has(imgKey)) {
      pending.then((url) => URL.revokeObjectURL(url)).catch(() => {});
      imageUrls.delete(imgKey);
    }
  }
}

function renderWaiting(waiting) {
  const items = waiting.map((item) => {
    const box = el("article", "post card waiting-post");
    const why = item.home_full
      ? "The home node is full, so this can't upload yet. It'll try again later."
      : `Waiting to upload: ${item.uploaded} of ${item.chunks} pieces sent to the home node.`;
    box.append(el("p", "badge", why));
    if (item.text) box.append(el("p", "text", item.text));
    return box;
  });
  $("waiting").replaceChildren(...items);
}

async function refreshFeed() {
  const data = await apiJson(`/api/feed?limit=${PAGE_SIZE}`);
  state.firstPage = data.posts;
  if (state.olderPosts.length === 0) state.olderCursor = data.next;
  $("load-more").hidden = !state.olderCursor;
  renderWaiting(data.waiting);
  renderFeed();
}

async function loadOlder() {
  if (!state.olderCursor) return;
  const data = await apiJson(`/api/feed?limit=${PAGE_SIZE}&before=${encodeURIComponent(state.olderCursor)}`);
  state.olderPosts = state.olderPosts.concat(data.posts);
  state.olderCursor = data.next;
  $("load-more").hidden = !state.olderCursor;
  renderFeed();
}

// --- Status --------------------------------------------------------------------------------

async function refreshStatus() {
  const status = await apiJson("/api/status");
  const pill = $("status-pill");
  if (status.is_home) {
    pill.textContent = "you're the home node";
    pill.className = "pill pill-home";
  } else if (status.home_connected) {
    pill.textContent = "connected to home";
    pill.className = "pill pill-online";
  } else {
    pill.textContent = "offline";
    pill.className = "pill pill-offline";
  }
  pill.title = status.last_sync_ms ? `Last synced ${relativeTime(status.last_sync_ms)}` : "Not synced yet";
  const names = {};
  (status.authors || []).forEach((author) => { names[author.author] = author.name; });
  const forked = status.forked || [];
  const banner = $("fork-banner");
  banner.hidden = forked.length === 0;
  banner.textContent = forked.length === 0 ? "" : "Warning: " + forked
    .map((author) => names[author] || `${author.slice(0, 8)}…`).join(", ")
    + " has posted from two places with the same keys, so their posts may be out of sync.";
}

// --- Writing ---------------------------------------------------------------------------------

function pickFiles(files) {
  state.previewUrls.forEach((url) => URL.revokeObjectURL(url));
  state.previewUrls = [];
  const picked = Array.from(files);
  const hint = $("compose-hint");
  if (picked.length > MAX_IMAGES) {
    hint.textContent = `Pick up to ${MAX_IMAGES} photos.`;
    state.pickedFiles = [];
  } else if (picked.some((file) => file.size > MAX_IMAGE_BYTES)) {
    hint.textContent = "Each photo can be up to 10 MiB.";
    state.pickedFiles = [];
  } else {
    hint.textContent = picked.length ? `${picked.length} photo${picked.length > 1 ? "s" : ""} attached` : "";
    state.pickedFiles = picked;
  }
  const previews = state.pickedFiles.map((file) => {
    const img = el("img", "preview");
    img.alt = `Preview of ${file.name}`;
    const url = URL.createObjectURL(file);
    state.previewUrls.push(url);
    img.src = url;
    return img;
  });
  $("previews").replaceChildren(...previews);
}

async function submitPost(event) {
  event.preventDefault();
  const text = $("compose-text").value;
  if (!text.trim() && state.pickedFiles.length === 0) return;
  if (text.length > MAX_TEXT_CHARS) {
    showNotice(`Posts are up to ${MAX_TEXT_CHARS} characters.`);
    return;
  }
  const form = new FormData();
  form.append("text", text);
  state.pickedFiles.forEach((file) => form.append("image", file, file.name));
  const button = $("compose-submit");
  button.disabled = true;
  try {
    const result = await apiJson("/api/posts", { method: "POST", body: form });
    $("compose-form").reset();
    pickFiles([]);
    showNotice(result.waiting ? "Your photos are uploading to the home node; the post appears when they're all there." : "");
    await refreshFeed();
  } catch (error) {
    showNotice(`Couldn't post: ${error.message}`);
  } finally {
    button.disabled = false;
  }
}

async function saveName(event) {
  event.preventDefault();
  const name = $("name-input").value.trim();
  try {
    await apiJson("/api/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    showNotice(`Saved. You're "${name}" now.`);
    state.shownKey = "";
    await Promise.all([loadMe(), refreshFeed()]);
  } catch (error) {
    showNotice(`Couldn't save your name: ${error.message}`);
  }
}

async function loadMe() {
  state.me = await apiJson("/api/me");
  $("name-input").value = state.me.name || "";
  $("me-line").textContent = `Your account ID: ${state.me.pubkey}`;
}

// --- Start -------------------------------------------------------------------------------------

async function tick() {
  try {
    await Promise.all([refreshFeed(), refreshStatus()]);
    if ($("notice").textContent.startsWith("Lost touch")) showNotice("");
  } catch (error) {
    showNotice(`Lost touch with your node: ${error.message}`);
  }
}

function start() {
  if (!TOKEN) {
    showNotice("Open this page with the link `python -m hearth run` printed: it ends in ?token=…");
    return;
  }
  $("compose-form").addEventListener("submit", submitPost);
  $("compose-images").addEventListener("change", (event) => pickFiles(event.target.files));
  $("profile-form").addEventListener("submit", saveName);
  $("load-more").addEventListener("click", () => loadOlder().catch((error) => showNotice(error.message)));
  $("settings-toggle").addEventListener("click", () => {
    const panel = $("settings");
    panel.hidden = !panel.hidden;
    $("settings-toggle").setAttribute("aria-expanded", String(!panel.hidden));
  });
  loadMe().catch((error) => showNotice(error.message));
  tick();
  setInterval(tick, POLL_MS);
}

document.addEventListener("DOMContentLoaded", start);
