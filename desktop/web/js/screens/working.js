// Suggest cards and Highlights.

import { useEffect, useRef, useState } from "../../vendor/preact-htm.js";
import { store, useStore, go, replace, here, setUi, mutate, openDrawer } from "../store.js";
import { html, CompanionCopy, Top, BookButton, WorkTop, CheckRow, Pulse, isTyping } from "../ui.js";
import { currentOptions, setOption, CountOpt, ChoiceOpt } from "./working-options.js";
import { Cards, useClearRemoved } from "./working-cards.js";
import { Passage } from "./working-passage.js";
import { AskPanel, askForCards } from "./working-ask.js";

const RUN_OPTIONS = ["count", "kind", "model", "prompt", "sees", "companion", "hint", "highlights", "connections", "meta_model", "ideas"];

function runAction(book, action, options) {
  if (action === "start") setUi({ suggestAgain: null, lastRun: null });
  return mutate("POST", `/api/books/${book.id}/run`, options ? { action, options } : { action });
}

// Starts over from an interrupted run, with the choices it had.
function carryOn(book) {
  const options = Object.fromEntries(RUN_OPTIONS.filter((key) => key in book.run.options).map((key) => [key, book.run.options[key]]));
  return runAction(book, "start", options);
}

// Suggest cards

// A Companion's state, in one line above the choices.
function CompanionBanner({ book, companion }) {
  if (companion.have) {
    const send = currentOptions(book).companion;
    return html`<div class="banner"><span>Book Companion: <${CompanionCopy} book=${book} />. ${send === "Off" ? "These suggestions won't use it, because Companion is set to Off." : `These suggestions will use it as background notes (${send}).`}</span></div>`;
  }
  if (companion.busy) {
    return html`<div class="banner"><span class="rowg"><${Pulse} />${companion.busyText}</span><button class="lnk" onClick=${() => openDrawer("create")}>Details</button></div>`;
  }
  return html`<div class="banner"><span><b style="font-weight:600">No Book Companion for this book.</b> Cards still work without one. One would give the AI a map of the whole book, and find key ideas you didn't highlight.</span><button class="btn" onClick=${() => openDrawer("create")}>Create one</button></div>`;
}

function SuggestForm({ book }) {
  const { state, ui } = useStore();
  const options = currentOptions(book);
  const have = book.companion.have;
  const { due, redo, canConnect, sugBtn, againBtn } = book.suggest;
  // Suggest again replaces suggestions; choices shown for waiting highlights only add to them
  const fresh = ui.suggestAgain === book.id;
  const writes = fresh ? redo : due;
  // With no highlight waiting, the button says which other steps it will run
  const steps = [options.connections && canConnect && "connections", options.ideas && have && "key ideas"].filter(Boolean);
  const label = writes || !steps.length ? (fresh ? againBtn : sugBtn) : `Look for ${steps.join(" and ")}`;
  const again = book.run.phase === "done";
  const process = () => runAction(book, "start", options);
  return html`
    <section class="comp">
      <div class="compt">Highlight cards</div>
      <div class="opts">
        <${CountOpt} book=${book} label="Per highlight" />
        <${ChoiceOpt} book=${book} label="Kind" option="kind" choices=${state.kinds} />
        <${ChoiceOpt} book=${book} label="Model" option="model" choices=${state.models} />
        <${ChoiceOpt} book=${book} label="Prompt" option="prompt" choices=${state.prompts.names} />
        <${ChoiceOpt} book=${book} label="AI reads" option="sees" choices=${state.sees_opts} />
        ${have && html`<${ChoiceOpt} book=${book} label="Companion" option="companion" choices=${state.comp_opts} />`}
      </div>
    </section>
    <section class="comp">
      <label class="cbx"><input type="checkbox" checked=${options.connections} onChange=${(e) => setOption(book, "connections", e.target.checked)} /><span><span class="compt">Connections</span><br />Questions that take several highlights to answer</span></label>
      <div class="opts">
        <${ChoiceOpt} book=${book} label="Model" option="meta_model" choices=${state.models} />
      </div>
    </section>
    ${have && html`<section class="comp">
      <label class="cbx"><input type="checkbox" checked=${options.ideas} onChange=${(e) => setOption(book, "ideas", e.target.checked)} /><span><span class="compt">Key ideas you didn't highlight</span><br />Important points in the Book Companion that your highlights miss. Kept apart from your highlight cards.</span></label>
    </section>`}
    ${again && !fresh
      ? html`<div class="rowg">${unreviewedCount(book) > 0
        ? html`<button class="btn pri" style="padding:11px 16px" onClick=${() => go(here("highlights"))}>${continueLabel(book)}</button><button class="lnk" onClick=${process}>${processLabel(book)}</button>`
        : html`<button class="btn pri" style="padding:11px 16px" onClick=${process}>${processLabel(book)}</button><button class="lnk" onClick=${() => setUi({ suggestAgain: null, lastRun: book.id })}>Back to the last run</button>`}</div>`
      : html`<div class="rowg"><button class="btn pri" style="padding:11px 16px" onClick=${() => runAction(book, "start", fresh ? { ...options, fresh: true } : options)} disabled=${!writes && !steps.length}>${label}</button>${again
        ? html`<button class="lnk" onClick=${() => setUi({ suggestAgain: null, lastRun: book.id })}>Back to the last run</button>`
        : html`<button class="lnk" onClick=${() => go(here("highlights"))}>Skip, write them myself</button>`}</div>`}
  `;
}

