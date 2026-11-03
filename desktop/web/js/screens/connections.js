// Connections: the cards that take several highlights, and Find connections.

import { useCallback, useEffect, useRef, useState } from "../../vendor/preact-htm.js";
import { useStore, store, mutate, setUi, here, go, replace, refreshBook, openDrawer, saveSettings, useEvent, showError } from "../store.js";
import { html, WorkTop, Field, EinkBar, Opt, Select, Stepper, PromptLine, Running, plural, useReloadAgo } from "../ui.js";

// The item a screen shows: the one in the route, else the last one picked there, else the first.
export function useSelected(items, slot) {
  const { route, ui } = useStore();
  const remembered = (ui[slot] || {})[route.bookId];
  const wanted = route.sub || remembered;
  // Items that arrive while the list was empty wait to be clicked, since opening one gets it saved
  const wasEmpty = useRef(!items.length);
  if (!items.length) wasEmpty.current = true;
  const found = items.find((item) => item.id === wanted);
  if (found) wasEmpty.current = false;
  useEffect(() => {
    if (route.sub && route.sub !== remembered) setUi({ [slot]: { ...ui[slot], [route.bookId]: route.sub } });
  }, [route.sub]);
  return { wanted, current: found || (wasEmpty.current ? null : items[0] || null) };
}

// Whatever a screen shows counts as opened. Each item is asked for once.
export function useOpenShown(item, path) {
  const asked = useRef(null);
  useEffect(() => {
    if (item && !item.opened && asked.current !== item.id) {
      asked.current = item.id;
      mutate("POST", path(item.id));
    }
    const main = document.getElementById("main");
    if (main) main.scrollTop = 0;
  }, [item && item.id, item && item.opened]);
}

// What the background run is doing about a step, or why it failed, while there's nothing to show.
export function StageNote({ book, title, failure, retry = false }) {
  const row = book.run.rows.find((r) => r.title === title);
  if (!row || row.ok) return null;
  if (row.failed) {
    return html`<div class="banner"><span>${failure} ${row.path}</span>${retry && html`<button class="lnk" onClick=${() => go(here("suggest"))}>Suggest cards</button>`}</div>`;
  }
  if (book.run.active && !book.run.paused) return html`<${Running}>${row.path === "Waiting" ? "Waiting for highlight cards" : row.path}<//>`;
  return null;
}

const save = (id, field) => (text) => mutate("PATCH", `/api/connections/${id}`, { [field]: text }, { quiet: true });

function ConnectionPage({ book, item, rest }) {
  const remove = async () => {
    const answer = await mutate("DELETE", `/api/connections/${item.id}`);
    if (answer) replace(here("connections", rest.length ? rest[0].id : "find"));
  };
  return html`
    <div class="rowg" style="justify-content:space-between">
      <span class="crumbt">Connection · ${item.chapters} · ${item.srcCount}</span>
      <button class="lnk" onClick=${remove}>Remove</button>
    </div>
    <h2 class="mtitle">${item.title}</h2>
    <div class="two">
      <div class="fl" style="gap:22px;min-width:0">
        <div class="card" style="background:var(--sheet)">
          <div class="chd"><span class="ckind">Connection card</span><span>${item.src}</span></div>
          <${Field} key=${`${item.id}-front`} class="ta f" rows="1" value=${item.front} placeholder="Question" onSave=${save(item.id, "front")} />
          <${Field} key=${`${item.id}-back`} class="ta b" rows="1" value=${item.back} placeholder="Answer" onSave=${save(item.id, "back")} />
        </div>
        <div class="fl" style="gap:0">
          <div class="sech"><span class="sect">Drawn from these highlights</span></div>
          <div class="srcs">
            ${item.sources.map((s) => html`<button key=${s.hid} class="src" onClick=${() => go(here("highlights", s.hid))}>
              <span class="srct"><span class="mkl">${s.mark}</span></span>
              <span class="srcm">${s.where}</span>
            </button>`)}
          </div>
        </div>
      </div>
      <div class="pf">
        <span class="lab">Context on the reader</span>
        <div class="eink" style="overflow-y:auto">
          <${EinkBar} style="flex:none" />
          <div class="einkd" style="flex:none"><span class="einkbk">${book.title}</span><span>${book.author}</span></div>
          <div class="einkrule" style="flex:none"></div>
          <div style="display:flex;flex-direction:column;gap:9px;flex:none">
            ${item.sources.map((s) => html`<div key=${s.hid} style="display:flex;flex-direction:column;gap:2px">
              <span style="font-size:8px;font-weight:700;letter-spacing:.02em">${s.whereLong}</span>
              <span style="font-style:italic">“${s.mark}”</span>
            </div>`)}
          </div>
        </div>
      </div>
    </div>
  `;
}

