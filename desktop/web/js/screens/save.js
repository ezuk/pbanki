// Save cards and Saved.

import { useCallback, useEffect, useRef, useState } from "../../vendor/preact-htm.js";
import { api } from "../api.js";
import { useStore, store, go, here, mutate, removeRecord, editsSent, setUi, useEvent } from "../store.js";
import { html, Top, Pulse, Waiting, Field, plural } from "../ui.js";
import { ClashNote } from "./clash-note.js";

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

// A card still to be saved, editable. Cards already sent to the reader have no path.
function Row({ row, onRemove, onLater }) {
  if (!row.path) {
    return html`<div class="sr">
      <span class="srq">${row.front}</span>
      <span>${row.need && html`<span class="srneed">${row.needLabel}</span>`}<span>${row.back}</span></span>
    </div>`;
  }
  return html`<div class="sr sre">
    <${Field} class=${row.cloze ? "ta c" : "ta f"} rows="1" value=${row.front_raw} path=${row.path} field="front" placeholder="Question" />
    <${Field} class=${row.need ? "ta b need" : "ta b"} rows="1" value=${row.back_raw} path=${row.path} field="back" placeholder=${row.need ? row.needLabel : row.cloze ? "Extra context, if any" : "Answer"} />
    ${row.path.startsWith("/api/cards/")
      ? html`<button class="x" title="Remove this card" onClick=${() => onRemove(row)}>✕</button>`
      : html`<span class="rowg"><button class="btn" title="Leave it out of this save and keep it for later" onClick=${() => onLater(row)}>Later</button><button class="btn" title="Drop it for good" onClick=${() => onRemove(row)}>Dismiss</button></span>`}
  </div>`;
}

// Where a card came from, as a link back to it.
const SCREEN_OF = { highlight: "highlights", connection: "connections", idea: "ideas" };
const WhereLink = ({ group }) => html`<button class="lnk sgm" style="font-size:11.5px;font-weight:400" onClick=${() => go(here(SCREEN_OF[group.type], group.id))}>${group.where || "Open"}</button>`;

const Group = ({ group, onRemove, onLater, onDecide }) => html`<div>
  <div class="sgh"><span>${group.head}</span><${WhereLink} group=${group} /></div>
  ${group.emptied && html`<div class="sr sremp">
    <span>You removed every card for this highlight.</span>
    <span class="rowg"><button class="btn" onClick=${() => onDecide(group, "redo")}>Redo later</button><button class="btn" onClick=${() => onDecide(group, "dismiss")}>Dismiss highlight</button></span>
  </div>`}
  ${group.rows.map((row, i) => html`<${Row} key=${row.path || i} row=${row} onRemove=${onRemove} onLater=${onLater} />`)}
</div>`;

// A card that clashed, with what it clashes with until an edit clears it.
const ClashRow = ({ row, onRemove, onLater, onNotAClash }) => html`<div>
  <div class="sgh" style="border-bottom:0;padding-bottom:0"><span>${row.group.head}</span><${WhereLink} group=${row.group} /></div>
  <${Row} row=${row} onRemove=${onRemove} onLater=${onLater} />
  ${row.clash
    ? html`<${ClashNote} clash=${row.clash} onNotAClash=${() => onNotAClash(row)} />`
    : html`<div class="setn" style="padding:4px 0">${row.unchecked ? "Checking this card again..." : "✓ No clash now."}</div>`}
</div>`;

// The clash check's place in the save: running, failed, or what it found.
function ClashCheck({ check, onCheck }) {
  if (check.state === "checking" || check.state === "pending") {
    return html`<div class="waitb"><div class="rowg"><${Pulse} /><b style="font-weight:600">Checking for clashes...</b></div><div style="font-size:13.5px">Comparing these cards with each other and the ${check.total} already on your reader. You can save when it's done.</div></div>`;
  }
  if (check.state === "failed") {
    return html`<div class="errb"><span>Clashes weren't checked. ${check.error}</span><button class="lnk" style="color:var(--ink)" onClick=${onCheck}>Check again</button></div>`;
  }
  if (check.found) {
    return html`<div class="errb"><span>${check.found === 1 ? "1 possible clash" : `${check.found} possible clashes`}. Resolve them at the top of the list, or save anyway.</span></div>`;
  }
  if (check.state === "done") return html`<div class="rowg" style="font-size:13.5px"><span>✓ No clashes among these cards or with the ${check.total} on your reader</span></div>`;
  return null;
}