// Highlights to do that already have their suggestions.
const unreviewedCount = (book) => book.highlights.filter((h) => inFilter(h, "todo")).length - book.suggest.due;

const continueLabel = (book) => {
  const count = unreviewedCount(book);
  return count > 0 ? `Continue (${count} To Do)` : "Continue";
};
const processLabel = (book) => `Process ${book.suggest.due} new ${book.suggest.due === 1 ? "highlight" : "highlights"} since last run`;

const RunRows = ({ rows }) => html`<div style="border-top:3px solid var(--ink)">${rows.map((row, i) => html`<${CheckRow} key=${i} row=${row} />`)}</div>`;

function SuggestBusy({ book }) {
  return html`
    <${RunRows} rows=${book.run.rows} />
    <div class="rowg"><${Pulse} /><span style="font-size:14px;color:var(--ink2)">You can start reviewing now. Cards appear as they're written.</span></div>
    <div class="rowg"><button class="btn" onClick=${() => go(here("highlights"))}>Review highlights</button><button class="lnk" onClick=${() => runAction(book, "stop")}>Stop and change settings</button></div>
  `;
}

function SuggestDone({ book }) {
  const { due } = book.suggest;
  const process = () => runAction(book, "start", currentOptions(book));
  const processFirst = due > 0 && unreviewedCount(book) === 0;
  return html`
    <${RunRows} rows=${book.run.rows} />
    <div class="rowg">
      ${processFirst
        ? html`<button class="btn pri" onClick=${process}>${processLabel(book)}</button>`
        : html`<button class="btn pri" onClick=${() => go(here("highlights"))}>${continueLabel(book)}</button>`}
      <button class="btn" onClick=${() => go(here("connections"))}>Review connections</button>
      ${book.companion.have && html`<button class="btn" onClick=${() => go(here("ideas"))}>Key ideas</button>`}
      ${due > 0 && !processFirst
        ? html`<button class="lnk" onClick=${process}>${processLabel(book)}</button>`
        : html`<button class="lnk" onClick=${() => setUi({ suggestAgain: book.id, lastRun: null })}>Suggest new cards</button>`}
    </div>
  `;
}

export function Suggest() {
  const { book, ui } = useStore();
  const { run, suggest } = book;
  // A finished run gives way to the choices when asked, or when highlights are waiting
  const wantsChoices = ui.suggestAgain === book.id || (suggest.due > 0 && ui.lastRun !== book.id);
  const phase = run.phase === "done" && wantsChoices ? "idle" : run.phase;
  const lede = phase === "idle" ? (ui.suggestAgain === book.id ? suggest.againLede : suggest.formLede) : suggest.lede;
  // Leaving forgets which of the two was asked for
  useEffect(() => () => {
    if (store.ui.suggestAgain || store.ui.lastRun) setUi({ suggestAgain: null, lastRun: null });
  }, []);
  return html`
    <${Top} middle=${html`<${BookButton} book=${book} />`} />
    <div class="center centertop"><div class="narrow" style="gap:18px">
      <div><h1 class="h1">Suggest cards</h1><p class="lede" style="margin-top:6px">${lede}</p></div>
      <${CompanionBanner} book=${book} companion=${book.companion} />
      ${run.offer && html`<div class="banner"><span>${run.offer}</span><button class="btn" onClick=${() => carryOn(book)}>Carry on</button></div>`}
      ${book.error && html`<div class="errb"><span>${book.error}</span></div>`}
      ${run.error && html`<div class="errb"><span>${run.error}</span></div>`}
      ${phase === "idle" && html`<${SuggestForm} book=${book} />`}
      ${phase === "busy" && html`<${SuggestBusy} book=${book} />`}
      ${phase === "done" && html`<${SuggestDone} book=${book} />`}
    </div></div>
  `;
}

