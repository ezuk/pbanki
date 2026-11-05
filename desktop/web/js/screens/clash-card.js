// A card already in the reader's collection, with what can be done to it from here.

import { useState } from "../../vendor/preact-htm.js";
import { api } from "../api.js";
import { html, plural } from "../ui.js";

const cardPath = (card, action) => `/api/clashes/cards/${card.nid}/${action}`;

function Editor({ card, saveHint, onDone, onClose }) {
  const [front, setFront] = useState(card.frontText);
  const [back, setBack] = useState(card.backText);
  const [learn, setLearn] = useState(card.learnNote);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const typeBack = (value) => {
    setBack(value);
    if (!touched) setLearn(value !== card.backText);
  };
  const save = async () => {
    setSaving(true);
    setError("");
    try {
      onDone(await api.post(cardPath(card, "edit"), { front, back, learn }));
      onClose();
    } catch (failure) {
      setError(failure.message);
      setSaving(false);
    }
  };

  return html`<div class="ed">
    ${card.frontLocked
      ? html`<div class="edl">Question<div class="clashq">${card.front}</div><div class="lock">This side has formatting. Edit it in Anki so it isn't lost.</div></div>`
      : html`<label class="edl">Question<textarea class="edta q" rows="3" value=${front} onInput=${(e) => { setFront(e.target.value); setError(""); }}></textarea></label>
        ${card.isCloze && html`<div class="edh">${"Keep the {{c1::...}} marks and their numbers. They set what is hidden."}</div>`}`}
    ${card.backLocked
      ? html`<div class="edl">${card.backLabel}<div class="clasha">${card.back}</div><div class="lock">This side has formatting. Edit it in Anki so it isn't lost.</div></div>`
      : html`<label class="edl">${card.backLabel}<textarea class="edta a" rows="3" value=${back} onInput=${(e) => typeBack(e.target.value)}></textarea></label>`}
    ${error && html`<div class="errb"><span>${error}</span></div>`}
    <label class="cbx"><input type="checkbox" checked=${learn} onChange=${(e) => { setLearn(e.target.checked); setTouched(true); }} /><span>Learn this card again</span></label>
    <div class="rowg">
      <button class="btn pri" disabled=${saving} onClick=${save}>Save change</button>
      <button class="btn" onClick=${onClose}>Cancel</button>
    </div>
    <div class="edh">${saveHint}</div>
  </div>`;
}

// `onDone(answer)` takes the fresh view from each change; `onError` a failure.
export function ClashCard({ card, letter, saveHint, onDone, onError }) {
  const [editing, setEditing] = useState(false);
  const send = async (path) => {
    try {
      onDone(await api.post(path));
    } catch (failure) {
      onError(failure);
    }
  };
  const undo = () => send(card.change && card.undoLabel === "Undo" ? `/api/clashes/changes/${card.change}/undo` : cardPath(card, "unsuspend"));
  const away = card.undoLabel === "Unsuspend" && card.canUndo;
  return html`<div class="clashc">
    <div class="chead">
      ${letter && html`<span class="ltr">${letter}</span>`}
      <span class="clashd">${card.deck}</span>
      ${card.tagText && html`<span class=${card.tagCls}>${card.tagText}</span>`}
    </div>
    ${card.stats && html`<div class="cstat">${card.stats}</div>`}
    ${editing
      ? html`<${Editor} card=${card} saveHint=${saveHint} onDone=${onDone} onClose=${() => setEditing(false)} />`
      : html`
        <div class=${away ? "clashq gone" : "clashq"}>${card.front}</div>
        ${card.back && html`<div class=${away ? "clasha gone" : "clasha"}>${card.back}</div>`}
        ${card.was && html`<div class="was"><span class="wasl">Reader still has: </span>${card.was}</div>`}
        ${card.learnNote && html`<div class="was">Comes back as a new card, and keeps its review log.</div>`}
        <div class="cact">
          ${card.canEdit && html`<button class="lnk" onClick=${() => setEditing(true)}>Edit</button>`}
          ${card.canSuspend && !card.suspended && !card.change && html`<button class="lnk" onClick=${() => send(cardPath(card, "suspend"))}>Suspend</button>`}
          ${card.canUndo && html`<button class="lnk" onClick=${undo}>${card.undoLabel}</button>`}
          ${card.lockNote && html`<span class="locknote">${card.lockNote}</span>`}
        </div>
      `}
  </div>`;
}

// Connected pairs, in the order their first pair appears.
export function connected(pairs) {
  const parent = new Map();
  const root = (nid) => {
    if (!parent.has(nid)) parent.set(nid, nid);
    while (parent.get(nid) !== nid) nid = parent.get(nid);
    return nid;
  };
  pairs.forEach((pair) => parent.set(root(pair.a), root(pair.b)));
  const groups = new Map();
  pairs.forEach((pair) => {
    const key = root(pair.a);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(pair);
  });
  return [...groups.values()].map((rows) => ({
    rows,
    nids: [...new Set(rows.flatMap((pair) => [pair.a, pair.b]))],
  }));
}

export const groupNote = (group) => `${plural(group.nids.length, "connected card")}, ${plural(group.rows.length, "pair")}. Editing one card can fix several.`;
