// Entry point: routes to the screen and drawer the hash names.

import { render, useErrorBoundary, useEffect } from "../vendor/preact-htm.js";
import { useStore, start, useBookLoader, replace, go, bookPath } from "./store.js";
import { html, ErrorBar, Waiting } from "./ui.js";
import { PlugIn, ReaderCheck } from "./screens/reader.js";
import { PickBook } from "./screens/books.js";
import { Suggest, Highlights } from "./screens/working.js";
import { Connections } from "./screens/connections.js";
import { Ideas } from "./screens/ideas.js";
import { SaveCards, Saved } from "./screens/save.js";
import { PromptDrawer } from "./drawers/prompt.js";
import { SettingsDrawer } from "./drawers/settings.js";
import { CreateDrawer } from "./drawers/create.js";

// Looked up with text from the address, so these have no inherited names
const table = (entries) => Object.assign(Object.create(null), entries);

// Screens with no book
const SCREENS = table({ plug: PlugIn, check: ReaderCheck, books: PickBook });
// Screens of the open book. They can count on store.book being there.
const BOOK_SCREENS = table({ suggest: Suggest, highlights: Highlights, connections: Connections, ideas: Ideas, save: SaveCards, saved: Saved });
const DRAWERS = table({ prompt: PromptDrawer, settings: SettingsDrawer, create: CreateDrawer });

// The plug-in screen moves on by itself, and a home address picks the right start.
function useReaderRedirects(route, connected) {
  useEffect(() => {
    if (connected === null) return;
    const name = route.name;
    // An open drawer stays open
    const query = location.hash.split("?")[1];
    const move = (hash) => replace(query ? `${hash}?${query}` : hash);
    if (connected && (name === "plug" || name === "home")) move("#/check");
    else if (!connected && (name === "check" || name === "home")) move("#/plug");
    else if (route.bookId && !BOOK_SCREENS[name]) move(bookPath(route.bookId));
    else if (name !== "home" && !route.bookId && !SCREENS[name]) move(connected ? "#/check" : "#/plug");
  }, [route.name, route.bookId, connected]);
}

function Problem({ title, children }) {
  return html`<div class="center"><div class="narrow"><h1 class="h1">${title}</h1><p class="lede">${children}</p></div></div>`;
}

function Screen() {
  const { route, state, book, bookError } = useStore();
  const Plain = SCREENS[route.name];
  if (Plain) return html`<${Plain} />`;
  const Booked = BOOK_SCREENS[route.name];
  if (!Booked || !route.bookId) return null;
  if (bookError) {
    return html`
      <${ErrorBar} />
      <div class="center"><div class="narrow">
        <h1 class="h1">This book didn't open</h1>
        <p class="lede">${bookError}</p>
        <div class="rowg"><button class="btn pri" onClick=${() => go(state.reader.connected ? "#/books" : "#/plug")}>Back</button></div>
      </div></div>
    `;
  }
  if (!book || book.id !== route.bookId) return html`<${Waiting}>Opening the book<//>`;
  return html`<${Booked} />`;
}

function App() {
  const { route, state, startError } = useStore();
  const [crash] = useErrorBoundary();
  useBookLoader();
  useReaderRedirects(route, state ? state.reader.connected : null);

  useEffect(() => {
    start();
  }, []);

  if (crash) {
    return html`<div class="app"><${Problem} title="Something went wrong on this page">${crash.message}. Your work is safe. Reload the page to carry on.<//></div>`;
  }
  if (startError) {
    return html`<div class="app"><${Problem} title="pbAnki isn't answering">${startError}<//></div>`;
  }
  if (!state) return html`<div class="app"></div>`;
  const Open = DRAWERS[route.query.drawer];
  return html`
    <div class="app"><${Screen} /></div>
    ${Open && html`<${Open} />`}
  `;
}

render(html`<${App} />`, document.getElementById("root"));