// Highlights

function RunProgress({ book }) {
  const { run } = book;
  if (run.offer) {
    return html`<div class="bg"><div class="bgr"><span>${run.offer}</span></div><div class="rowg"><button class="lnk" onClick=${() => carryOn(book)}>Carry on</button><button class="lnk" onClick=${() => runAction(book, "stop")}>Not now</button></div></div>`;
  }
  if (!run.bg.show) return null;
  const toggle = () => runAction(book, run.paused ? "resume" : "pause");
  return html`<div class="bg">
    <div class="bgr"><span class="rowg" style="gap:6px">${run.bg.label}</span>${run.bg.active && html`<button class="lnk" onClick=${toggle}>${run.bg.btn}</button>`}</div>
    <div class="bgs">${run.bg.cells.map((cell, i) => html`<span key=${i} class=${cell.cls}></span>`)}</div>
  </div>`;
}

const inFilter = (h, filter) => (filter === "todo" ? h.status !== "reviewed" && !h.skipped : filter === "rev" ? h.status === "reviewed" || h.skipped : true);

const FILTER_ORDER = ["todo", "rev", "all"];

// The highlight, its note and the text of its cards, lower case.
function searchText(h) {
  const cards = [...h.cards, ...h.saved].flatMap((card) => [card.frontPlain ?? card.front, card.backPlain ?? card.back]);
  return [h.text, h.mark, h.note, ...cards].filter(Boolean).join("\n").toLowerCase();
}

const searchQuery = (ui, book) => ((ui.hlSearches || {})[book.id] || "").trim().toLowerCase();

const chosenFilter = (ui, book) => (ui.hlFilters || {})[book.id] || "todo";

// Whether a highlight is in the list as filtered and searched.
function inView(h, ui, book) {
  const query = searchQuery(ui, book);
  return inFilter(h, chosenFilter(ui, book)) && (!query || searchText(h).includes(query));
}

// The nearest highlight in the list before (-1) or after (1) `h`, even if `h` itself has left the list.
function neighbour(book, ui, h, by) {
  const index = book.highlights.indexOf(h);
  const side = by < 0 ? book.highlights.slice(0, index).reverse() : book.highlights.slice(index + 1);
  return side.find((item) => inView(item, ui, book)) || null;
}

const unclozed = (text) => text.replace(/\{\{c\d+::(.*?)\}\}/gs, "$1");

// Marks what matches in the text and card fields of `root`. Returns the matches in page order.
function markMatches(root, query) {
  const found = [];
  const ranges = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  while (query && walker.nextNode()) {
    const node = walker.currentNode;
    if (node.nodeType === Node.ELEMENT_NODE) {
      if (node.tagName === "TEXTAREA" && unclozed(node.value).toLowerCase().includes(query)) found.push(node);
    } else if (!node.parentElement.closest("button, textarea")) {
      const text = node.data.toLowerCase();
      for (let at = text.indexOf(query); at !== -1; at = text.indexOf(query, at + query.length)) {
        const range = new Range();
        range.setStart(node, at);
        range.setEnd(node, at + query.length);
        ranges.push(range);
        found.push(range);
      }
    }
  }
  for (const field of root.querySelectorAll("textarea")) field.classList.toggle("srchhit", found.includes(field));
  if (window.Highlight && CSS.highlights) CSS.highlights.set("hl-search", new Highlight(...ranges));
  return found;
}

// Keeps the open highlight's matches marked, and scrolls to the first one if it's out of sight.
function useSearchMarks(query, hid) {
  const paint = () => {
    const main = document.getElementById("main");
    return main ? markMatches(main, query) : [];
  };
  useEffect(() => {
    paint();
  });
  useEffect(() => {
    const first = paint()[0];
    const main = document.getElementById("main");
    if (!first || !main) return;
    const spot = first.getBoundingClientRect();
    const box = main.getBoundingClientRect();
    if (spot.top < box.top || spot.bottom > box.bottom) main.scrollTop += spot.top - box.top - box.height / 3;
  }, [query, hid]);
  useEffect(() => () => {
    if (window.Highlight && CSS.highlights) CSS.highlights.delete("hl-search");
  }, []);
}

