// The single store: state, books, open book, route and local UI fields, and how they stay fresh.

import { useEffect, useState } from "../vendor/preact-htm.js";
import { api, listen, NO_ANSWER } from "./api.js";

export const store = {
  state: null, // GET /api/state
  books: null, // GET /api/books, the rows
  clashes: null, // GET /api/clashes
  book: null, // GET /api/books/<id>, the open book
  bookError: null, // Why the open book couldn't load
  route: parseRoute(location.hash),
  ui: {}, // Local UI fields, see setUi
  error: null, // Sentence in the error bar
  lost: false, // The event stream is down
  unsaved: false, // Typed text the server hasn't accepted yet
  unsavedOnlyHere: false, // ...and the browser couldn't keep a copy of it
  startError: null, // The first load failed
};

// Changes and subscribing

const watchers = new Set();

export function update(patch) {
  Object.assign(store, patch);
  watchers.forEach((watcher) => watcher());
}

export function setUi(patch) {
  update({ ui: { ...store.ui, ...patch } });
}

// Re-renders the component whenever the store changes, and returns the store.
export function useStore() {
  const [, tick] = useState(0);
  useEffect(() => {
    const watcher = () => tick((n) => n + 1);
    watchers.add(watcher);
    return () => watchers.delete(watcher);
  }, []);
  return store;
}

export function showError(error) {
  update({ error: typeof error === "string" ? error : error.message });
}

export function clearError() {
  if (store.error) update({ error: null });
}

// Routes

function decoded(part) {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}

export function parseRoute(hash) {
  const [path, search] = hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decoded);
  const query = Object.fromEntries(new URLSearchParams(search || ""));
  if (parts[0] === "book" && parts[1]) {
    return { name: parts[2] || "suggest", bookId: parts[1], sub: parts[3] || null, query };
  }
  return { name: parts[0] || "home", bookId: null, sub: null, query };
}

// Hash for a book screen: bookPath(id, "highlights", hid)
export function bookPath(id, screen = "suggest", sub = null) {
  return `#/book/${encodeURIComponent(id)}/${screen}` + (sub ? `/${encodeURIComponent(sub)}` : "");
}

// Hash for a screen in the open book.
export function here(screen, sub = null) {
  return bookPath(store.route.bookId, screen, sub);
}

export function go(hash) {
  clearError();
  location.hash = hash;
}

// Moves without leaving a step in the browser's history.
export function replace(hash) {
  history.replaceState(history.state, "", hash);
  routeChanged();
}

function routeChanged() {
  update({ route: parseRoute(location.hash) });
}

window.addEventListener("hashchange", routeChanged);

function withQuery(query) {
  const base = location.hash.split("?")[0] || "#/";
  const search = new URLSearchParams(query).toString();
  return base + (search ? `?${search}` : "");
}

// Drawers live in the hash, so a reload keeps them: openDrawer("prompt", { name: "Standard" })
// Opening one from a closed state leaves a history step, which closing it takes back.
export function openDrawer(name, params = {}) {
  const hash = withQuery({ drawer: name, ...params });
  if (store.route.query.drawer) history.replaceState(history.state, "", hash);
  else history.pushState({ drawerStep: true }, "", hash);
  routeChanged();
}

export function closeDrawer() {
  if (history.state && history.state.drawerStep) history.back();
  else replace(withQuery({}));
}

// Loading

// One call at a time. A call during a run gets one more run after it.
function coalesced(run) {
  let active = null;
  let queued = null;
  const call = (...args) => {
    if (!active) {
      active = run(...args).finally(() => {
        active = null;
      });
      return active;
    }
    if (!queued) {
      queued = active
        .catch(() => {})
        .then(() => {
          queued = null;
          return call(...args);
        });
    }
    return queued;
  };
  return call;
}

export const loadState = coalesced(async () => {
  update({ state: await api.get("/api/state"), startError: null });
});

