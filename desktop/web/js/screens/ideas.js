// Key ideas: points in the Book Companion that none of the highlights cover.

import { useStore, mutate, removeRecord, here, go, replace, openDrawer } from "../store.js";
import { ClashNote } from "./clash-note.js";
import { html, WorkTop, Field, EinkBar, Running } from "../ui.js";
import { useSelected, useOpenShown, StageNote } from "./connections.js";
import { currentOptions } from "./working-options.js";

const RUN_ROW = "Key ideas you didn't highlight";

function IdeaPage({ book, idea, rest }) {
  const remove = async () => {
    const answer = await removeRecord(`/api/ideas/${idea.id}`);
    if (answer) replace(here("ideas", rest.length ? rest[0].id : null));
  };
  return html`
    <div class="rowg" style="justify-content:space-between">
      <span class="crumbt">Key idea · ${idea.chapter} · Not in your highlights</span>
      <button class="lnk" onClick=${remove}>Not for me</button>
    </div>
    <h2 class="mtitle">${idea.title}</h2>
    ${idea.note && html`<div><div class="ideal">The Book Companion says</div><div style="font-size:15.5px;text-wrap:pretty">${idea.note}</div></div>`}
    ${idea.quote && html`<div><div class="ideal">In the book</div><p class="ideaq">${idea.quote}</p></div>`}
    <div class="two">
      <div class="card" style="background:var(--sheet);min-width:0">
        <div class="chd"><span class="ckind">Key idea card</span><span>${idea.src}</span></div>
        <${Field} key=${`${idea.id}-front`} class="ta f" rows="1" value=${idea.front} placeholder="Question" path=${`/api/ideas/${idea.id}`} field="front" />
        <${Field} key=${`${idea.id}-back`} class="ta b" rows="1" value=${idea.back} placeholder="Answer" path=${`/api/ideas/${idea.id}`} field="back" />
        <${ClashNote} clash=${idea.clash} />
      </div>
      <div class="pf">
        <span class="lab">Context on the reader</span>
        <div class="eink">
          <${EinkBar} />
          <div class="einkm"><div class="einkd"><span class="einkbk">${book.title}</span><span>${book.author}</span><br /><span>${idea.chapter}</span></div></div>
        </div>
      </div>
    </div>
  `;
}

function NeedsCompanion({ companion }) {
  return html`
    <div>
      <h2 class="mtitle">This needs a Book Companion</h2>
      <p class="lede" style="margin-top:8px">pbAnki finds what you missed by comparing your highlights with the Companion's list of each chapter's key points. This book doesn't have one yet.</p>
    </div>
    ${companion.none && html`<div class="rowg"><button class="btn pri" onClick=${() => openDrawer("create")}>Create a Book Companion</button></div>`}
    ${companion.busy && html`<${Running}>${companion.busyText}<button class="lnk" onClick=${() => openDrawer("create")}>Details</button><//>`}
  `;
}

// The empty list's sentence. It can't say nothing's left while the run hasn't finished looking.
function emptyText(book) {
  const row = book.run.rows.find((r) => r.title === RUN_ROW);
  if (row && !row.ok && (row.failed || book.run.active)) return "No key ideas yet.";
  return book.ideaEmptyText;
}

// Runs key ideas alone, for a book already gone through or read further since.
function LookForIdeas({ book }) {
  const { state } = useStore();
  const look = book.ideaLook;
  if (!look.show) return null;
  const { meta_model, prompt } = currentOptions(book);
  const start = () => mutate("POST", `/api/books/${book.id}/run`, {
    action: "start",
    options: { highlights: false, connections: false, ideas: true, meta_model, prompt },
  });
  const usable = state.models.some((model) => model.id === meta_model);
  return html`<div class="rowg" style="font-size:14px;color:var(--ink2)">
    <button class="btn" onClick=${start} disabled=${book.run.active || !usable}>${book.run.active ? "Working..." : look.label}</button>
    ${look.note && html`<span>${look.note}</span>`}
  </div>`;
}

export function Ideas() {
  const { book } = useStore();
  const items = book.ideasNeedComp ? [] : book.ideas;
  const { current } = useSelected(items, "ideaPick");
  useOpenShown(current, (id) => `/api/ideas/${id}/open`);
  return html`
    <${WorkTop} tab="ideas" />
    <div class="work">
      <aside class="nav">
        <div class="navhd"><div style="font-size:14px;color:var(--ink2)" class="rowg"><span>Important points in the Book Companion that none of your highlights cover, from chapters you've read. Only the ones you open get saved.</span></div><${LookForIdeas} book=${book} /></div>
        <div class="navlist" style="padding:0">
          ${items.map((item) => html`<button key=${item.id} class=${current && item.id === current.id ? "ml on" : "ml"} onClick=${() => go(here("ideas", item.id))}>
            <span class="mlt">${item.title}</span>
            <span class="mlm">${item.meta}</span>
          </button>`)}
          ${!items.length && html`<div class="empty" style="margin:14px">${emptyText(book)}</div>`}
        </div>
      </aside>
      <main class="main" id="main"><div class="col">
        ${!items.length && !book.ideasNeedComp && html`<${StageNote} book=${book} title=${RUN_ROW} failure="Looking for key ideas didn't finish." retry />`}
        ${current && html`<${IdeaPage} book=${book} idea=${current} rest=${items.filter((item) => item.id !== current.id)} />`}
        ${book.ideasNeedComp && html`<${NeedsCompanion} companion=${book.companion} />`}
      </div></main>
    </div>
  `;
}
