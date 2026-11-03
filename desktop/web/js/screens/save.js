// Save cards and Saved.

import { useCallback, useEffect, useRef, useState } from "../../vendor/preact-htm.js";
import { api } from "../api.js";
import { useStore, store, go, here, mutate, setUi, useEvent } from "../store.js";
import { html, Top, Pulse, Waiting } from "../ui.js";

const previewPath = (id) => `/api/books/${encodeURIComponent(id)}/save`;

// GET /api/books/<id>/save, again whenever `deps` change. Only the newest answer counts.
function usePreview(id, mark, deps) {
  const [found, setFound] = useState({ preview: null, error: null });
  const newest = useRef(0);
  const load = useCallback(async () => {
    const asked = ++newest.current;
    const query = mark === undefined || mark === null ? "" : `?mark_connection_sources=${mark}`;
    try {
      const preview = await api.get(previewPath(id) + query);
      if (asked === newest.current) setFound({ preview, error: null });
    } catch (error) {
      if (asked === newest.current) setFound((now) => ({ ...now, error: error.message }));
    }
  }, [id, mark]);
  useEffect(() => {
    load();
  }, [load, ...deps]);
  return { ...found, load };
}

// When it can't load and there's nothing to show.
const NoAnswer = ({ error, onRetry }) => html`<div class="center"><div class="narrow">
  <h1 class="h1">This didn't load</h1>
  <p class="lede">${error}</p>
  <div class="rowg"><button class="btn pri" onClick=${onRetry}>Try again</button></div>
</div></div>`;

// A failed save's sentence, which always says the cards are kept.
const SAFE = "Your cards are safe on this computer.";
const safeError = (error) => (error.includes(SAFE) ? error : `${error.replace(/\.$/, "")}. ${SAFE}`);

const Group = ({ group }) => html`<div>
  <div class="sgh"><span>${group.head}</span><span class="sgm">${group.where}</span></div>
  ${group.rows.map((row, i) => html`<div key=${i} class="sr">
    <span class="srq">${row.front}</span>
    <span>${row.need && html`<span class="srneed">No answer yet</span>`}<span>${row.back}</span></span>
  </div>`)}
</div>`;

const Step = ({ step }) => html`<div class="step2">
  <span class="stepn">${step.n}</span>${step.n === 4 ? html`<span class="rowg" style="gap:6px">${step.title}</span>` : html`<span>${step.title}</span>`}<span class="stepd">${step.detail}</span>
</div>`;

