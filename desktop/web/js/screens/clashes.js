// Find clashes: questions so similar that a review can't tell which answer is wanted.

import { useEffect } from "../../vendor/preact-htm.js";
import { useStore, go, setUi, loadClashes, showError, clearError, update } from "../store.js";
import { api } from "../api.js";
import { html, Top, Running, plural } from "../ui.js";

const TABS = [
  { id: "new", label: "Please review" },
  { id: "real", label: "Clashes" },
  { id: "no", label: "Not a clash" },
];

const EMPTY = {
  new: "Nothing to review. Find clashes to look through your cards.",
  real: "No clashes yet. Pairs you mark Real clash wait here until you reword one of the questions.",
  no: "No pairs marked yet.",
};

async function send(path, body) {
  clearError();
  try {
    update({ clashes: await api.post(path, body) });
  } catch (error) {
    showError(error);
  }
}

function Side({ card }) {
  return html`<div class="clashc">
    <span class="clashd">${card.deck}</span>
    <div class="clashq">${card.front}</div>
    ${card.back && html`<div class="clasha">${card.back}</div>`}
  </div>`;
}

function Pair({ pair }) {
  const mark = (verdict) => send(`/api/clashes/pairs/${pair.id}`, { verdict: pair.state === verdict ? "new" : verdict });
  return html`<div class="card clashp">
    <div class="clashs"><${Side} card=${pair.first} /><${Side} card=${pair.second} /></div>
    ${pair.why && html`<div class="clashw">${pair.why}</div>`}
    <div class="rowg">
      <button class=${pair.state === "real" ? "btn pri" : "btn"} onClick=${() => mark("real")}>Real clash</button>
      <button class=${pair.state === "no" ? "btn pri" : "btn"} onClick=${() => mark("no")}>Not a clash</button>
    </div>
  </div>`;
}

function Status({ clashes, connected }) {
  const { running, progress, error, cards, fixed } = clashes;
  if (running) {
    const count = progress && progress.total ? ` (${progress.done} of ${progress.total})` : "";
    return html`<div class="rowg">
      <${Running}>${progress ? progress.step : "Starting"}${count}<//>
      <button class="lnk" onClick=${() => send("/api/clashes/stop")}>Stop</button>
    </div>`;
  }
  return html`<div class="fl">
    ${error && html`<div class="errb"><span>${error}</span></div>`}
    <div class="rowg">
      <button class="btn pri" disabled=${!cards && !connected} onClick=${() => send("/api/clashes/find")}>Find clashes</button>
      <span class="lab">${cards ? plural(cards, "card") : "Plug in your reader to read your cards"}</span>
      ${fixed > 0 && html`<span class="lab">${fixed} fixed since you marked ${fixed === 1 ? "it" : "them"}</span>`}
    </div>
  </div>`;
}

export function Clashes() {
  const { clashes, state, ui } = useStore();
  const tab = ui.clashTab || "new";

  useEffect(() => {
    loadClashes().catch(showError);
  }, []);

  if (!clashes) return html`<${Top} /><div class="center centertop"><${Running}>Opening your cards<//></div>`;

  const groups = clashes.groups
    .map((pairs) => pairs.filter((pair) => pair.state === tab))
    .filter((pairs) => pairs.length);

  return html`
    <${Top}><button class="lnk" onClick=${() => go("#/start")}>Back</button><//>
    <div class="center centertop"><div class="wide">
      <div>
        <h1 class="h1">Find clashes</h1>
        <p class="lede" style="margin-top:6px">Cards whose questions read alike but whose answers differ, so a review can't tell which answer it wants. To fix one, reword a question on your reader, then plug it in again.</p>
      </div>
      <${Status} clashes=${clashes} connected=${state.reader.connected} />
      <div class="seg" style="align-self:flex-start">
        ${TABS.map((item) => html`<button key=${item.id} class=${tab === item.id ? "sg on" : "sg"} onClick=${() => setUi({ clashTab: item.id })}>${item.label} (${clashes.counts[item.id]})</button>`)}
      </div>
      ${groups.length === 0 && html`<div class="empty">${EMPTY[tab]}</div>`}
      ${groups.map((pairs) => html`<div class="fl" key=${pairs[0].id}>
        ${pairs.length > 1 && html`<div class="lab">${pairs.length} connected pairs. Rewording one question may fix several.</div>`}
        ${pairs.map((pair) => html`<${Pair} key=${pair.id} pair=${pair} />`)}
      </div>`)}
    </div></div>
  `;
}
