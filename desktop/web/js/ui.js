// Pieces the mock repeats across screens.

import { html, useEffect, useLayoutEffect, useRef, useState } from "../vendor/preact-htm.js";
import { store, useStore, go, openDrawer, closeDrawer, clearError, debouncedSave, bookPath, here } from "./store.js";

export { html };

export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// Whether a key press went to something being typed in, for keyboard shortcuts.
export function isTyping(event) {
  const el = event.target;
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
}

// Top bar

const LOST = "pbAnki stopped answering. Check it's still running in Terminal. Your work is safe on this computer.";

// The error strip. Screens with a Top get it from there.
export function ErrorBar() {
  const { error, lost } = useStore();
  return html`
    ${lost && html`<div class="unplug">${LOST}</div>`}
    ${error &&
    html`<div class="unplug"><span>${error}</span><button class="lnk" style="color:var(--ink)" onClick=${clearError}>Dismiss</button></div>`}
  `;
}

export function ReaderChip() {
  const { state } = useStore();
  const reader = state.reader;
  return html`<span class="chip"><span class=${reader.dot}></span>${reader.label}</span>`;
}

export function BookButton({ book }) {
  return html`<button class="bookbtn" onClick=${() => go("#/books")} style="max-width:300px">
    <span class="bookt">${book.title}</span>
    <span class="booka">${book.author ? `${book.author} · ` : ""}change book</span>
  </button>`;
}

export const SettingsLink = () => html`<button class="lnk" onClick=${() => openDrawer("settings")}>Settings</button>`;

// The bar across the top. `middle` goes after the brand, `children` before the right-hand end's close.
export function Top({ middle, settings = true, children }) {
  return html`
    <div class="top">
      <span class="brand">pbAnki</span>
      ${middle}
      <div class="topr"><${ReaderChip} />${settings && html`<${SettingsLink} />`}${children}</div>
    </div>
    <${ErrorBar} />
  `;
}

export function UnplugBar() {
  const { state } = useStore();
  if (state.reader.connected) return null;
  return html`<div class="unplug">Your reader is unplugged. Keep working. Your cards stay on this computer until you plug it in and save.</div>`;
}

// The top bar of Highlights, Connections and Key ideas. `tab` is the route name of the one showing.
export function WorkTop({ tab }) {
  const { book } = useStore();
  const tabs = [
    ["highlights", "Highlights"],
    ["connections", `Connections ${book.counts.metaCount}`],
    ["ideas", `Key ideas ${book.counts.ideaCount}`],
  ];
  const middle = html`
    <${BookButton} book=${book} />
    <div class="seg">
      ${tabs.map(([id, label]) => html`<button key=${id} class=${tab === id ? "sg on" : "sg"} onClick=${() => go(bookPath(book.id, id))}>${label}</button>`)}
    </div>
  `;
  return html`
    <${Top} middle=${middle}>
      <button class="btn pri" onClick=${() => go(here("save"))} disabled=${book.save.noCards}>${book.save.saveLabel}</button>
    <//>
    <${UnplugBar} />
  `;
}

// Small repeated parts

export const Pulse = () => html`<span class="dot pulse"></span>`;

// "Running claude -p ..." and other lines with a pulsing dot.
export const Running = ({ children }) => html`<div class="running"><${Pulse} />${children}</div>`;

// A row of the Reader check or the run progress: {cls, glyph, title, path}.
export function CheckRow({ row, children }) {
  return html`<div class=${row.cls || "chk"}>
    <span class="chkg">${row.glyph}</span>
    <span class="chkt">${row.title}</span>
    <span class="chkp">${row.path}</span>
    ${children}
  </div>`;
}

export const Opt = ({ label, children }) => html`<div class="opt"><span class="lab">${label}</span>${children}</div>`;

// A dropdown. `options` are {id, label} or plain strings.
export function Select({ value, options, onChange }) {
  return html`<select class="sel" value=${value} onChange=${(e) => onChange(e.target.value)}>
    ${options.map((option) => {
      const id = typeof option === "string" ? option : option.id;
      const label = typeof option === "string" ? option : option.label;
      return html`<option key=${id} value=${id} selected=${id === value}>${label}</option>`;
    })}
  </select>`;
}