export const loadBooks = coalesced(async () => {
  const { books } = await api.get("/api/books");
  update({ books });
});

export const loadClashes = coalesced(async () => {
  update({ clashes: await api.get("/api/clashes") });
});

// Writes under way, and a count that moves with each one. A book fetched across a write is stale.
let writing = 0;
let writeCount = 0;

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

// One fetcher per book, since a queued call runs with the arguments of the call that queued it.
const bookFetchers = new Map();

function fetchBook(id) {
  if (!bookFetchers.has(id)) {
    bookFetchers.set(
      id,
      coalesced(async () => {
        for (;;) {
          const seen = writeCount;
          const book = await api.get(`/api/books/${encodeURIComponent(id)}`);
          if (seen === writeCount && !writing) {
            if (store.route.bookId === id) update({ book, bookError: null });
            return book;
          }
          await sleep(50);
        }
      }),
    );
  }
  return bookFetchers.get(id)();
}

// Refetches the open book. Resolves once the store holds a copy read after this call.
export function refreshBook() {
  const id = store.route.bookId;
  return id ? fetchBook(id) : Promise.resolve(null);
}

// Runs when the route's book changes, or on a reload. A book with no highlights stored yet is
// opened instead, which reads it from the reader.
async function loadBook(id) {
  if (store.book && store.book.id !== id) update({ book: null });
  update({ bookError: null });
  try {
    const book = await fetchBook(id);
    if (book.not_loaded) {
      const opened = await api.post(`/api/books/${encodeURIComponent(id)}/open`);
      if (store.route.bookId === id) update({ book: opened });
    }
  } catch (error) {
    if (store.route.bookId === id && (!store.book || store.book.id !== id)) update({ bookError: error.message });
  }
}

// Picks a book from a list: marks it current, reads it fresh and goes to `screen`.
// On failure the error bar says why, and it stays put.
export async function openBook(id, screen = "suggest") {
  setUi({ opening: id });
  try {
    const book = await api.post(`/api/books/${encodeURIComponent(id)}/open`);
    update({ book, bookError: null });
    go(bookPath(id, screen));
  } catch (error) {
    showError(error);
  } finally {
    setUi({ opening: null });
  }
}

// Writing

async function write(method, path, body) {
  writing += 1;
  writeCount += 1;
  try {
    const verb = { POST: api.post, PATCH: api.patch, DELETE: api.del }[method];
    return await verb(path, body);
  } finally {
    writing -= 1;
    writeCount += 1;
  }
}

// Sends a change and refetches the open book. Answers with the response, or null after showing
// the error. With `quiet`, it throws instead of showing it.
export async function mutate(method, path, body, { quiet = false } = {}) {
  let answer;
  try {
    answer = await write(method, path, body);
  } catch (error) {
    if (quiet) throw error;
    showError(error);
    return null;
  }
  clearError();
  try {
    await refreshBook();
  } catch {
    // The event stream brings the next copy
  }
  return answer;
}

// Changes settings. The answer is the new state.
export async function saveSettings(changes) {
  try {
    update({ state: await api.patch("/api/settings", changes), error: null });
  } catch (error) {
    showError(error);
  }
}

// Throws on a refusal, so the field keeps what was typed and tries again on the next blur.
export async function saveMount(mount) {
  update({ state: await api.patch("/api/settings", { mount }), error: null });
}

// Debounced saves

const RETRY_MS = 3000;
// Browsers refuse a keepalive request above 64 KB
const KEEPALIVE_LIMIT = 60000;
const KEPT_EDITS = "pbanki-unsent-edits";

const edits = new Map(); // One saver per record field, kept across screens
const unsent = new Set(); // Savers holding text the server hasn't accepted
let retryTimer = null;

// The browser's copy of unsent edits, so closing the page loses nothing. {key: {path, field, text}}
function readKept() {
  try {
    return JSON.parse(localStorage.getItem(KEPT_EDITS)) || {};
  } catch {
    return {};
  }
}

