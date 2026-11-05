// Find clashes: questions so similar that a review can't tell which answer is wanted, and fixing them.

import { useEffect, useRef, useState } from "../../vendor/preact-htm.js";
import { useStore, go, setUi, loadClashes, showError, clearError, update } from "../store.js";
import { api } from "../api.js";
import { html, Top, Running, Pulse, plural } from "../ui.js";
import { ClashCard, connected, groupNote } from "./clash-card.js";

const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";

async function send(path, body) {
  clearError();
  try {
    update({ clashes: await api.post(path, body) });
  } catch (error) {
    showError(error);
  }
}

const take = (view) => update({ clashes: view });
const pairAction = (pair, action) => send(`/api/clashes/pairs/${pair.id}`, { action });

function PairRow({ pair, label }) {
  const asking = pair.status === "ask" || pair.status === "ai";
  const undoable = pair.status === "dismissed" || pair.status === "fixed";
  const done = pair.closed || pair.status === "still";
  return html`<div class="prow">
    ${label && html`<span class="prl">${label}</span>`}
    <div class="prm">${pair.msg && html`<span class=${done ? "prdone" : "prmsg"}>${pair.msg}</span>`}</div>
    <div class="rowg prb">
      ${asking && html`<button class="btn pri" onClick=${() => pairAction(pair, "fixed")}>Yes, fixed</button>
        <button class="btn" onClick=${() => pairAction(pair, "still")}>Still a clash</button>`}
      ${pair.status === "ask" && html`<button class="btn" onClick=${() => pairAction(pair, "ask_ai")}>Ask the AI</button>`}
      ${(pair.status === "open" || pair.status === "still") && html`<button class="btn" onClick=${() => pairAction(pair, "dismiss")}>Dismiss</button>`}
      ${undoable && html`<button class="lnk" onClick=${() => pairAction(pair, "undo")}>Undo</button>`}
    </div>
  </div>`;
}

function Group({ group, clashes }) {
  const multi = group.nids.length > 2;
  const letter = (nid) => (multi ? LETTERS[group.nids.indexOf(nid)] : "");
  return html`<div class="stack" style="gap:6px">
    ${multi && html`<div class="lab">${groupNote(group)}</div>`}
    <div class="card clashp">
      <div class="clashs">
        ${group.nids.map((nid) => html`<${ClashCard} key=${nid} card=${clashes.cardViews[nid]} letter=${letter(nid)}
          saveHint=${clashes.changes.saveHint} onDone=${take} onError=${showError} />`)}
      </div>
      <div class="prows">
        ${group.rows.map((pair) => html`<${PairRow} key=${pair.id} pair=${pair} label=${multi ? `${letter(pair.a)} and ${letter(pair.b)}` : ""} />`)}
      </div>
    </div>
  </div>`;
}

function Status({ clashes }) {
  const { running, progress, error, cards, connected: plugged, learn } = clashes;
  if (running) {
    const count = progress && progress.total ? ` (${progress.done} of ${progress.total})` : "";
    return html`<div class="rowg">
      <${Running}>${progress ? progress.step : "Starting"}${count}<//>
      <button class="lnk" onClick=${() => send("/api/clashes/stop")}>Stop</button>
    </div>`;
  }
  return html`<div class="fl">
    ${error && html`<div class="errb"><span>${error}</span></div>`}
    <div class="statusr">
      <button class="btn pri" disabled=${!cards && !plugged} onClick=${() => send("/api/clashes/find")}>Find clashes</button>
      <span class="lab">${cards ? plural(cards, "card") : "Plug in your reader to read your cards"}</span>
      ${learn && html`<span class="learn">${learn}</span>`}
    </div>
  </div>`;
}

function Banner({ banner }) {
  if (!banner) return null;
  if (banner.kind === "old") {
    return html`<div class="errb"><b style="font-weight:600">Your reader's pbAnki can't take these changes yet</b><span>They're safe on this computer and nothing was written to the reader. Install the new version of pbAnki on it, open it once, then plug it in again.</span></div>`;
  }
  if (banner.kind === "error") {
    return html`<div class="errb"><span>${banner.text}</span><button class="lnk" style="color:var(--ink)" onClick=${() => send("/api/clashes/changes/send")}>Try again</button></div>`;
  }
  if (banner.kind === "wait") {
    return html`<div class="waitb"><div class="rowg"><${Pulse} /><b style="font-weight:600">${banner.title}</b></div><div style="font-size:13.5px">${banner.text}</div></div>`;
  }
  if (banner.kind === "eject") {
    return html`<div class="waitb"><div class="rowg"><span class="dot"></span><b style="font-weight:600">${banner.title}</b></div><div style="font-size:13.5px">${banner.text}</div><div class="rowg"><button class="btn pri" onClick=${async () => { clearError(); try { await api.post("/api/reader/eject"); await loadClashes(); } catch (error) { showError(error); } }}>Eject reader</button></div></div>`;
  }
  return html`<div class="banner"><span>${banner.text}</span></div>`;
}