export function Stepper({ value, onChange, min = 1, max = 10 }) {
  return html`<div class="step">
    <button class="stepb" onClick=${() => onChange(Math.max(min, value - 1))}>−</button>
    <span class="stepv">${value}</span>
    <button class="stepb" onClick=${() => onChange(Math.min(max, value + 1))}>+</button>
  </div>`;
}

// Re-renders every second while mounted.
export function useTick() {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, []);
}

// Seconds since the prompts reloaded, counting up between fetches of the state.
export function useReloadAgo() {
  const { state } = useStore();
  const prompts = state.prompts;
  const from = useRef(null);
  if (!from.current || from.current.prompts !== prompts) from.current = { prompts, at: Date.now() };
  useTick();
  return prompts.reloadAgo + Math.floor((Date.now() - from.current.at) / 1000);
}

// "Prompt file <path> Open prompt reloaded N s ago"
export function PromptLine({ path, reloadAgo, onOpen }) {
  return html`<div class="pline">
    <span>Prompt file</span><span class="path">${path}</span>
    <button class="lnk" onClick=${onOpen}>Open prompt</button>
    <span>reloaded ${reloadAgo} s ago</span>
  </div>`;
}

// The e-reader's status line, as the previews show it.
export const EinkBar = ({ style }) => html`<div class="einkb" style=${style}><span>KOReader</span><span>09:41</span></div>`;

// A drawer on its scrim, with the title and close button. Fills the rest with `children`.
export function Drawer({ title, children }) {
  return html`<div class="scrim" onClick=${closeDrawer}>
    <div class="drawer" onClick=${(e) => e.stopPropagation()}>
      <div class="drh"><h2 class="drt">${title}</h2><button class="x" onClick=${closeDrawer} aria-label="Close">✕</button></div>
      <${ErrorBar} />
      ${children}
    </div>
  </div>`;
}

// A text field that saves itself. Give it a `key` per record. Once the user types, a new `value`
// from the server waits until they leave the field and the text is saved. `onSave(text)` returns
// a promise, which rejects if the save failed: the text then stays, the error shows, and the next
// blur tries again. `commit` saves only on blur or Enter, for values a half-typed entry would break.
// `tag` is "textarea" (the default) or "input"; everything else goes on the element.
export function Field({ value, onSave, commit = false, tag = "textarea", ...rest }) {
  const [, rerender] = useState(0);
  // The user's text, or null when showing `value`
  const draft = useRef(null);
  const ref = useRef(null);
  const latest = useRef(onSave);
  latest.current = onSave;
  const saver = useRef(null);
  if (!saver.current) saver.current = debouncedSave((text) => latest.current(text));

  useEffect(() => () => saver.current.flush(), []);

  const shown = draft.current ?? value ?? "";
  useLayoutEffect(() => {
    // Browsers without field-sizing: grow with the text
    const el = ref.current;
    if (tag === "textarea" && el && !CSS.supports("field-sizing", "content")) {
      el.style.height = "auto";
      el.style.height = `${el.scrollHeight + 2}px`;
    }
  }, [shown]);

  const onInput = (event) => {
    draft.current = event.target.value;
    if (!commit) saver.current.set(draft.current);
    rerender((n) => n + 1);
  };
  const onBlur = async (event) => {
    if (commit && draft.current !== null && draft.current !== (value ?? "")) saver.current.set(draft.current);
    await saver.current.flush();
    if (!saver.current.waiting() && document.activeElement !== event.target) {
      draft.current = null;
      rerender((n) => n + 1);
    }
  };
  const onKeyDown = (event) => {
    if (commit && event.key === "Enter") event.target.blur();
    if (rest.onKeyDown) rest.onKeyDown(event);
  };

  const Tag = tag;
  return html`<${Tag} ...${rest} ref=${ref} value=${shown} onInput=${onInput} onBlur=${onBlur} onKeyDown=${onKeyDown} />`;
}

// While a screen waits on the server.
export const Waiting = ({ children }) => html`<div class="center"><div class="narrow"><${Running}>${children}<//></div></div>`;
