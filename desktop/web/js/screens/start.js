// Choose what to do: make new cards or tidy up existing ones.

import { useEffect } from "../../vendor/preact-htm.js";
import { go, useStore, loadClashes, showError } from "../store.js";
import { html, Top, plural } from "../ui.js";

function Choice({ title, detail, to, children }) {
  return html`<button class="choice" onClick=${() => go(to)}>
    <span class="choicet">${title}</span>
    <span class="choiced">${detail}</span>
    ${children}
  </button>`;
}

export function Start() {
  const { clashes } = useStore();
  useEffect(() => {
    loadClashes().catch(showError);
  }, []);
  const count = clashes ? clashes.counts.clashes : 0;
  const waiting = clashes ? clashes.changes.waiting : 0;
  return html`
    <${Top} />
    <div class="center"><div class="narrow">
      <h1 class="h1">What would you like to do?</h1>
      <div class="choices">
        <${Choice} title="Make new cards" detail="Pick a book and turn its highlights into cards." to="#/books" />
        <${Choice} title="Tidy up cards" detail="Find cards whose questions read alike but whose answers differ, and fix them here." to="#/clashes">
          ${(count > 0 || waiting > 0) && html`<span class="rowg" style="padding-top:4px">
            ${count > 0 && html`<span class="tg neu">${count === 1 ? "1 clash" : `${count} clashes`}</span>`}
            ${waiting > 0 && html`<span class="tg wait">${plural(waiting, "change")} waiting for your reader</span>`}
          </span>`}
        <//>
      </div>
    </div></div>
  `;
}