const Steps = ({ steps }) => html`<div class="trkw">${steps.map((step) => html`<span key=${step.label} class=${step.cls}><span class=${step.dot}></span>${step.label}</span>`)}</div>`;

function Need({ item }) {
  return html`<div class="card chg">
    <div class="chgh"><span class="tg bad">${item.tag}</span><span>${item.deck}</span></div>
    <div class="prmsg">${item.title}</div>
    ${item.hasCompare && html`<div class="cmp">
      <div class="cmpc"><span class="diffl">You wrote</span><span class="diffn">${item.mine}</span></div>
      <div class="cmpc"><span class="diffl">${item.theirsLabel}</span><span class="diffn">${item.theirs}</span></div>
    </div>`}
    <div class="rowg">
      ${item.showMine && html`<button class="btn pri" onClick=${() => send(`/api/clashes/changes/${item.id}/use-mine`)}>${item.mineLabel}</button>`}
      <button class="btn" onClick=${() => send(`/api/clashes/changes/${item.id}/drop`)}>${item.dropLabel}</button>
    </div>
  </div>`;
}

function Moving({ item }) {
  return html`<div class="card chg">
    <div class="chgh"><span class="chgk">${item.kind}</span><span>${item.deck}</span></div>
    <div class="diff">
      <span class="diffl">Question</span>
      <div class="fl" style="gap:2px">
        ${item.frontChanged && html`<span class="diffo">${item.oldFront}</span>`}
        <span class="diffn">${item.front}</span>
      </div>
      ${item.backChanged && html`<span class="diffl">Answer</span>
        <div class="fl" style="gap:2px"><span class="diffo">${item.oldBack}</span><span class="diffa">${item.back}</span></div>`}
    </div>
    ${item.learn && html`<div class="was">Comes back as a new card, and keeps its review log.</div>`}
    <${Steps} steps=${item.steps} />
    <div class="rowg" style="justify-content:space-between">
      <span class="chgnow">${item.now}</span>
      ${item.canUndo && html`<button class="lnk" onClick=${() => send(`/api/clashes/changes/${item.id}/undo`)}>Undo</button>`}
    </div>
  </div>`;
}

function Suspended({ item }) {
  const [confirming, setConfirming] = useState(false);
  return html`<div class="card chg">
    <div class="chgh"><span class="chgk">Suspended</span><span>${item.deck}</span></div>
    <div class="diffn">${item.front}</div>
    <div class="clasha">${item.back}</div>
    <${Steps} steps=${item.steps} />
    ${confirming
      ? html`<div class="confirm">
          <b style="font-weight:600">Delete this card for good?</b>
          <span>The card, its interval and its ease go from your reader, then from AnkiWeb and every other device at the next sync, even if it was changed there. Its review log stays. This computer keeps a copy of its text.</span>
          <div class="rowg">
            <button class="btn pri" onClick=${() => send(`/api/clashes/cards/${item.nid}/delete`)}>Delete for good</button>
            <button class="btn" onClick=${() => setConfirming(false)}>Keep it suspended</button>
          </div>
        </div>`
      : html`<div class="rowg" style="justify-content:space-between">
          <span class="chgnow">${item.now}</span>
          <span class="rowg">
            <button class="btn" onClick=${() => send(`/api/clashes/cards/${item.nid}/unsuspend`)}>Unsuspend</button>
            <button class="lnk" onClick=${() => setConfirming(true)}>Delete for good</button>
          </span>
        </div>`}
  </div>`;
}