// This book's Find choices, kept while moving between screens.
function useFindChoices() {
  const { ui, route } = useStore();
  const all = ui.find || {};
  const choices = { off: {}, scope: "pending", hint: "", prompt: null, nothing: false, ...all[route.bookId] };
  const change = (patch) => {
    const latest = store.ui.find || {};
    setUi({ find: { ...latest, [route.bookId]: { ...latest[route.bookId], ...patch } } });
  };
  return [choices, change];
}

// Picks stand for chapters, and a highlight with no chapter has a null id.
const pickKey = (pick) => pick.id ?? "";

function FindPage({ book, choices, change }) {
  const { state } = useStore();
  const [sending, setSending] = useState(false);
  const find = book.find;
  const picked = find.picks.filter((pick) => !choices.off[pickKey(pick)]);
  const inScope = (pick) => (choices.scope === "all" ? pick.all : pick.pending);
  const reading = picked.reduce((sum, pick) => sum + inScope(pick), 0);
  const names = state.prompts.names;
  const prompt = names.includes(choices.prompt) ? choices.prompt : book.options.prompt;
  const model = state.settings.meta_model;
  const count = state.settings.count;
  const reloadAgo = useReloadAgo();
  const busy = find.busy || sending;

  const toggle = (pick) => change({ off: { ...choices.off, [pickKey(pick)]: !choices.off[pickKey(pick)] } });
  const all = () => change({ off: {} });
  const none = () => change({ off: Object.fromEntries(find.picks.map((pick) => [pickKey(pick), true])) });
  const choosePrompt = (name) => {
    change({ prompt: name });
    saveSettings({ last_prompt: name });
  };
  const start = async () => {
    setSending(true);
    change({ nothing: false });
    replace(here("connections", "find"));
    const body = { scope: choices.scope, count, model, prompt };
    if (picked.length < find.picks.length) body.chapters = picked.map((pick) => pick.id);
    if (choices.hint.trim()) body.hint = choices.hint;
    await mutate("POST", `/api/books/${encodeURIComponent(book.id)}/connections/find`, body);
    setSending(false);
  };

  return html`
    <div>
      <h2 class="mtitle">Find connections</h2>
      <p style="font-size:14px;color:var(--ink2);margin:6px 0 0">The AI reads the highlights in the chapters you pick together and looks for questions that need more than one of them.</p>
    </div>
    <div class="fl" style="gap:0">
      <div class="sech">
        <span class="sect">Chapters</span>
        <span class="rowg" style="gap:12px"><button class="lnk" onClick=${all}>All</button><button class="lnk" onClick=${none}>None</button></span>
      </div>
      <div style="display:flex;flex-direction:column;gap:2px;padding-top:6px">
        ${find.picks.map((pick) => html`<label key=${pickKey(pick)} class="cbx" style="padding:6px 0;border-bottom:1px solid var(--hair)">
          <input type="checkbox" checked=${!choices.off[pickKey(pick)]} onChange=${() => toggle(pick)} />
          <span style="flex:1">${pick.name}</span>
          <span style="font:400 12px var(--mono);color:var(--ink3)">${plural(inScope(pick), "highlight")}</span>
        </label>`)}
      </div>
    </div>
    <${Opt} label="Highlights to read">
      <div class="seg" style="align-self:flex-start">
        <button class=${choices.scope === "pending" ? "sg on" : "sg"} onClick=${() => change({ scope: "pending" })}>Pending</button>
        <button class=${choices.scope === "all" ? "sg on" : "sg"} onClick=${() => change({ scope: "all" })}>All, including reviewed</button>
      </div>
    <//>
    <section class="comp">
      <div class="compt">${picked.length ? `Reading ${plural(reading, "highlight")} from ${plural(picked.length, "chapter")}` : "Pick at least one chapter"}</div>
      ${book.companion.have && html`<div style="font-size:13.5px;color:var(--ink2)">Also reads the Book Companion's chapter-by-chapter notes, so it knows how the book's argument fits together.</div>`}
      <textarea class="hint" rows="1" value=${choices.hint} onInput=${(e) => change({ hint: e.target.value })} placeholder="Steer it if you like: look for where the book contradicts itself"></textarea>
      <div class="opts">
        <${Opt} label="How many"><${Stepper} value=${count} onChange=${(value) => saveSettings({ count: value })} /><//>
        <${Opt} label="Model"><${Select} value=${model} options=${state.models} onChange=${(value) => saveSettings({ meta_model: value })} /><//>
        <${Opt} label="Prompt"><${Select} value=${prompt} options=${names} onChange=${choosePrompt} /><//>
        <button class="btn pri go" onClick=${start} disabled=${busy || reading < 2}>${busy ? "Reading highlights..." : `Find ${plural(count, "connection")}`}</button>
      </div>
      ${find.busy && html`<${Running}>Running ${find.busyCmd}<//>`}
      ${!busy && find.error && html`<div class="running">${find.error}</div>`}
      ${!busy && !find.error && choices.nothing && html`<div class="running">The AI didn't find any new connections.</div>`}
      <${PromptLine}
        path=${`${state.prompts.dir}/${prompt}/connections.md`}
        reloadAgo=${reloadAgo}
        onOpen=${() => openDrawer("prompt", { name: prompt, file: "connections.md" })}
      />
    </section>
  `;
}

export function Connections() {
  const { book, route } = useStore();
  const items = book.connections;
  const { wanted, current } = useSelected(items, "connectionPick");
  const shown = wanted === "find" ? null : current;
  const isFind = !shown;
  useOpenShown(shown, (id) => `/api/connections/${id}/open`);
  const [choices, change] = useFindChoices();

  // A finished Find shows its first new connection, if Find is still on screen.
  const onEvent = useCallback(async (event) => {
    if (event.type !== "find" || event.book_id !== route.bookId) return;
    if (event.state !== "done") return;
    try {
      await refreshBook();
    } catch (error) {
      showError(error);
    }
    const stillHere = store.route.name === "connections" && store.route.sub === "find";
    if (event.found) {
      change({ hint: "", nothing: false });
      if (stillHere) replace(here("connections", event.connections[0]));
    } else {
      change({ nothing: true });
    }
  }, [route.bookId]);
  useEvent(onEvent);

  const pickedCount = book.find.picks.filter((pick) => !choices.off[pickKey(pick)]).length;
  return html`
    <${WorkTop} tab="connections" />
    <div class="work">
      <aside class="nav">
        <div class="navhd">
          <div style="font-size:14px;color:var(--ink2)">Questions that take several highlights to answer. Each one becomes one card.</div>
        </div>
        <div class="navlist" style="padding:0">
          <button class=${isFind ? "ml on" : "ml"} onClick=${() => go(here("connections", "find"))}>
            <span class="mlt">+ Find connections</span>
            <span class="mlm">${pickedCount === book.find.picks.length ? "All chapters" : plural(pickedCount, "chapter")}</span>
          </button>
          ${items.map((item) => html`<button key=${item.id} class=${shown && item.id === shown.id ? "ml on" : "ml"} onClick=${() => go(here("connections", item.id))}>
            <span class="mlt">${item.title}</span>
            <span class="mlm">${item.meta}</span>
          </button>`)}
          ${!items.length && html`<div class="empty" style="margin:14px">No connection cards yet. Use Find connections above.</div>`}
        </div>
      </aside>
      <main class="main" id="main"><div class="col">
        ${!items.length && html`<${StageNote} book=${book} title="Connections" failure="Looking for connections didn't finish." />`}
        ${shown
          ? html`<${ConnectionPage} book=${book} item=${shown} rest=${items.filter((item) => item.id !== shown.id)} />`
          : html`<${FindPage} book=${book} choices=${choices} change=${change} />`}
      </div></main>
    </div>
  `;
}
