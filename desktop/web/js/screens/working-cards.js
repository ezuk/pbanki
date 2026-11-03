// The cards of one highlight: written by hand or by the AI, editable, with Rewrite and the preview.

import { useEffect } from "../../vendor/preact-htm.js";
import { store, useStore, setUi, mutate, useEvent } from "../store.js";
import { html, Field, Pulse, EinkBar } from "../ui.js";

const saveFront = (card) => (front) => mutate("PATCH", `/api/cards/${card.id}`, { front }, { quiet: true });
const saveBack = (card) => (back) => mutate("PATCH", `/api/cards/${card.id}`, { back }, { quiet: true });

// Open panels and the Rewrite text, per card. They outlive moving between highlights.
const panels = (id) => (store.ui.cardPanels || {})[id] || {};

function setPanels(id, patch) {
  setUi({ cardPanels: { ...store.ui.cardPanels, [id]: { ...panels(id), ...patch } } });
}

// Puts {{cN::...}} around what's selected in the field. The field's own input handler saves it.
function hideSelected(event, card) {
  event.preventDefault();
  const el = document.getElementById(`f-${card.id}`);
  if (!el || el.selectionStart === el.selectionEnd) return;
  const start = el.selectionStart;
  const end = el.selectionEnd;
  const text = el.value;
  const n = (text.match(/\{\{c\d+::/g) || []).length + 1;
  el.value = `${text.slice(0, start)}{{c${n}::${text.slice(start, end)}}}${text.slice(end)}`;
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function ClozeFields({ card }) {
  return html`
    <${Field} key="front" id=${`f-${card.id}`} class="ta c" rows="1" value=${card.front} onSave=${saveFront(card)} placeholder="Paste or type a sentence, select the words to hide" />
    <div class="clbar"><button class="btn" onMouseDown=${(event) => hideSelected(event, card)}>Hide selected words</button><span style="font-size:13px;color:var(--ink3)">Select words above, then press this.</span></div>
    <div class="clprev">${card.parts.map((part, i) => (part.gap ? html`<span key=${i} class="gap">[...]</span>` : html`<span key=${i}>${part.text}</span>`))}</div>
  `;
}

function PlainFields({ card }) {
  return html`
    <${Field} key="front" id=${`f-${card.id}`} class="ta f" rows="1" value=${card.front} onSave=${saveFront(card)} placeholder="Question" />
    <${Field} key="back" class=${card.backCls} rows="1" value=${card.back} onSave=${saveBack(card)} placeholder=${card.backPh} />
  `;
}

function RewritePanel({ card }) {
  const send = () => {
    const instruction = (panels(card.id).text || "").trim();
    const body = { model: store.state.settings.model };
    if (instruction) body.instruction = instruction;
    mutate("POST", `/api/cards/${card.id}/rewrite`, body);
  };
  return html`<div class="rw">
    <input class="in" value=${panels(card.id).text || ""} onInput=${(e) => setPanels(card.id, { text: e.target.value })} placeholder="Say what to change, or leave empty" />
    <button class="btn" onClick=${send} disabled=${card.rewriting}>${card.rwBtn}</button>
  </div>`;
}

function Preview({ card, highlight, book }) {
  return html`<div class="prevs">
    <div class="pf"><span class="lab">Question</span><div class="eink"><${EinkBar} /><div class="einkm">${card.frontPlain}</div></div></div>
    <div class="pf"><span class="lab">Answer</span><div class="eink"><${EinkBar} /><div class="einkm" style="flex-direction:column"><span>${card.frontPlain}</span><span class="einkrule" style="width:100%"></span><span>${card.backPlain}</span></div></div></div>
    <div class="pf"><span class="lab">Context</span><div class="eink"><${EinkBar} /><div class="einkq">“${highlight.mark}”</div><div class="einkd"><span class="einkbk">${book.title}</span><span>${book.author}</span><br /><span>${highlight.chapterShort}${highlight.page != null ? ` · p. ${highlight.page}` : ""}</span></div></div></div>
  </div>`;
}

function Card({ card, highlight, book }) {
  useStore();
  const open = panels(card.id);
  const remove = async () => {
    if (await mutate("DELETE", `/api/cards/${card.id}`)) setUi({ removed: { hid: card.hid, id: card.id } });
  };
  return html`<div class="card">
    <div class="chd"><span class="ckind">${card.kindLabel}</span><span>${card.src}</span>
      <span class="chdr">
        <button class="lnk" onClick=${() => setPanels(card.id, { rw: !open.rw })}>Rewrite</button>
        <button class="lnk" onClick=${() => setPanels(card.id, { prev: !open.prev })}>On the reader</button>
        <button class="x" onClick=${remove} aria-label="Remove card">✕</button>
      </span>
    </div>
    ${card.isCloze ? html`<${ClozeFields} card=${card} />` : html`<${PlainFields} card=${card} />`}
    ${card.warn && html`<div class="warn"><span>${card.warn}</span></div>`}
    ${open.rw && html`<${RewritePanel} card=${card} />`}
    ${open.prev && html`<${Preview} card=${card} highlight=${highlight} book=${book} />`}
  </div>`;
}

function SavedCard({ card }) {
  return html`<div class="card ro">
    <div class="chd"><span class="ckind">Saved on the reader</span><span></span></div>
    <textarea class="ta f" rows="1" value=${card.front} placeholder="Question" readOnly></textarea>
    <textarea class="ta b" rows="1" value=${card.back} readOnly></textarea>
  </div>`;
}

// The Cards section, with its header, empty states and Undo.
export function Cards({ highlight, book }) {
  const { ui } = useStore();
  const reviewed = highlight.status === "reviewed";
  const working = highlight.writing || highlight.busy;
  const removed = ui.removed && ui.removed.hid === highlight.id ? ui.removed : null;

  // A finished rewrite closes its panel
  useEvent(onRewriteDone);

  const add = async (kind) => {
    const answer = await mutate("POST", `/api/books/${book.id}/highlights/${highlight.id}/cards`, { kind });
    if (!answer) return;
    setUi({ removed: null });
    setTimeout(() => {
      const el = document.getElementById(`f-${answer.card.id}`);
      if (el) el.focus();
    }, 60);
  };
  const undo = async () => {
    if (await mutate("POST", `/api/cards/${removed.id}/restore`)) setUi({ removed: null });
  };

  return html`<section class="fl" style="gap:22px">
    <div class="sech"><span class="sect">${highlight.cardsTitle}</span>
      ${highlight.canEdit && html`<span class="sechr"><button class="btn" onClick=${() => add("qa")}>+ Question</button><button class="btn" onClick=${() => add("cloze")}>+ Cloze</button></span>`}
    </div>
    <div class="cards">
      ${highlight.error && !reviewed && html`<div class="errb"><span>${highlight.error}</span></div>`}
      ${working && !highlight.cards.length && !reviewed && html`<div class="empty"><${Pulse} />Writing suggestions for this highlight.</div>`}
      ${!working && !highlight.cards.length && !reviewed && html`<div class="empty">No cards yet. Ask the AI below, or write your own.</div>`}
      ${reviewed
        ? highlight.saved.map((card, i) => html`<${SavedCard} key=${i} card=${card} />`)
        : highlight.cards.map((card) => html`<${Card} key=${card.id} card=${card} highlight=${highlight} book=${book} />`)}
      ${removed && html`<div class="undo">Card removed. The AI won't suggest it again.<button class="lnk" onClick=${undo}>Undo</button></div>`}
    </div>
  </section>`;
}

function onRewriteDone(event) {
  if (event.type === "rewrite" && event.state === "done") setPanels(event.card_id, { rw: false, text: "" });
}

// Clears the Undo line when the highlight on screen changes.
export function useClearRemoved(hid) {
  useEffect(() => {
    if (store.ui.removed && store.ui.removed.hid !== hid) setUi({ removed: null });
  }, [hid]);
}