function writeKept(change) {
  try {
    const kept = readKept();
    change(kept);
    localStorage.setItem(KEPT_EDITS, JSON.stringify(kept));
    return true;
  } catch {
    return false;
  }
}

function noteUnsaved() {
  const failed = [...unsent].filter((saver) => saver.failed());
  const unsaved = failed.length > 0;
  const onlyHere = failed.some((saver) => !saver.kept());
  if (store.unsaved !== unsaved || store.unsavedOnlyHere !== onlyHere) update({ unsaved, unsavedOnlyHere: onlyHere });
  clearTimeout(retryTimer);
  if (unsaved) retryTimer = setTimeout(retryEdits, RETRY_MS);
}

function retryEdits() {
  [...unsent].filter((saver) => saver.failed()).forEach((saver) => saver.flush());
}

// A save that may work later: nothing answered, or the server failed.
const mayWorkLater = (error) => !error.status || error.status >= 500;

// Holds the latest value for `save`, which returns a promise, and sends one at a time. `set` saves
// after `ms` of quiet; `flush` saves now and resolves once what's waiting is sent and `after` has run.
// With a `key`, the value is also kept in the browser, and a failure that may pass is retried
// until it goes through. Otherwise a failed save shows its error and waits for the next flush.
function makeSaver(save, { ms = 300, key = null, path = null, field = null, after = null, quietRefusal = false } = {}) {
  let timer = null;
  let waiting = false;
  let value;
  let running = null;
  let failed = false;
  let kept = true;
  let quiet = quietRefusal;
  let settling = null;
  const listeners = new Set();

  const keep = () => {
    if (key) kept = writeKept((all) => (all[key] = { path, field, text: value }));
  };
  const finish = () => {
    unsent.delete(saver);
    if (key) writeKept((all) => delete all[key]);
  };

  async function pump() {
    while (waiting) {
      const sending = value;
      waiting = false;
      try {
        await save(sending);
        failed = false;
        quiet = false;
        if (!waiting) finish();
      } catch (error) {
        const retry = !key || mayWorkLater(error);
        if (retry) {
          if (!waiting) {
            value = sending;
            waiting = true;
          }
          if (key) failed = true;
          else showError(error);
          return;
        }
        // Refused for good, such as a card that's gone
        failed = false;
        if (!waiting) finish();
        if (!quiet) showError(error);
        quiet = false;
      }
    }
  }

  async function settle() {
    await saver.sent();
    if (failed) return;
    if (after) await after();
    if (!waiting) listeners.forEach((listener) => listener());
  }

  const saver = {
    waiting: () => waiting,
    failed: () => failed,
    kept: () => kept,
    pending: () => (waiting || running ? value : undefined),
    listen(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    set(next) {
      value = next;
      waiting = true;
      unsent.add(saver);
      keep();
      clearTimeout(timer);
      timer = setTimeout(saver.flush, ms);
    },
    // What's under way or waiting has gone to the server, without `after`
    sent() {
      clearTimeout(timer);
      if (waiting && !running) {
        running = pump().finally(() => {
          running = null;
          noteUnsaved();
        });
      }
      return running || Promise.resolve();
    },
    flush() {
      if (waiting || running) {
        const mine = settle().finally(() => {
          if (settling === mine) settling = null;
        });
        settling = mine;
      }
      return settling || Promise.resolve();
    },
    // Forgets the value
    drop() {
      clearTimeout(timer);
      waiting = false;
      failed = false;
      finish();
      noteUnsaved();
    },
    // As the page goes away: sends at once, not waiting for what's under way. The browser's copy stays
    // until the next load has seen it through, in case this request doesn't arrive.
    lastCall() {
      clearTimeout(timer);
      if (!waiting) return;
      if (!key) {
        saver.flush();
      } else if (new Blob([value]).size < KEEPALIVE_LIMIT) {
        api.patch(path, { [field]: value }, { keepalive: true }).catch(() => {});
      }
    },
  };
  return saver;
}

export function debouncedSave(save, ms = 300) {
  return makeSaver(save, { ms });
}

function refreshQuietly() {
  return refreshBook().catch(() => {});
}

function makeEdit(path, field, options) {
  const key = `${path} ${field}`;
  const saver = makeSaver(
    async (text) => {
      await write("PATCH", path, { [field]: text });
      if (store.error === NO_ANSWER) clearError();
    },
    { key, path, field, after: refreshQuietly, ...options },
  );
  edits.set(key, saver);
  return saver;
}

// The saver of one field of a record: editSaver("/api/cards/1", "front")
export function editSaver(path, field) {
  return edits.get(`${path} ${field}`) || makeEdit(path, field);
}

// Resolves once this record's edits have gone to the server, or failed to.
export function editsSent(prefix = "") {
  const matching = [...edits.entries()].filter(([key]) => key.startsWith(prefix)).map(([, saver]) => saver.sent());
  return Promise.all(matching);
}

// Removes a record: its edits go first, so Undo brings them back and none arrive after it's gone.
export async function removeRecord(path) {
  await editsSent(`${path} `);
  const answer = await mutate("DELETE", path);
  if (answer) {
    for (const [key, saver] of edits) {
      if (key.startsWith(`${path} `)) {
        saver.drop();
        edits.delete(key);
      }
    }
  }
  return answer;
}

// Sends edits left over from a page that went away
function resendKept() {
  for (const { path, field, text } of Object.values(readKept())) {
    if (typeof path === "string" && typeof field === "string" && typeof text === "string") {
      const saver = edits.get(`${path} ${field}`) || makeEdit(path, field, { quietRefusal: true });
      saver.set(text);
      saver.flush();
    }
  }
}

window.addEventListener("pagehide", () => unsent.forEach((saver) => saver.lastCall()));

// Events

const eventListeners = new Set();

// Calls `listener` with every server event from now on.
export function onEvent(listener) {
  eventListeners.add(listener);
}

// Calls `listener` with every server event while the component is mounted.
export function useEvent(listener) {
  useEffect(() => {
    eventListeners.add(listener);
    return () => eventListeners.delete(listener);
  }, [listener]);
}

function quietly(promise) {
  promise.catch(() => {});
}

function refetchForEvent(event) {
  const { topic, type } = event;
  if (topic === "reader") {
    quietly(loadState());
    if (type === "changed" || type === "books") {
      quietly(loadBooks());
      quietly(refreshBook());
      if (store.route.name === "clashes") quietly(loadClashes());
    }
  } else if (topic === "settings" || topic === "prompts") {
    quietly(loadState());
  } else if (topic === "clashes") {
    if (store.route.name === "clashes" || store.route.name === "start") quietly(loadClashes());
  } else if (topic.startsWith("book:")) {
    if (store.route.bookId === topic.slice(5)) quietly(refreshBook());
    if (store.route.name === "books") quietly(loadBooks());
    if (type === "save") quietly(loadState());
  }
}

function refetchAll() {
  quietly(loadState());
  quietly(loadBooks());
  quietly(refreshBook());
}

// First load and the event stream. Called once by app.js.
export async function start() {
  resendKept();
  listen({
    onEvent(event) {
      refetchForEvent(event);
      eventListeners.forEach((listener) => listener(event));
    },
    onOpen(afterLoss) {
      update({ lost: false });
      if (afterLoss) {
        if (store.error === NO_ANSWER) clearError();
        retryEdits();
        refetchAll();
      }
    },
    onLost() {
      update({ lost: true });
    },
  });
  try {
    await loadState();
  } catch (error) {
    update({ startError: error.message });
  }
  quietly(loadBooks());
}

// Keeps the open book loaded as the route changes. Called once by app.js.
export function useBookLoader() {
  const id = store.route.bookId;
  useEffect(() => {
    if (id) loadBook(id);
  }, [id]);
}
