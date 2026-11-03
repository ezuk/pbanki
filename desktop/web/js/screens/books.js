// Pick a book.

import { useEffect } from "../../vendor/preact-htm.js";
import { useStore, openBook, loadBooks } from "../store.js";
import { html, Top, Running } from "../ui.js";

function BookRow({ book, opening }) {
  return html`<button class=${book.dim ? "bk dim" : "bk"} onClick=${() => openBook(book.id)} disabled=${book.none || !!opening}>
    <span>
      <span class="bkt" style="display:block">${book.title}</span>
      <span class="bka" style="display:block">${book.author}</span>
      ${book.note && html`<span class="bka" style="display:block">${book.note}</span>`}
      ${book.draft && html`<span class="bkd">${book.draft}</span>`}
    </span>
    <span class="bkm hd">${book.last}</span>
    <span class="bkm hd">${book.read}</span>
    <span class="bkm">${book.hl}</span>
    <span class="bkm hd">${book.comp}</span>
    <span class="bkgo hd">${opening === book.id ? "Opening..." : book.action}</span>
  </button>`;
}

export function PickBook() {
  const { books, state, ui } = useStore();

  useEffect(() => {
    loadBooks().catch(() => {});
  }, []);

  let list;
  if (!books) {
    list = html`<${Running}>Reading your KOReader history<//>`;
  } else if (!books.length) {
    list = html`<div class="empty">${state.reader.connected
      ? "No books in your KOReader history yet. Open one on the reader, then come back."
      : "No books on this computer yet. Plug in your PocketBook to see them."}</div>`;
  } else {
    list = html`<div>
      <div class="bkh"><span>Book</span><span>Last read</span><span>Read</span><span>Highlights</span><span>Companion</span><span></span></div>
      ${books.map((book) => html`<${BookRow} key=${book.id} book=${book} opening=${ui.opening} />`)}
    </div>`;
  }

  return html`
    <${Top} />
    <div class="center centertop"><div class="wide">
      <div><h1 class="h1">Pick a book</h1><p class="lede" style="margin-top:6px">From your KOReader history, most recent first.</p></div>
      ${list}
    </div></div>
  `;
}