const EMPTY_FILTER = { all: "This book has no highlights.", todo: "Nothing left to do.", rev: "Nothing reviewed yet." };

function HighlightNav({ book, selected }) {
  const { ui } = useStore();
  const filter = chosenFilter(ui, book);
  const search = (ui.hlSearches || {})[book.id] || "";
  const query = searchQuery(ui, book);
  const filters = FILTER_ORDER.map((id) => book.filters.find((item) => item.id === id)).filter(Boolean);
  const groups = [];
  for (const h of book.highlights.filter((item) => inView(item, ui, book))) {
    // Only neighbours share a heading, so the list keeps the order of Next, J and "3 of 5"
    let group = groups[groups.length - 1];
    if (!group || group.name !== h.chapter_name) groups.push((group = { name: h.chapter_name, items: [] }));
    group.items.push(h);
  }
  const listRef = useRef(null);
  useEffect(() => {
    // Scrolls the list only, not the page around it
    const list = listRef.current;
    const on = list && list.querySelector(".hl.on");
    if (!on) return;
    const item = on.getBoundingClientRect();
    const box = list.getBoundingClientRect();
    if (item.top < box.top) list.scrollTop -= box.top - item.top;
    else if (item.bottom > box.bottom) list.scrollTop += item.bottom - box.bottom;
  }, [selected]);
  return html`<aside class="nav">
    <div class="navhd">
      <div class="seg">${filters.map((item) => html`<button key=${item.id} class=${filter === item.id ? "sg on" : "sg"} onClick=${() => setUi({ hlFilters: { ...ui.hlFilters, [book.id]: item.id } })}>${item.label}</button>`)}</div>
      <input class="in" type="search" placeholder="Search highlights and cards" aria-label="Search highlights and cards" value=${search} onInput=${(e) => setUi({ hlSearches: { ...ui.hlSearches, [book.id]: e.target.value } })} />
      <${RunProgress} book=${book} />
    </div>
    <div class="navlist" ref=${listRef}>
      ${!groups.length && html`<div class="empty" style="margin:14px">${query ? "No matches." : EMPTY_FILTER[filter]}</div>`}
      ${groups.map((group, i) => html`
        <div key=${`c-${i}-${group.name}`} class="chap">${group.name}</div>
        ${group.items.map((h) => html`<button key=${h.id} class=${`hl${h.id === selected ? " on" : ""}${h.state === "rev" || h.state === "skip" ? " rev" : ""}`} onClick=${() => go(here("highlights", h.id))}>
          <span class="hlg">${h.glyph}</span><span class="hls">${h.mark}</span>
          <span class="hlm">${h.page != null && html`<span>p. ${h.page}</span>`}<span>${h.meta}</span></span>
        </button>`)}
      `)}
    </div>
    <div class="navft">J K next and previous · ⌘↵ suggest · X skip</div>
  </aside>`;
}

// The chapter's Companion notes, folded by default.
function CompanionNotes({ comp }) {
  const [open, setOpen] = useState(false);
  const tagged = comp.tagged;
  return html`<section class="cpn">
    <button class="cpnh" onClick=${() => setOpen(!open)}><span class="cpnk">Book Companion</span><span class="cpnb">${comp.big}</span><span class="cpnt">${open ? "Hide" : "Show notes"}</span></button>
    ${open && html`<div class="cpnl">
      ${comp.pts.map((point, i) => html`<div key=${i} class="cpnr"><span class="cpng">${point.glyph}</span><span>${point.t}</span><span>${point.idea && html`<button class="lnk" onClick=${() => go(here("ideas", point.idea))}>Key idea</button>`}</span></div>`)}
      <div class="cpnf">${tagged ? "✓ means one of your highlights covers it. " : ""}The AI reads these notes as background and won't quiz you on them directly.</div>
    </div>`}
  </section>`;
}

function toggleSkip(book, h) {
  return mutate("POST", `/api/books/${book.id}/highlights/${h.id}/skip`, { skipped: !h.skipped });
}

