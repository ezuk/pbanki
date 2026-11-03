// Prompt file drawer. Opened with openDrawer("prompt", { name, file }).

import { html, Drawer, Pulse, Running, useTick } from "../ui.js";
import { useEffect, useCallback, useRef, useState } from "../../vendor/preact-htm.js";
import { useStore, useEvent, openDrawer, showError } from "../store.js";
import { api } from "../api.js";

// Reads the prompt, and again when its files change. Answers {data, failure, reload}.
function usePrompt(name) {
  const [answer, setAnswer] = useState({ name: null, data: null, failure: null, at: 0 });
  const latest = useRef(name);
  latest.current = name;

  const reload = useCallback(async () => {
    try {
      const data = await api.get(`/api/prompts/${encodeURIComponent(name)}`);
      if (latest.current === name) setAnswer({ name, data, failure: null, at: Date.now() });
    } catch (error) {
      if (latest.current === name) setAnswer({ name, data: null, failure: error.message, at: 0 });
    }
  }, [name]);

  useEffect(() => {
    reload();
  }, [reload]);
  useEvent(
    useCallback(
      (event) => {
        if (event.topic === "prompts") reload();
      },
      [reload],
    ),
  );
  return answer.name === name ? { ...answer, reload } : { data: null, failure: null, at: 0, reload };
}

function PathButtons({ name, file, reload }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(file.display);
      setCopied(true);
    } catch {
      showError("That path couldn't be copied.");
    }
  };
  const launch = async (action) => {
    try {
      await api.post(`/api/prompts/${action}`, { name, file: file.file });
      await reload();
    } catch (error) {
      showError(error);
    }
  };
  return html`<div class="rowg" style="margin-top:4px">
    <button class="btn" onClick=${copy}>${copied ? "Copied" : "Copy path"}</button>
    <button class="btn" onClick=${() => launch("open")}>Open in editor</button>
    <button class="btn" onClick=${() => launch("reveal")}>Show in Finder</button>
  </div>`;
}

export function PromptDrawer() {
  const { route, state } = useStore();
  const name = route.query.name || state.settings.last_prompt;
  const { data, failure, at, reload } = usePrompt(name);
  useTick();

  const files = data ? data.files : [];
  const kindFile = (state.kinds.find((k) => k.id === state.settings.kind) || {}).file;
  const file = files.find((f) => f.file === route.query.file) || files.find((f) => f.file === kindFile) || files[0];
  const reloadAgo = data ? data.reloadAgo + Math.floor((Date.now() - at) / 1000) : 0;

  return html`<${Drawer} title=${`Prompt: ${name}`}>
    ${file &&
    html`<div class="fl">
      <div class="lab">Edit this file in any editor</div>
      <div class="path" style="font-size:13px">${file.display}</div>
      <${PathButtons} key=${file.display} name=${name} file=${file} reload=${reload} />
    </div>`}
    ${file && file.undone_on_start && html`<div class="setn">pbAnki puts Standard back each time it starts, so edits here are undone. To make your own, copy the Standard folder and rename the copy.</div>`}
    ${data && html`<div class="running"><${Pulse} />Watching for changes. Reloaded ${reloadAgo} s ago.</div>`}
    ${!data && !failure && html`<${Running}>Reading the prompt<//>`}
    ${failure && html`<div class="setn">${failure}</div>`}
    ${data &&
    html`<div class="fl" style="gap:0">
      <div class="lab" style="padding-bottom:6px">Files in this prompt</div>
      ${files.map(
        (f) => html`<button key=${f.file} class=${f === file ? "pf-r on" : "pf-r"} onClick=${() => openDrawer("prompt", { name, file: f.file })}>
          <span>${f.name}</span><span class="pf-rn">${f.note}</span>
        </button>`,
      )}
    </div>`}
    ${file && html`<div class="pfile">${file.text}</div>`}
  <//>`;
}
