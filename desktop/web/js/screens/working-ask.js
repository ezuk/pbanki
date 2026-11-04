// "Ask the AI for more": the panel under the cards, and the request it sends.

import { store, useStore, setUi, mutate, onEvent } from "../store.js";
import { html, plural, Running } from "../ui.js";
import { currentOptions, askCount, CountOpt, ChoiceOpt, ChosenPromptLine } from "./working-options.js";

// Each highlight has its own hint.
const hintOf = (book, highlight) => (store.ui.hints || {})[`${book.id}/${highlight.id}`] || "";

function setHint(key, text) {
  setUi({ hints: { ...store.ui.hints, [key]: text } });
}

// The hint each request went out with
const sentHints = new Map();

// Also what the keyboard's suggest sends. A request under way isn't sent again.
export async function askForCards(book, highlight) {
  const key = `${book.id}/${highlight.id}`;
  if ((store.ui.asking || {})[key]) return null;
  const { count, kind, model, prompt, sees, companion } = currentOptions(book);
  const body = { count, kind, model, prompt, sees, companion };
  const typed = hintOf(book, highlight);
  const hint = typed.trim();
  if (hint) body.hint = hint;
  sentHints.set(key, typed);
  setUi({ asking: { ...store.ui.asking, [key]: true } });
  try {
    return await mutate("POST", `/api/books/${book.id}/highlights/${highlight.id}/suggest`, body);
  } finally {
    setUi({ asking: { ...store.ui.asking, [key]: false } });
  }
}

// A finished request clears the hint it used, unless more has been typed since.
onEvent((event) => {
  if (event.type !== "suggest" || event.state !== "done") return;
  const key = `${event.book_id}/${event.hid}`;
  const sent = sentHints.get(key);
  sentHints.delete(key);
  if (sent && (store.ui.hints || {})[key] === sent) setHint(key, "");
});

export function AskPanel({ highlight, book }) {
  const { state, ui } = useStore();
  const options = currentOptions(book);
  const key = `${book.id}/${highlight.id}`;
  const busy = highlight.busy || highlight.writing || !!(ui.asking || {})[key];
  const label = busy ? "Writing..." : `Suggest ${plural(askCount(options), "card")}`;
  return html`<section class="comp">
    <div class="compt">Ask the AI for more</div>
    <textarea class="hint" rows="1" value=${hintOf(book, highlight)} onInput=${(e) => setHint(key, e.target.value)} placeholder="Steer it if you like: ask what I can do this week, or use the swimming-bath image"></textarea>
    <div class="opts">
      <${CountOpt} book=${book} label="How many" />
      <${ChoiceOpt} book=${book} label="Kind" option="kind" choices=${state.kinds} />
      <${ChoiceOpt} book=${book} label="Model" option="model" choices=${state.models} />
      <${ChoiceOpt} book=${book} label="Prompt" option="prompt" choices=${state.prompts.names} />
      <${ChoiceOpt} book=${book} label="AI reads" option="sees" choices=${state.sees_opts} />
      ${book.companion.have && html`<${ChoiceOpt} book=${book} label="Companion" option="companion" choices=${state.comp_opts} />`}
      <button class="btn pri go" onClick=${() => askForCards(book, highlight)} disabled=${busy}>${label}</button>
    </div>
    ${highlight.busy && highlight.busyCmd && html`<${Running}>Running ${highlight.busyCmd}<//>`}
    ${book.aiNote && html`<div class="running">${book.aiNote}</div>`}
    <${ChosenPromptLine} book=${book} />
  </section>`;
}