function Changes({ clashes }) {
  const { changes, asks } = clashes;
  const empty = !changes.needs.length && !changes.moving.length && !changes.suspended.length && !changes.done.length && !asks.length;
  return html`
    <${Banner} banner=${changes.banner} />
    ${changes.needs.length > 0 && html`
      <div class="sectl">Needs you <span class="sectn">${changes.needs.length}</span></div>
      ${changes.needs.map((item) => html`<${Need} key=${item.id} item=${item} />`)}`}
    ${asks.length > 0 && html`
      <div class="sectl">Is it fixed? <span class="sectn">Only you can say. The AI's view is one click away, and only runs if you ask.</span></div>
      ${asks.map((pair) => html`<div class="card chg" key=${pair.id}>
        <div class="pairq"><span>${pair.qa}</span><span>${pair.qb}</span></div>
        ${pair.status === "ai" && html`<div class="prdone">${pair.msg}</div>`}
        <div class="rowg">
          <button class="btn pri" onClick=${() => pairAction(pair, "fixed")}>Yes, fixed</button>
          <button class="btn" onClick=${() => pairAction(pair, "still")}>Still a clash</button>
        </div>
      </div>`)}
      <div class="rowg">
        <button class="btn" disabled=${clashes.running} onClick=${() => send("/api/clashes/check-reworded")}>Check ${plural(asks.length, "reworded pair")} with the AI</button>
        <span class="lab">Compares each pair using your AI.</span>
      </div>`}
    ${changes.moving.length > 0 && html`
      <div class="sectl">On their way <span class="sectn">${changes.moving.length}</span></div>
      ${changes.moving.map((item) => html`<${Moving} key=${item.id} item=${item} />`)}`}
    ${changes.suspended.length > 0 && html`
      <div class="sectl">Suspended <span class="sectn">Out of your reviews, with everything kept.</span></div>
      ${changes.suspended.map((item) => html`<${Suspended} key=${item.id} item=${item} />`)}`}
    ${changes.done.length > 0 && html`
      <div class="sectl">Done <span class="sectn">${changes.done.length}</span></div>
      ${changes.done.map((item) => html`<div class="sr" key=${item.id} style="grid-template-columns:minmax(0,1fr) auto"><span>${item.front}</span><span class="tg done" style="align-self:center">${item.tag}</span></div>`)}`}
    ${empty && html`<div class="empty">No changes yet. Edit or suspend a card on the Clashes tab and it shows up here until it reaches AnkiWeb.</div>`}
  `;
}

function Dismissed({ clashes }) {
  return html`
    <div class="banner"><span>These pairs stay hidden, from Find clashes and from the Save check, while their questions read about the same. If you reword a question a lot, the pair can come up again.</span></div>
    ${clashes.dismissed.length === 0 && html`<div class="empty">Nothing dismissed yet.</div>`}
    ${clashes.dismissed.map((item) => html`<div class="card chg" key=${item.id}>
      <div class="pairq"><span>${item.qa}</span><span>${item.qb}</span></div>
      <div class="rowg" style="justify-content:space-between">
        <span class="lab">${item.source}</span>
        <button class="lnk" onClick=${() => send(`/api/clashes/dismissed/${item.id}/undo`)}>Undo dismiss</button>
      </div>
    </div>`)}
  `;
}

export function Clashes() {
  const { clashes, ui } = useStore();
  const tab = ui.clashTab || "clashes";
  // A pair answered here stays listed until leaving, so its Undo stays in reach
  const kept = useRef(new Set());

  useEffect(() => {
    loadClashes().catch(showError);
  }, []);

  if (!clashes) return html`<${Top} /><div class="center centertop"><${Running}>Opening your cards<//></div>`;

  clashes.pairs.forEach((pair) => { if (!pair.closed) kept.current.add(pair.id); });
  const groups = connected(clashes.pairs.filter((pair) => kept.current.has(pair.id)));
  const { waiting, onReader } = clashes.changes;
  const tabs = [
    { id: "clashes", label: "Clashes", count: clashes.counts.clashes },
    { id: "changes", label: "Changes", count: clashes.counts.changes },
    { id: "dismissed", label: "Dismissed", count: clashes.counts.dismissed },
  ];

  return html`
    <${Top}><button class="lnk" onClick=${() => go("#/start")}>Back</button><//>
    <div class="center centertop"><div class="wide"><div class="stack">
      <div>
        <h1 class="h1">Find clashes</h1>
        <p class="lede" style="margin-top:6px">Cards whose questions read alike but whose answers differ, so a review can't tell which answer it wants. Reword one or suspend it here. Your changes go to the reader when it's plugged in.</p>
      </div>
      <${Status} clashes=${clashes} />
      <div class="seg" style="align-self:flex-start">
        ${tabs.map((item) => html`<button key=${item.id} class=${tab === item.id ? "sg on" : "sg"} onClick=${() => setUi({ clashTab: item.id })}>${item.label} (${item.count})</button>`)}
      </div>
      ${tab !== "changes" && waiting + onReader > 0 && html`<div class="banner">
        <span>${waiting > 0 ? `${plural(waiting, "change")} waiting for your reader.` : `${plural(onReader, "change")} on your reader, not opened yet.`}</span>
        <button class="lnk" onClick=${() => setUi({ clashTab: "changes" })}>See changes</button>
      </div>`}
      ${tab === "clashes" && clashes.noRaw && html`<div class="banner"><span>Some cards are only a simplified copy on this computer, so they can't be edited yet. Plug in your reader once and pbAnki will read them in full.</span></div>`}
      ${tab === "clashes" && groups.length === 0 && html`<div class="empty">No clashes to fix. Find clashes to look through your cards.</div>`}
      ${tab === "clashes" && groups.map((group) => html`<${Group} key=${group.rows[0].id} group=${group} clashes=${clashes} />`)}
      ${tab === "changes" && html`<${Changes} clashes=${clashes} />`}
      ${tab === "dismissed" && html`<${Dismissed} clashes=${clashes} />`}
    </div></div></div>
  `;
}