const Step = ({ step }) => html`<div class="step2">
  <span class="stepn">${step.n}</span>${step.n === 4 ? html`<span class="rowg" style="gap:6px">${step.title}</span>` : html`<span>${step.title}</span>`}<span class="stepd">${step.detail}</span>
</div>`;

export function SaveCards() {
  const { book, state, ui } = useStore();
  const id = book.id;
  const [busy, setBusy] = useState(null); // "save" or "retry"
  const marks = ui.saveMarks || {};
  const { preview, error, load } = usePreview(id, marks[id], [book, state.reader.connected, state.reader.mount]);

  const finished = useCallback(() => {
    // Carrying on after a save starts from what's still to do
    setUi({ ejected: false, highlightPick: { ...store.ui.highlightPick, [id]: null } });
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

  // A waiting save moves on even if the finished event was missed
  const waiting = !!(preview && preview.saveWaiting);
  const connected = state.reader.connected;
  useEffect(() => {
    if (!waiting || !connected) return undefined;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [waiting, connected, load]);
  const wasWaiting = useRef(false);
  useEffect(() => {
    if (!preview) return;
    if (wasWaiting.current && !preview.saveWaiting && !preview.failed.length) finished();
    wasWaiting.current = preview.saveWaiting;
  }, [preview, finished]);

  // The clash check runs once edits settle, and the page follows it until it ends
  const check = preview && preview.clashCheck;
  const clashPaths = useRef(new Set());
  const notAClash = (row) => mutate("POST", `${previewPath(id)}/not-a-clash`, { path: row.path });
  const startCheck = useCallback(() => mutate("POST", `${previewPath(id)}/check`), [id]);
  useEffect(() => {
    if (!check || check.state !== "pending") return undefined;
    const timer = setTimeout(startCheck, 1500);
    return () => clearTimeout(timer);
  }, [check && check.state, check && check.key, startCheck]);
  useEffect(() => {
    if (!check || check.state !== "checking") return undefined;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [check && check.state, load]);

  const send = async (kind, path, body) => {
    setBusy(kind);
    await editsSent();
    const answer = await mutate("POST", path, body);
    setBusy(null);
    if (answer && answer.status === "verified") finished();
    else load();
  };
  const save = () => send("save", previewPath(id), { mark_connection_sources: marks[id] ?? preview.markMeta });
  const retry = () => send("retry", `${previewPath(id)}/retry`);
  // What the last action did, and how to take it back
  const [done, setDone] = useState(null);
  const remove = async (row) => {
    if (!(await removeRecord(row.path))) return;
    const text = row.path.startsWith("/api/cards/") ? "Card removed." : row.path.startsWith("/api/ideas/") ? "Key idea dismissed." : "Connection dismissed.";
    setDone({ text, undo: () => mutate("POST", `${row.path}/restore`) });
  };
  const later = async (row) => {
    if (!(await mutate("POST", `${row.path}/later`))) return;
    const where = row.path.startsWith("/api/ideas/") ? "Key ideas" : "Connections";
    setDone({ text: `Left out of this save. It's still under ${where}.`, undo: () => mutate("POST", `${row.path}/open`) });
  };
  const decide = async (group, choice) => {
    const base = `/api/books/${id}/highlights/${group.id}`;
    if (choice === "redo") {
      if (await mutate("POST", `${base}/redo`, { redo: true })) {
        setDone({ text: "This highlight stays to do, and gets new suggestions next time you suggest.", undo: () => mutate("POST", `${base}/redo`, { redo: false }) });
      }
    } else if (await mutate("POST", `${base}/skip`, { skipped: true })) {
      setDone({ text: "Highlight dismissed. It counts as done when you save.", undo: () => mutate("POST", `${base}/skip`, { skipped: false }) });
    }
  };
  const undo = async () => {
    const action = done.undo;
    setDone(null);
    await action();
  };

  const middle = html`<span class="bookt">Save cards</span>`;
  const top = html`<${Top} middle=${middle} settings=${false}><button class="btn" onClick=${() => go(here("highlights"))}>Back to editing</button><//>`;
  if (!preview) {
    return html`${top}${error ? html`<${NoAnswer} error=${error} onRetry=${load} />` : html`<${Waiting}>Getting your cards ready<//>`}`;
  }

  const failed = preview.failed.length > 0;
  const mark = marks[id] ?? preview.markMeta;
  // A clashing card stays at the top once listed, so a fix doesn't move the box being typed in
  const allRows = preview.groups.flatMap((group) => group.rows.map((row) => ({ ...row, group })));
  allRows.forEach((row) => row.clash && row.path && clashPaths.current.add(row.path));
  const clashRows = allRows.filter((row) => clashPaths.current.has(row.path));
  const groups = preview.groups
    .map((group) => ({ ...group, rows: group.rows.filter((row) => !clashPaths.current.has(row.path)) }))
    .filter((group) => group.rows.length || group.emptied);
  const checkBusy = check && (check.state === "checking" || check.state === "pending");
  return html`
    ${top}
    <div class="savew">
      <div class="savel"><div class="saveg">
        ${done && html`<div class="banner"><span>${done.text}</span><button class="lnk" onClick=${undo}>Undo</button></div>`}
        ${clashRows.length > 0 && html`<div>
          <div class="sgh"><span>Clashes to resolve</span><span class="sgm">Edit a question or answer, remove the card, or mark it Not a clash</span></div>
          ${clashRows.map((row) => html`<${ClashRow} key=${row.path} row=${row} onRemove=${remove} onLater=${later} onNotAClash=${notAClash} />`)}
        </div>`}
        ${groups.map((group) => html`<${Group} key=${group.type + group.id} group=${group} onRemove=${remove} onLater=${later} onDecide=${decide} />`)}
        ${!preview.groups.length && html`<div class="empty">There are no cards to save yet. Only the cards you open get saved.</div>`}
      </div></div>
      <aside class="saver">
        <div class="rowg" style="align-items:baseline"><span class="bignum">${preview.saveCount}</span><span>${preview.saveNoun}</span></div>
        ${check && !preview.saveWaiting && preview.saveCount > 0 && html`<${ClashCheck} check=${check} onCheck=${startCheck} />`}
        ${preview.needCount > 0 &&
        html`<div class="errb"><span>${preview.needText}</span><button class="lnk" style="color:var(--ink)" onClick=${() => go(here(preview.needLink.screen, preview.needLink.id))}>Go to it</button></div>`}
        ${failed && html`<div class="errb"><span>${safeError(preview.failed[0].error)}</span></div>`}
        <div class="fl" style="gap:12px">
          <div class="lab">What happens when you save</div>
          ${preview.steps.map((step) => html`<${Step} key=${step.n} step=${step} />`)}
        </div>
        <label class="cbx"><input type="checkbox" checked=${mark} onChange=${(e) => setUi({ saveMarks: { ...store.ui.saveMarks, [id]: e.target.checked } })} /><span>Also mark highlights used only by connection cards</span></label>
        <div style="font-size:13.5px;color:var(--ink2)">${preview.leftText}</div>
        ${preview.heldText && html`<div style="font-size:13.5px;color:var(--ink2)">${preview.heldText}</div>`}
        ${preview.saveWaiting && connected &&
        html`<div class="waitb"><div class="rowg"><${Pulse} /><b style="font-weight:600">Reader found. Saving your cards...</b></div><div style="font-size:13.5px;overflow-wrap:anywhere">Keep it plugged in. Writing and checking the files can take a little while.</div></div>`}
        ${preview.saveWaiting && !connected &&
        html`<div class="waitb"><div class="rowg"><${Pulse} /><b style="font-weight:600">Plug in your PocketBook to finish</b></div><div style="font-size:13.5px;overflow-wrap:anywhere">${preview.waitText}</div></div>`}
        ${!preview.saveWaiting && failed &&
        html`<button class="btn pri" style="padding:11px" onClick=${retry} disabled=${!!busy || preview.saving}>${busy === "retry" ? "Trying again..." : "Try again"}</button>`}
        ${!preview.saveWaiting && !failed &&
        html`<button class="btn pri" style="padding:11px" onClick=${save} disabled=${preview.saveBlocked || !!busy || checkBusy}>${busy ? "Saving..." : check && check.found ? "Save anyway" : preview.saveBtn}</button>`}
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
      ${preview.freshCount > 0 && html`<p class="lede">${plural(preview.freshCount, "more card")} written since ${preview.freshCount === 1 ? "is" : "are"} still here, not saved yet.</p>`}
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
