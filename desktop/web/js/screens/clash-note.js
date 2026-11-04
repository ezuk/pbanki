// Says when a new card may be mistaken for one the reader already studies.

import { html } from "../ui.js";

export function ClashNote({ clash }) {
  if (!clash) return null;
  return html`<div class="warn"><span>May be confused with: ${clash.front} → ${clash.back}</span></div>`;
}