function HighlightMain({ book, highlight: h }) {
  const { ui } = useStore();
  const previous = neighbour(book, ui, h, -1);
  const next = neighbour(book, ui, h, 1);
  const open = (item) => go(here("highlights", item.id));
  const buttons = () => html`<div class="rowg" style="justify-content:space-between">
    <button class="btn" onClick=${() => open(previous)} disabled=${!previous}>‹ Previous highlight</button>
    <span class="rowg">
      ${h.canSkip && html`<button class="btn" onClick=${() => toggleSkip(book, h)}>Skip</button>`}
      <button class="btn" onClick=${() => open(next)} disabled=${!next}>Next highlight ›</button>
    </span>
  </div>`;
  const page = h.page != null ? ` · p. ${h.page}` : "";
  return html`
    <div class="fl" style="gap:10px">
      <span class="crumbt">${h.chapterFull}${page} · ${h.curPos}</span>
      ${buttons()}
    </div>
    ${h.banner && html`<div class="banner"><span>${h.banner}</span>${h.isSkipped && html`<button class="lnk" onClick=${() => toggleSkip(book, h)}>Undo skip</button>`}</div>`}
    ${h.removed_on_reader && html`<div class="banner"><span>This highlight isn't on the reader any more.</span></div>`}
    ${h.passage_note && html`<div class="banner"><span>${h.passage_note}</span></div>`}
    <${Passage} highlight=${h} book=${book} />
    ${h.later && html`<div class="later"><span class="laterk">Book Companion: later research</span><span>${h.later}</span></div>`}
    ${book.companion.have && h.comp && h.comp.found && html`<${CompanionNotes} comp=${h.comp} />`}
    <${Cards} highlight=${h} book=${book} />
    ${h.canEdit && html`<${AskPanel} highlight=${h} book=${book} />`}
    ${buttons()}
  `;
}

// J, K, command-Enter and X, ignored while typing or with a drawer open.
function useShortcuts(run) {
  const latest = useRef(run);
  latest.current = run;
  useEffect(() => {
    const onKey = (event) => {
      if (isTyping(event) || store.route.query.drawer || event.isComposing) return;
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        latest.current("suggest");
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const action = { j: "next", k: "previous", x: "skip" }[event.key.toLowerCase()];
      if (action) {
        event.preventDefault();
        latest.current(action);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

// With no highlight in the address, the one last open here, else the first still to do.
function firstHighlight(book, remembered) {
  return book.highlights.find((h) => h.id === remembered) || book.highlights.find((h) => inFilter(h, "todo")) || book.highlights[0] || null;
}

export function Highlights() {
  const { book, route, ui } = useStore();
  const h = book.highlights.find((item) => item.id === route.sub) || firstHighlight(book, (ui.highlightPick || {})[book.id]);
  const hid = h ? h.id : null;

  useEffect(() => {
    if (hid) setUi({ highlightPick: { ...store.ui.highlightPick, [book.id]: hid } });
  }, [book.id, hid]);

  // Put the choice in the address, keeping an open drawer
  useEffect(() => {
    if (hid && route.sub !== hid) {
      const search = location.hash.split("?")[1];
      replace(here("highlights", hid) + (search ? `?${search}` : ""));
    }
  }, [hid, route.sub]);

  // Opening a highlight is what makes its cards count for saving
  useEffect(() => {
    if (h && !h.opened) mutate("POST", `/api/books/${book.id}/highlights/${h.id}/open`);
    const main = document.getElementById("main");
    if (main) main.scrollTop = 0;
  }, [hid]);

  useSearchMarks(searchQuery(ui, book), hid);

  useClearRemoved(hid);

  useShortcuts((action) => {
    if (!h) return;
    const step = { next: 1, previous: -1 }[action];
    if (step) {
      const next = neighbour(book, store.ui, h, step);
      if (next) go(here("highlights", next.id));
    } else if (action === "skip" && h.status !== "reviewed") {
      toggleSkip(book, h);
    } else if (action === "suggest" && h.canEdit && !h.busy && !h.writing) {
      askForCards(book, h);
    }
  });

  return html`
    <${WorkTop} tab="highlights" />
    <div class="work">
      <${HighlightNav} book=${book} selected=${hid} />
      <main class="main" id="main"><div class="col">
        ${h
          ? html`<${HighlightMain} key=${h.id} book=${book} highlight=${h} />`
          : html`<div class="empty">${book.error || "This book has no highlights, so there's nothing to review here."}</div>`}
      </div></main>
    </div>
  `;
}
