// Plug in and Reader check.

import { api } from "../api.js";
import { store, useStore, go, openBook, saveSettings, showError, clearError, loadState, loadBooks, update, setUi } from "../store.js";
import { html, ErrorBar, Field, Pulse, CheckRow } from "../ui.js";

export function PlugIn() {
  const { state, ui } = useStore();
  const { reader, resume, settings } = state;

  const chooseFolder = async () => {
    try {
      await api.post("/api/reader/choose-folder");
      await loadState();
    } catch (error) {
      showError(error);
    }
  };

  return html`
    <${ErrorBar} />
    <div class="center"><div class="narrow">
      <h1 class="h1">Plug in your PocketBook</h1>
      <p class="lede">pbAnki gets your book and highlights from the reader, so it has to be connected before you start. This page moves on by itself as soon as it appears.</p>
      <div class="watch"><${Pulse} /><span>Looking for ${reader.mount}</span><span style="color:var(--ink3)">checked ${reader.checked_ago} s ago</span></div>
      <div class="fl"><div class="lab">Where the reader mounts</div><div class="rowg">
        <${Field} tag="input" commit class="in" style="flex:1 1 260px;width:auto" value=${settings.mount} onSave=${(mount) => saveSettings({ mount })} />
        <button class="btn" onClick=${chooseFolder}>Choose folder</button>
      </div></div>
      ${resume && html`<div class="resume">
        <div><div style="font-weight:500" class="rowg">${resume.text}</div><div style="font-size:13.5px;color:var(--ink2)">${resume.detail}</div></div>
        <button class="btn" onClick=${() => openBook(resume.book_id, "highlights")} disabled=${!!ui.opening}>Keep working</button>
      </div>`}
    </div></div>
  `;
}

export function ReaderCheck() {
  const { state, ui } = useStore();
  const { reader } = state;

  const checkAgain = async () => {
    clearError();
    try {
      await loadState();
      await loadBooks();
    } catch (error) {
      showError(error);
    }
  };

  const updatePlugin = async () => {
    clearError();
    setUi({ updatingPlugin: true });
    try {
      const answer = await api.post("/api/reader/plugin/update");
      update({ state: { ...store.state, reader: answer.reader } });
    } catch (error) {
      showError(error);
    } finally {
      setUi({ updatingPlugin: false });
    }
  };

  return html`
    <${ErrorBar} />
    <div class="center"><div class="narrow" style="gap:10px">
      <h1 class="h1">${reader.title}</h1>
      <div style="border-top:3px solid var(--ink);margin-top:12px">
        ${reader.rows.map((row) => html`<${CheckRow} key=${row.title} row=${row}>
          ${row.detail && html`<span class="chkp">${row.detail}</span>`}
          ${row.action === "update_plugin" && html`<button class="btn" style="grid-column:2;justify-self:start;margin-top:6px" onClick=${updatePlugin} disabled=${!!ui.updatingPlugin}>${ui.updatingPlugin ? "Updating plugin..." : "Update plugin"}</button>`}
        <//>`)}
      </div>
      <div class="rowg" style="margin-top:12px">
        ${reader.ready
          ? html`<button class="btn pri" onClick=${() => go("#/books")}>Show my books</button>`
          : html`<button class="btn pri" onClick=${checkAgain}>Check again</button>`}
      </div>
    </div></div>
  `;
}
