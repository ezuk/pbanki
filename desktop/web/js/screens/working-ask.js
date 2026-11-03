// "Ask the AI for more": the panel under the cards, and the request it sends.

import { store, useStore, setUi, mutate, useEvent } from "../store.js";
import { html, plural, Running } from "../ui.js";
import { currentOptions, askCount, CountOpt, ChoiceOpt, ChosenPromptLine } from "./working-options.js";

// Also what the keyboard's suggest sends.
export function askForCards(book, highlight) {
  const { count, kind, model, prompt, sees, companion } = currentOptions(book);
  const body = { count, kind, model, prompt, sees, companion };
  const hint = (store.ui.hint || "").trim();
  if (hint) body.hint = hint;
  return mutate("POST", `/api/books/${book.id}/highlights/${highlight.id}/suggest`, body);
}

function clearHintWhenDone(event) {
  if (event.type === "suggest" && event.state === "done") setUi({ hint: "" });
}

export function AskPanel({ highlight, book }) {
  const { state, ui } = useStore();
  useEvent(clearHintWhenDone);
  const options = currentOptions(book);
  const busy = highlight.busy || highlight.writing;
  const label = busy ? "Writing..." : `Suggest ${plural(askCount(options), "card")}`;
  return html`<section class="comp">
    <div class="compt">Ask the AI for more</div>
    <textarea class="hint" rows="1" value=${ui.hint || ""} onInput=${(e) => setUi({ hint: e.target.value })} placeholder="Steer it if you like: ask what I can do this week, or use the swimming-bath image"></textarea>
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
    <${ChosenPromptLine} book=${book} />
  </section>`;
}
