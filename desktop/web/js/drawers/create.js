// Create a Book Companion drawer. Starts only on the button, never by itself.

import { html, Drawer, Pulse, Running } from "../ui.js";
import { useState } from "../../vendor/preact-htm.js";
import { useStore, closeDrawer, mutate, refreshBook } from "../store.js";

const NOTE = "font-size:13.5px";

function Steps() {
  return html`
    <p style="margin:0;font-size:15px;text-wrap:pretty">Long notes on the whole book: each chapter's key points, the book's own vocabulary, and what later research says. pbAnki runs your existing Book Companion skill on this book.</p>
    <div class="fl" style="gap:12px">
      <div class="step2"><span class="stepn">1</span><span>It opens in Terminal</span><span class="stepd">The skill may ask what you want from the book. You can skip the questions.</span></div>
      <div class="step2"><span class="stepn">2</span><span>It takes a while</span><span class="stepd">It reads the whole book using Opus. The Willpower Instinct took about 30 minutes. You can keep working here meanwhile.</span></div>
      <div class="step2"><span class="stepn">3</span><span>pbAnki notices when it's done</span><span class="stepd">It asks before copying the file beside the book on your reader, named so KOReader finds it.</span></div>
    </div>
  `;
}

// Copy to reader, with Keep unless the file is already kept.
function CopyButtons({ companion, busy, act, keep }) {
  return html`
    <div class="rowg">
      <button class="btn pri" onClick=${() => act("copy")} disabled=${!companion.can_copy || !!busy}>${busy === "copy" ? "Copying..." : "Copy to reader"}</button>
      ${keep && html`<button class="btn" onClick=${() => act("keep")} disabled=${!!busy}>Keep it on this computer</button>`}
    </div>
    ${!companion.can_copy && html`<div style=${NOTE}>Plug in your reader to copy it.</div>`}
  `;
}

export function CreateDrawer() {
  const { book, route } = useStore();
  const [busy, setBusy] = useState(null);

  if (!route.bookId) {
    return html`<${Drawer} title="Create a Book Companion"><p style="margin:0;font-size:15px">Pick a book first. A Book Companion belongs to one book.</p><//>`;
  }
  if (!book || book.id !== route.bookId) {
    return html`<${Drawer} title="Create a Book Companion"><${Running}>Opening the book<//><//>`;
  }

  const companion = book.companion;
  const act = async (action) => {
    setBusy(action);
    const answer = await mutate("POST", `/api/books/${encodeURIComponent(book.id)}/companion/${action}`);
    // A failed copy can leave a cut-off file, which the book then reports
    if (!answer) await refreshBook().catch(() => {});
    setBusy(null);
  };

  return html`<${Drawer} title="Create a Book Companion">
    <${Steps} />
    <div class="fl"><div class="lab">What it runs</div><div class="pfile">${companion.command}</div></div>
    ${companion.none &&
    html`<div class="rowg">
        <button class="btn pri" onClick=${() => act("create")} disabled=${!companion.can_create || !!busy}>Open in Terminal</button>
        <button class="btn" onClick=${closeDrawer}>Not now</button>
      </div>
      ${companion.problem && html`<div style=${NOTE}>${companion.problem}</div>`}`}
    ${companion.running &&
    html`<div class="waitb">
      <div class="rowg"><${Pulse} /><b style="font-weight:600">Running in Terminal</b></div>
      <div style=${NOTE}>Watching for ${companion.watching}</div>
      <div style=${NOTE}>Closed the Terminal window before it finished? Start over, then open it again.</div>
      <div class="rowg"><button class="lnk" style="color:var(--ink)" onClick=${() => act("forget")} disabled=${!!busy}>Start over</button></div>
    </div>`}
    ${companion.ready &&
    html`<div class="waitb">
      <b style="font-weight:600">Your Book Companion is ready</b>
      ${companion.cutOff
        ? html`<div style=${NOTE}>The last copy to your reader was cut off, so pbAnki won't use what it left there. Copy it again?</div>`
        : html`<div style=${NOTE}>Copy it beside the book on your reader? On-reader suggestions and the AI Assistant plugin will use it too.</div>`}
      <div class="path">${companion.dest}</div>
      <${CopyButtons} companion=${companion} busy=${busy} act=${act} keep />
    </div>`}
    ${companion.have && companion.cutOff &&
    html`<div class="waitb">
      <b style="font-weight:600">The copy to your reader was cut off</b>
      <div style=${NOTE}>pbAnki won't use what it left there. Copy it again?</div>
      <div class="path">${companion.dest}</div>
      <${CopyButtons} companion=${companion} busy=${busy} act=${act} />
    </div>`}
    ${companion.have && html`<div class="banner"><span>${companion.status}.</span><button class="btn" onClick=${closeDrawer}>Done</button></div>`}
  <//>`;
}
