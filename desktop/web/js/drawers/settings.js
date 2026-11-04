// Settings drawer.

import { html, Drawer, Field, ReaderChip, Select } from "../ui.js";
import { useStore, saveSettings, saveMount } from "../store.js";

const SettingSelect = ({ label, value, options, name }) => html`<div class="setrow">
  <span>${label}</span>
  <${Select} value=${value} options=${options} onChange=${(next) => saveSettings({ [name]: next })} />
</div>`;

function Assistant({ assistant, all }) {
  const toggle = (event) => saveSettings({ assistants: { ...all, [assistant.id]: event.target.checked } });
  return html`<label class="asst">
    <input type="checkbox" checked=${assistant.on && assistant.found} disabled=${!assistant.found} onChange=${toggle} />
    <span>${assistant.name}</span>
    <span class="assts">${assistant.status}</span>
    <span class="asstp">${assistant.path}</span>
  </label>`;
}

export function SettingsDrawer() {
  const { state, book, route } = useStore();
  const { settings, files, skill } = state;
  const openBook = book && book.id === route.bookId ? book : null;
  return html`<${Drawer} title="Settings">
    <div class="setg">
      <div class="setgt">Reader</div>
      <div class="fl">
        <div class="lab">Where it mounts</div>
        <${Field} tag="input" commit class="in" value=${settings.mount} onSave=${saveMount} />
      </div>
      <div class="setrow"><span>Current status</span><${ReaderChip} /></div>
    </div>
    <div class="setg">
      <div class="setgt">AI</div>
      ${state.assistants.map((a) => html`<${Assistant} key=${a.id} assistant=${a} all=${settings.assistants} />`)}
      <div class="setn">Turn on both and each model menu lists them together, so you can use a different one per job.</div>
      <${SettingSelect} label="Model for highlight cards" name="model" value=${settings.model} options=${state.models} />
      <${SettingSelect} label="Model for connections" name="meta_model" value=${settings.meta_model} options=${state.models} />
    </div>
    <div class="setg">
      <div class="setgt">Book Companion</div>
      ${openBook && html`<div class="setrow"><span>This book</span><span class="assts">${openBook.companion.status}</span></div>`}
      <${SettingSelect} label="What the AI reads per highlight" name="companion_send" value=${settings.companion_send} options=${state.comp_opts} />
      <div class="setn">Overview + chapter keeps each request small. Connections and key ideas always read the whole Companion.</div>
      <div class="fl">
        <div class="lab">Folder with the Book Companion skill</div>
        <input class="in" value=${files.assistant} readOnly />
      </div>
      ${skill.problem && html`<div class="setn">${skill.problem}</div>`}
      <div class="setn">Creating a Companion always uses Claude Code, because the skill runs there.</div>
    </div>
    <div class="setg">
      <div class="setgt">Files</div>
      <div class="fl"><div class="lab">Prompts folder</div><div class="path">${files.prompts}</div></div>
      <div class="fl"><div class="lab">Backup and drafts</div><div class="path">${files.db}</div></div>
    </div>
  <//>`;
}
