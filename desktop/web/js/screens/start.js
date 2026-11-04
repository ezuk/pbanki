// Choose what to do: make new cards or tidy up existing ones.

import { go } from "../store.js";
import { html, Top } from "../ui.js";

function Choice({ title, detail, to }) {
  return html`<button class="choice" onClick=${() => go(to)}>
    <span class="choicet">${title}</span>
    <span class="choiced">${detail}</span>
  </button>`;
}

export function Start() {
  return html`
    <${Top} />
    <div class="center"><div class="narrow">
      <h1 class="h1">What would you like to do?</h1>
      <div class="choices">
        <${Choice} title="Make new cards" detail="Pick a book and turn its highlights into cards." to="#/books" />
        <${Choice} title="Tidy up cards" detail="Find cards whose questions read alike but whose answers differ." to="#/clashes" />
      </div>
    </div></div>
  `;
}
