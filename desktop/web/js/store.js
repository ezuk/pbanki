// The single store: state, books, open book, route and local UI fields, and how they stay fresh.

import { useEffect, useState } from "../vendor/preact-htm.js";
import { api, listen } from "./api.js";

export const store = {
  state: null, // GET /api/state
  books: null, // GET /api/books, the rows
  book: null, // GET /api/books/<id>, the open book
  bookError: null, // Why the open book couldn't load
  route: parseRoute(location.hash),
  ui: {}, // Local UI fields, see setUi
  error: null, // Sentence in the error bar
  lost: false, // The event stream is down
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

export function parseRoute(hash) {
  const [path, search] = hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
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
  history.replaceState(null, "", hash);
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
export function openDrawer(name, params = {}) {
  location.hash = withQuery({ drawer: name, ...params });
}

export function closeDrawer() {
  location.hash = withQuery({});
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

// Writes under way, and a count that moves with each one. A book fetched across a write is stale.
let writing = 0;
let writeCount = 0;

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

const fetchBook = coalesced(async (id) => {
  for (;;) {
    const seen = writeCount;
    const book = await api.get(`/api/books/${encodeURIComponent(id)}`);
    if (seen === writeCount && !writing) {
      if (store.route.bookId === id) update({ book, bookError: null });
      return book;
    }
    await sleep(50);
  }
});

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
    if (book.not_loaded) update({ book: await api.post(`/api/books/${encodeURIComponent(id)}/open`) });
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

// Sends a change and refetches the open book. Answers with the response, or null after showing
// the error. With `quiet`, it throws instead of showing it.
export async function mutate(method, path, body, { quiet = false } = {}) {
  writing += 1;
  writeCount += 1;
  let answer;
  try {
    const verb = { POST: api.post, PATCH: api.patch, DELETE: api.del }[method];
    // A PATCH may be sent as the page goes away
    answer = await verb(path, body, { keepalive: method === "PATCH" });
  } catch (error) {
    if (quiet) throw error;
    showError(error);
    return null;
  } finally {
    writing -= 1;
    writeCount += 1;
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

// Debounced saves

const savers = new Set();

// Holds the latest value for `save`, which returns a promise. `set` saves after `ms` of quiet;
// `flush` saves now and resolves when every save so far is done. A failed save shows its error
// and the value waits for the next flush.
export function debouncedSave(save, ms = 300) {
  let timer = null;
  let waiting = false;
  let value;
  let chain = Promise.resolve();

  const saver = {
    waiting: () => waiting,
    set(next) {
      value = next;
      waiting = true;
      savers.add(saver);
      clearTimeout(timer);
      timer = setTimeout(saver.flush, ms);
    },
    flush() {
      clearTimeout(timer);
      if (waiting) {
        const sending = value;
        waiting = false;
        savers.delete(saver);
        chain = chain.then(async () => {
          try {
            await save(sending);
          } catch (error) {
            showError(error);
            if (!waiting) {
              value = sending;
              waiting = true;
              savers.add(saver);
            }
          }
        });
      }
      return chain;
    },
  };
  return saver;
}

// Sends what's waiting, as the page goes away
window.addEventListener("pagehide", () => savers.forEach((saver) => saver.flush()));

// Events

const eventListeners = new Set();

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
    }
  } else if (topic === "settings" || topic === "prompts") {
    quietly(loadState());
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
  listen({
    onEvent(event) {
      refetchForEvent(event);
      eventListeners.forEach((listener) => listener(event));
    },
    onOpen(afterLoss) {
      update({ lost: false });
      if (afterLoss) refetchAll();
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
