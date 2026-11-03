// The choices the AI panels share: read from settings, kept in settings when changed.

import { store, useStore, update, setUi, saveSettings, openDrawer } from "../store.js";
import { html, Opt, Select, Stepper, PromptLine, useReloadAgo } from "../ui.js";

// Option name to the setting that holds it
const SETTING = {
  count: "count",
  kind: "kind",
  model: "model",
  sees: "sees",
  companion: "companion_send",
  connections: "suggest_connections",
  meta_model: "meta_model",
  ideas: "suggest_ideas",
};

export function currentOptions(book) {
  const { settings } = store.state;
  const chosen = Object.fromEntries(Object.entries(SETTING).map(([option, setting]) => [option, settings[setting]]));
  chosen.prompt = (store.ui.prompts || {})[book.id] ?? book.options.prompt;
  return chosen;
}

export function setOption(book, option, value) {
  if (option === "prompt") {
    setUi({ prompts: { ...store.ui.prompts, [book.id]: value } });
    saveSettings({ last_prompt: value });
    return;
  }
  const setting = SETTING[option];
  update({ state: { ...store.state, settings: { ...store.state.settings, [setting]: value } } });
  saveSettings({ [setting]: value });
}

// The number of cards a request makes. Cloze always asks for one.
export const askCount = (options) => (options.kind === "cloze" ? 1 : options.count);

export function CountOpt({ book, label }) {
  const options = currentOptions(book);
  return html`<${Opt} label=${label}><${Stepper} value=${options.count} onChange=${(n) => setOption(book, "count", n)} /><//>`;
}

// A dropdown bound to one option. `choices` are what Select takes.
export function ChoiceOpt({ book, label, option, choices }) {
  const options = currentOptions(book);
  return html`<${Opt} label=${label}><${Select} value=${options[option]} options=${choices} onChange=${(value) => setOption(book, option, value)} /><//>`;
}

// "Prompt file <path> Open prompt reloaded N s ago" for the chosen prompt and kind.
export function ChosenPromptLine({ book }) {
  const { state } = useStore();
  const reloadAgo = useReloadAgo();
  const options = currentOptions(book);
  const file = (state.kinds.find((kind) => kind.id === options.kind) || state.kinds[0]).file;
  const folder = state.prompts.folders.find((item) => item.name === options.prompt);
  const row = folder && folder.files.find((item) => item.file === file);
  return html`<${PromptLine}
    path=${row ? row.display : ""}
    reloadAgo=${reloadAgo}
    onOpen=${() => openDrawer("prompt", { name: options.prompt, file })}
  />`;
}
