// The highlighted passage, with Show more and definition cards from selected words.

import { useEffect, useState } from "../../vendor/preact-htm.js";
import { store, mutate, useEvent } from "../store.js";
import { html } from "../ui.js";

// The text selected inside the passage, or "".
export function selectedPassageText() {
  const selection = window.getSelection();
  const node = selection && selection.anchorNode;
  const el = node && (node.nodeType === 1 ? node : node.parentElement);
  if (!el || !el.closest || !el.closest(".pass")) return "";
  return selection.toString().trim().replace(/\s+/g, " ");
}

// 1 to 3 words selected inside the passage, or "".
function selectedTerm() {
  const selection = window.getSelection();
  const text = ((selection && selection.toString()) || "").trim().replace(/\s+/g, " ");
  const node = selection && selection.anchorNode;
  const el = node && (node.nodeType === 1 ? node : node.parentElement);
  const inPassage = el && el.closest && el.closest(".pass");
  const words = text ? text.split(" ").length : 0;
  return inPassage && words >= 1 && words <= 3 ? text.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "") : "";
}

export function Passage({ highlight, book }) {
  const [more, setMore] = useState(false);
  const [term, setTerm] = useState("");
  const [defining, setDefining] = useState(false);

  useEffect(() => {
    const onChange = () => setTerm(selectedTerm());
    document.addEventListener("selectionchange", onChange);
    return () => document.removeEventListener("selectionchange", onChange);
  }, []);

  useEvent((event) => {
    if (event.type === "define") setDefining(false);
  });

  const define = async (event) => {
    event.preventDefault();
    if (!term) return;
    window.getSelection().removeAllRanges();
    setDefining(true);
    const body = { term, model: store.state.settings.model };
    const answer = await mutate("POST", `/api/books/${book.id}/highlights/${highlight.id}/define`, body);
    if (!answer) setDefining(false);
  };

  const label = defining && highlight.busy ? "Writing definition..." : term ? `Make definition cards for "${term}"` : "Select 1 to 3 words to define them";
  const hasMore = highlight.before2 || highlight.after2;
  const atEnd = more ? highlight.endCh2 : highlight.endCh;
  return html`<article class="pass">
    ${more && highlight.before2 && html`<p class="ctx">${highlight.before2}</p>`}
    ${highlight.before && html`<p class="ctx">${highlight.before}</p>`}
    <p><span>${highlight.pre}</span><mark class="mk" style="color:inherit">${highlight.mark}</mark><span>${highlight.post}</span></p>
    ${highlight.after && html`<p class="ctx">${highlight.after}</p>`}
    ${more && highlight.after2 && html`<p class="ctx">${highlight.after2}</p>`}
    ${atEnd && html`<div class="endch">End of the chapter</div>`}
    <div class="passft">
      ${hasMore && html`<button class="lnk" onClick=${() => setMore(!more)}>${more ? "Show less" : "Show more of the book"}</button>`}
      ${highlight.canEdit && html`<button class="btn" onMouseDown=${define} disabled=${!term || highlight.busy} style="font-family:var(--mono)">${label}</button>`}
    </div>
    ${highlight.note && html`<div class="note"><span class="notel">Your note</span><span style="font-family:var(--sans);font-size:15px">${highlight.note}</span></div>`}
  </article>`;
}
