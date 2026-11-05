// Says when a new card may be mistaken for one the reader already studies.

import { html } from "../ui.js";

export function ClashNote({ clash, onDismiss }) {
  if (!clash || clash.closed) return null;
  return html`<div class="clashn">
    <div class="clashh"><span class="rowg"><span class="clasht">CLASH</span>May be confused with</span>${onDismiss && html`<button class="lnk" style="color:var(--ink)" onClick=${onDismiss}>Dismiss</button>`}</div>
    <div class="clashr"><span class="clashl">Question</span><span class="clashnq">${clash.front}</span></div>
    ${clash.back && html`<div class="clashr"><span class="clashl">Answer</span><span>${clash.back}</span></div>`}
  </div>`;
}