export function SaveCards() {
  const { book, state, ui } = useStore();
  const id = book.id;
  const [busy, setBusy] = useState(null); // "save" or "retry"
  const { preview, error, load } = usePreview(id, ui.saveMark, [book, state.reader.connected, state.reader.mount]);

  const finished = useCallback(() => {
    setUi({ ejected: false });
    if (store.route.name === "save" && store.route.bookId === id) go(here("saved"));
  }, [id]);

  // The reader came back and a waiting save finished by itself
  useEvent(
    useCallback(
      (event) => {
        if (event.topic === `book:${id}` && event.type === "save" && event.status === "verified") finished();
      },
      [id, finished],
    ),
  );

  const send = async (kind, path, body) => {
    setBusy(kind);
    const answer = await mutate("POST", path, body);
    setBusy(null);
    if (answer && answer.status === "verified") finished();
    else load();
  };
  const save = () => send("save", previewPath(id), { mark_connection_sources: ui.saveMark ?? preview.markMeta });
  const retry = () => send("retry", `${previewPath(id)}/retry`);

  const middle = html`<span class="bookt">Save cards</span>`;
  const top = html`<${Top} middle=${middle} settings=${false}><button class="btn" onClick=${() => go(here("highlights"))}>Back to editing</button><//>`;
  if (!preview) {
    return html`${top}${error ? html`<${NoAnswer} error=${error} onRetry=${load} />` : html`<${Waiting}>Getting your cards ready<//>`}`;
  }

  const failed = preview.failed.length > 0;
  const mark = ui.saveMark ?? preview.markMeta;
  return html`
    ${top}
    <div class="savew">
      <div class="savel"><div class="saveg">
        ${preview.groups.map((group) => html`<${Group} key=${group.type + group.id} group=${group} />`)}
        ${!preview.groups.length && html`<div class="empty">There are no cards to save yet. Only the cards you open get saved.</div>`}
      </div></div>
      <aside class="saver">
        <div class="rowg" style="align-items:baseline"><span class="bignum">${preview.saveCount}</span><span>${preview.saveNoun}</span></div>
        ${preview.needCount > 0 &&
        html`<div class="errb"><span>${preview.needText}</span><button class="lnk" style="color:var(--ink)" onClick=${() => go(here("highlights", preview.needs[0]))}>Go to it</button></div>`}
        ${failed && html`<div class="errb"><span>${safeError(preview.failed[0].error)}</span></div>`}
        <div class="fl" style="gap:12px">
          <div class="lab">What happens when you save</div>
          ${preview.steps.map((step) => html`<${Step} key=${step.n} step=${step} />`)}
        </div>
        <label class="cbx"><input type="checkbox" checked=${mark} onChange=${(e) => setUi({ saveMark: e.target.checked })} /><span>Also mark highlights used only by connection cards</span></label>
        <div style="font-size:13.5px;color:var(--ink2)">${preview.leftText}</div>
        ${preview.saveWaiting &&
        html`<div class="waitb"><div class="rowg"><${Pulse} /><b style="font-weight:600">Plug in your PocketBook to finish</b></div><div style="font-size:13.5px;overflow-wrap:anywhere">${preview.waitText}</div></div>`}
        ${!preview.saveWaiting && failed &&
        html`<button class="btn pri" style="padding:11px" onClick=${retry} disabled=${!!busy || preview.saving}>${busy === "retry" ? "Trying again..." : "Try again"}</button>`}
        ${!preview.saveWaiting && !failed &&
        html`<button class="btn pri" style="padding:11px" onClick=${save} disabled=${preview.saveBlocked || !!busy}>${busy ? "Saving..." : preview.saveBtn}</button>`}
        ${preview.saveUnplugNote && !failed &&
        html`<div style="font-size:13.5px;color:var(--ink2)">Your reader is unplugged. Saving will back up your cards, then ask you to plug it in.</div>`}
      </aside>
    </div>
  `;
}

export function Saved() {
  const { book, state, ui } = useStore();
  const { preview, error, load } = usePreview(book.id, null, []);
  const [ejecting, setEjecting] = useState(false);

  // Plugged back in after an eject, there's something to eject again
  const connected = state.reader.connected;
  const wasConnected = useRef(connected);
  useEffect(() => {
    if (connected && !wasConnected.current && ui.ejected) setUi({ ejected: false });
    wasConnected.current = connected;
  }, [connected]);

  const eject = async () => {
    setEjecting(true);
    const answer = await mutate("POST", "/api/reader/eject");
    setEjecting(false);
    if (answer) setUi({ ejected: true, ejectLabel: answer.ejectLabel });
  };

  if (!preview) {
    return html`<${Top} settings=${false} />${error ? html`<${NoAnswer} error=${error} onRetry=${load} />` : html`<${Waiting}>Checking what was saved<//>`}`;
  }

  const done = preview.last_saved && preview.last_saved.done;
  const buttons = html`
    <button class="btn" onClick=${() => go("#/books")}>Another book</button>
    <button class="btn" onClick=${() => go(here("highlights"))}>Keep working on this one</button>
  `;
  if (!done) {
    return html`
      <${Top} settings=${false} />
      <div class="center"><div class="narrow">
        <h1 class="h1">Nothing saved yet</h1>
        <p class="lede">Cards from this book will show here once they're on your reader.</p>
        <div class="rowg">${buttons}</div>
      </div></div>
    `;
  }

  return html`
    <${Top} settings=${false} />
    <div class="center"><div class="narrow">
      <h1 class="h1">${done.doneTitle}</h1>
      <p class="lede">${done.lede}</p>
      <div style="border-top:3px solid var(--ink)">
        ${done.doneRows.map((row) => html`<div key=${row.t} class="chk"><span class="chkg">✓</span><span class="chkt">${row.t}</span><span class="chkp">${row.p}</span></div>`)}
      </div>
      <div class="rowg">
        <button class="btn pri" onClick=${eject} disabled=${!!ui.ejected || ejecting}>${ui.ejected ? ui.ejectLabel : ejecting ? "Ejecting..." : done.ejectLabel}</button>
        ${buttons}
      </div>
    </div></div>
  `;
}
