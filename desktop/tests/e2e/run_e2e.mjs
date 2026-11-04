#!/usr/bin/env node
// End-to-end test of the desktop companion: the real server and a real browser, a fake reader and
// fake AI, in temporary folders. See README.md here. Everything it asserts on comes from what the
// screens showed, the sidecars on the fake reader and the bytes of the reader's files.

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "../../..");
const INBOX = "applications/pbanki/inbox.jsonl";
const REVIEWED = "applications/pbanki/reviewed.jsonl";
const SHOTS = process.env.E2E_SCREENSHOTS || path.join(REPO, ".playwright-mcp/e2e");
const OUTPUT = process.env.E2E_OUTPUT || path.join(os.tmpdir(), "pbanki-e2e-output");

const require = createRequire(process.env.E2E_PLAYWRIGHT_DIR ? path.join(path.resolve(process.env.E2E_PLAYWRIGHT_DIR), "x.js") : import.meta.url);
const { chromium } = require("playwright");

// The fake reader, through the same helpers the dev server and unit tests use

const PYTHON = `
import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path[:0] = [${JSON.stringify(HERE)}, ${JSON.stringify(path.join(HERE, ".."))}, ${JSON.stringify(path.join(HERE, "../.."))}]
import dev_server
from fixtures.make_reader import FakeReader, build_reader
from pbanki_desktop import lua

COMPANION = """# Pride and Prejudice

## 1. Overview

A comedy of manners about money, marriage and first impressions.

## 2. Chapter by chapter

### Chapter I.

The Bennets hear that a rich young man has taken the house nearby.

- Marriage is treated as a matter of money.
- Mrs. Bennet's nerves drive the household.

### I hope Mr. Bingley will like it. CHAPTER II.

Mr. Bennet has already called on the new neighbour, and keeps it from his family.

- He teases his wife by holding back the news.

### CHAPTER IV.

Jane and Elizabeth talk over the ball.

- Jane thinks well of everybody, and Elizabeth does not.

### A note for Miss Bennet. CHAPTER VII.

The younger girls chase the officers while Jane is asked to Netherfield.

- Mr. Bennet mocks his daughters' silliness.
"""

if sys.argv[1] == "build":
    root = Path(sys.argv[2])
    mount = root / "PB629"
    fake = build_reader(mount)
    fake.local("pride").with_name("book.companion.md").write_text(COMPANION, encoding="utf-8")
    dev_server.make_assistant(root / "assistant")
    (root / "home").mkdir()
    settings = SimpleNamespace(companion_seconds=5, ai="believable", ai_delay=0.1)
    annotations = {}
    for name in ("pride", "pending", "mostly_done"):
        sidecar = fake.local(name).with_suffix(".sdr") / "metadata.epub.lua"
        items = lua.sequence(lua.load_file(sidecar)["annotations"])
        annotations[name] = [item for item in items if item.get("drawer")]
    print(json.dumps({"books": fake.books, "annotations": annotations, "snapshot": fake.snapshot(),
                      "env": dev_server.environment(root, mount, settings)}))
else:
    print(json.dumps(FakeReader(sys.argv[2]).snapshot()))
`;

function python(...args) {
  const done = spawnSync("python3", ["-c", PYTHON, ...args], { encoding: "utf-8", maxBuffer: 1 << 26, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  if (done.status !== 0) throw new Error(`The Python helper failed:\n${done.stderr}`);
  return JSON.parse(done.stdout);
}

// Checks

class Failed extends Error {}

function ok(value, message) {
  if (!value) throw new Failed(message);
}

function eq(actual, expected, message) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) throw new Failed(`${message}\n  expected ${b}\n  actual   ${a}`);
}

const squeeze = (text) => text.replace(/\s+/g, " ").trim();

// The run

const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pbanki-e2e-")));
const mount = path.join(root, "PB629");
const away = `${mount}.unplugged`;
const aiLog = path.join(root, "ai.jsonl");
const serverLog = path.join(root, "server.log");
ok(!mount.startsWith("/Volumes") && !root.includes("Application Support"), "The test must stay in temporary folders");

const reader = python("build", root);
const env = { ...reader.env, FAKE_AI_LOG: aiLog, PYTHONDONTWRITEBYTECODE: "1" };
ok(env.PBANKI_MOUNT === mount && env.PBANKI_DESKTOP_HOME === path.join(root, "home"), "Unexpected folders in the environment");
const snapshotBefore = reader.snapshot;
fs.renameSync(mount, away);

let server = null;
let url = null;
let port = null;
let browser = null;
let page = null;
const pageErrors = [];
const serverErrors = [];

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.on("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port: found } = probe.address();
      probe.close(() => resolve(found));
    });
  });
}

function startServer() {
  return new Promise((resolve, reject) => {
    const log = fs.openSync(serverLog, "a");
    const child = spawn("python3", ["-u", path.join(REPO, "desktop/run.py"), "--no-open", "--port", String(port)], {
      env,
      stdio: ["ignore", "pipe", log],
    });
    server = child;
    child.exited = new Promise((done) => child.on("exit", done));
    let text = "";
    const timer = setTimeout(() => reject(new Failed(`The server didn't start: ${text}`)), 20000);
    child.stdout.on("data", (chunk) => {
      fs.appendFileSync(serverLog, chunk);
      text += chunk;
      const found = text.match(/running at (http:\/\/\S+?)\.? /);
      if (found) {
        clearTimeout(timer);
        const address = found[1];
        if (url && address !== url) reject(new Failed(`The server moved from ${url} to ${address}`));
        url = address;
        resolve();
      }
    });
    child.on("exit", (code) => {
      if (!url) reject(new Failed(`The server stopped at once, code ${code}: ${text}`));
    });
  });
}

async function killServer() {
  server.kill("SIGKILL");
  await server.exited;
  server = null;
}

function unplug() {
  fs.renameSync(mount, away);
}

function plug() {
  fs.renameSync(away, mount);
}

const readFile = (rel) => {
  const file = path.join(mount, rel);
  return fs.existsSync(file) ? fs.readFileSync(file, "utf-8") : "";
};

const aiRuns = () => (fs.existsSync(aiLog) ? fs.readFileSync(aiLog, "utf-8").trim().split("\n").filter(Boolean).map(JSON.parse) : []);

// Steps and screenshots

let stepNumber = 0;
let phaseName = "";

async function shot(name) {
  await page.screenshot({ path: path.join(SHOTS, `${String(stepNumber).padStart(2, "0")}-${name}.png`) });
}

async function step(title, run) {
  stepNumber += 1;
  console.log(`  ${String(stepNumber).padStart(2)}. ${title}`);
  await run();
  await shot(title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 50));
}

function phase(title) {
  phaseName = title;
  console.log(`\n${title}`);
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function until(check, message, ms = 15000) {
  const deadline = Date.now() + ms;
  let last;
  for (;;) {
    try {
      last = await check();
      if (last) return last;
    } catch (error) {
      if (error instanceof Failed) throw error;
      last = error.message;
    }
    if (Date.now() > deadline) throw new Failed(`${message} (last: ${JSON.stringify(last)})`);
    await sleep(100);
  }
}

const textOf = (selector) => page.locator(selector).first().innerText();
const seeH1 = (text) => page.locator("h1.h1", { hasText: text }).waitFor();
const button = (name) => page.getByRole("button", { name, exact: true });

// Screens as data

function readBar() {
  return page.evaluate(() => {
    const unplug = [...document.querySelectorAll(".unplug")].map((el) => el.textContent);
    return { unplug, chip: document.querySelector(".chip")?.textContent ?? "" };
  });
}

// What the Highlights screen shows of the highlight that's open.
async function readHighlight() {
  return page.evaluate(() => {
    const q = (selector) => document.querySelector(selector);
    const crumb = q(".crumbt").textContent;
    const parts = crumb.match(/^(.*?)(?: · p\. (\d+))? · (\d+) of (\d+)$/);
    return {
      crumb,
      chapter: parts[1],
      page: parts[2] === undefined ? null : Number(parts[2]),
      position: Number(parts[3]) - 1,
      mark: q(".pass mark.mk").textContent,
      note: q(".note span:last-child")?.textContent ?? null,
      book: q(".bookt").textContent,
      author: q(".booka").textContent.replace(/( · )?change book$/, ""),
      banner: [...document.querySelectorAll(".col > .banner")].map((el) => el.textContent),
      error: q(".cards .errb")?.textContent ?? null,
      cards: [...document.querySelectorAll(".cards .card")].map((card) => ({
        id: card.querySelector('[id^="f-"]')?.id.slice(2) ?? null,
        kind: card.querySelector(".ckind").textContent,
        src: card.querySelector(".chd > span:nth-child(2)").textContent,
        front: card.querySelector(".ta.f, .ta.c").value,
        back: card.querySelector(".ta.b")?.value ?? "",
      })),
    };
  });
}

function readConnection() {
  return page.evaluate(() => {
    const q = (selector) => document.querySelector(selector);
    return {
      crumb: q(".crumbt").textContent,
      title: q(".mtitle").textContent,
      front: q(".card .ta.f").value,
      back: q(".card .ta.b").value,
      sources: [...document.querySelectorAll(".eink [style*='gap:2px'], .eink [style*='gap: 2px']")].map((el) => ({
        where: el.children[0].textContent,
        mark: el.children[1].textContent.replace(/^“|”$/g, ""),
      })),
      links: [...document.querySelectorAll(".srcs .src")].map((el) => el.querySelector(".mkl").textContent),
    };
  });
}

function readIdea() {
  return page.evaluate(() => {
    const q = (selector) => document.querySelector(selector);
    return {
      chapter: q(".crumbt").textContent.replace(/^Key idea · /, "").replace(/ · Not in your highlights$/, ""),
      title: q(".mtitle").textContent,
      quote: q(".ideaq")?.textContent ?? "",
      front: q(".card .ta.f").value,
      back: q(".card .ta.b").value,
    };
  });
}

function readSave() {
  return page.evaluate(() => {
    const q = (selector) => document.querySelector(selector);
    return {
      count: Number(q(".bignum").textContent),
      noun: q(".bignum").nextElementSibling.textContent,
      groups: [...document.querySelectorAll(".saveg > div")].filter((g) => g.querySelector(".sgh")).map((g) => ({
        head: g.querySelector(".sgh span").textContent,
        where: g.querySelector(".sgm").textContent,
        rows: [...g.querySelectorAll(".sr:not(.sremp)")].map((row) => ({
          // A card still to be saved is editable, one already sent is plain text
          front: row.querySelector(".srq")?.textContent ?? row.querySelector("textarea").value,
          back: row.querySelector(".srq") ? row.children[1].lastElementChild.textContent : row.querySelectorAll("textarea")[1].value,
          need: !!row.querySelector(".srneed, .ta.need"),
        })),
      })),
      steps: [...document.querySelectorAll(".step2")].map((el) => el.children[1].textContent),
      left: q(".cbx").nextElementSibling.textContent,
      errors: [...document.querySelectorAll(".saver .errb")].map((el) => el.textContent),
      waiting: q(".waitb")?.textContent ?? null,
      button: q(".saver .btn.pri")?.textContent ?? null,
      disabled: q(".saver .btn.pri")?.disabled ?? null,
      checked: q(".cbx input").checked,
    };
  });
}

// A book being worked through

class Book {
  constructor(name, title) {
    this.name = name;
    this.title = title;
    this.device = reader.books[name];
    this.annotations = reader.annotations[name];
    this.suggested = {}; // card id: the AI's text as first shown, or after a rewrite
    this.cards = {}; // highlight index: cards as last shown
    this.info = {}; // highlight index: what the screen last showed about it
    this.connections = {}; // title: as last shown
    this.ideas = {}; // title: as last shown
  }

  // Remembers the AI's text the first time a card is on screen.
  note(index, shown) {
    for (const card of shown.cards) {
      if (card.id && card.src.startsWith("AI") && !(card.id in this.suggested)) {
        this.suggested[card.id] = { front: card.front, back: card.back };
      }
    }
    this.cards[index] = shown.cards;
    this.info[index] = shown;
    return shown;
  }

  async read(index) {
    return this.note(index, await readHighlight());
  }

  async open(index) {
    await showAll();
    const row = page.locator(".navlist .hl").nth(index);
    await row.click();
    const total = await page.locator(".navlist .hl").count();
    await until(async () => (await textOf(".crumbt")).endsWith(` · ${index + 1} of ${total}`), `Highlight ${index + 1} didn't open`);
    await until(async () => !(await row.innerText()).includes("suggested"), `Highlight ${index + 1} didn't get marked opened`);
    return this.read(index);
  }

  source(index) {
    return this.annotations[index];
  }

  key(index) {
    const a = this.source(index);
    return `${a.datetime}|${a.pos0}`;
  }

  // The highlight text on screen, against what KOReader wrote.
  checkMark(index) {
    const shown = this.info[index];
    eq(squeeze(shown.mark), squeeze(this.source(index).text), `Highlight ${index + 1}'s text on screen`);
    eq(shown.note, this.source(index).note ?? null, `Highlight ${index + 1}'s note on screen`);
  }

  // Saved highlight cards with a filled front, as on screen.
  saved(index) {
    return this.cards[index].filter((card) => card.front.trim() !== "");
  }

  highlightLine(index, card, batch) {
    const shown = this.info[index];
    const line = { front: card.front, back: card.back, quote: this.source(index).text, book: shown.book, author: shown.author, chapter: shown.chapter };
    if (shown.page !== null) line.page = shown.page;
    return JSON.stringify({ ...line, file: this.device, position: this.source(index).pos0, batch });
  }

  connectionLine(item, batch) {
    const shown = this.info[Object.keys(this.info)[0]];
    const quote = item.sources.map((s) => `${s.where}: "${squeeze(s.mark)}"`).join("\n");
    const chapter = item.crumb.replace(/^Connection · /, "").replace(/ · \d+ highlights?$/, "");
    return JSON.stringify({ front: item.front, back: item.back, quote, book: shown.book, author: shown.author, chapter, file: this.device, position: "", batch });
  }

  ideaLine(item, batch) {
    const shown = this.info[Object.keys(this.info)[0]];
    return JSON.stringify({ front: item.front, back: item.back, quote: "", book: shown.book, author: shown.author, chapter: item.chapter, file: this.device, position: "", batch });
  }

  reviewedLine(index, batch) {
    const a = this.source(index);
    const cards = (this.cards[index] ?? [])
      .filter((card) => card.front.trim() !== "" && card.kind !== "Definition")
      .map((card) => {
        const entry = { front: card.front, back: card.back };
        if (this.suggested[card.id]) entry.suggested = this.suggested[card.id];
        entry.quote = a.text;
        if (a.note) entry.note = a.note;
        return entry;
      });
    return JSON.stringify({ v: 1, batch, file: this.device, key: this.key(index), cards });
  }

  markedEmpty(index, batch) {
    return JSON.stringify({ v: 1, batch, file: this.device, key: this.key(index), cards: [] });
  }
}

// Actions on a highlight

const cardAt = (index) => page.locator(".cards .card").nth(index);

// Types into a saved field and waits for the save. `kind` is cards, connections or ideas.
async function typeInto(field, text, kind = "cards") {
  const saved = page.waitForResponse((r) => r.request().method() === "PATCH" && r.url().includes(`/api/${kind}/`) && r.ok());
  await field.fill(text);
  await saved;
  await field.blur();
}

async function waitForCards(count) {
  await until(async () => (await page.locator(".cards .card").count()) === count, `Expected ${count} cards on screen`);
}

async function removeCard(index) {
  const before = await page.locator(".cards .card").count();
  const answered = page.waitForResponse((r) => r.request().method() === "DELETE" && r.ok());
  await cardAt(index).getByRole("button", { name: "Remove card" }).click();
  await answered;
  await waitForCards(before - 1);
}

async function setCount(wanted) {
  const panel = askPanel();
  for (let i = 0; i < 12; i += 1) {
    const now = Number(await panel.locator(".stepv").innerText());
    if (now === wanted) return;
    const settled = page.waitForResponse((r) => r.url().endsWith("/api/settings") && r.request().method() === "PATCH");
    await panel.locator(".stepb").nth(now > wanted ? 0 : 1).click();
    await settled;
  }
  throw new Failed(`The count didn't reach ${wanted}`);
}

const askPanel = () => page.locator("section.comp", { hasText: "Ask the AI for more" });

async function setKind(kind) {
  const settled = page.waitForResponse((r) => r.url().endsWith("/api/settings") && r.request().method() === "PATCH");
  await askPanel().locator(".opt", { hasText: "Kind" }).locator("select").selectOption(kind);
  await settled;
}

async function suggest(label) {
  const go = askPanel().getByRole("button", { name: label, exact: true });
  await go.click();
}

async function waitAskFree() {
  await until(() => askPanel().locator("button.go").isEnabled(), "The AI panel stayed busy", 30000);
}

async function selectInPassage(text) {
  await page.evaluate((wanted) => {
    const node = document.querySelector(".pass mark.mk").firstChild;
    const start = node.data.indexOf(wanted);
    if (start < 0) throw new Error(`"${wanted}" isn't in the highlight`);
    const range = document.createRange();
    range.setStart(node, start);
    range.setEnd(node, start + wanted.length);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }, text);
}

// Double-clicks one word in the passage with the real mouse.
async function doubleClickWord(word) {
  const box = await page.evaluate((wanted) => {
    const node = document.querySelector(".pass mark.mk").firstChild;
    const start = node.data.indexOf(wanted);
    const range = document.createRange();
    range.setStart(node, start);
    range.setEnd(node, start + wanted.length);
    const { x, y, width, height } = range.getBoundingClientRect();
    return { x: x + width / 2, y: y + height / 2 };
  }, word);
  await page.mouse.dblclick(box.x, box.y);
}

// Steps every book shares

async function pickBook(book) {
  await seeH1("Pick a book");
  const aiBefore = aiRuns().length;
  const row = page.locator("button.bk", { hasText: book.title });
  await row.click();
  await seeH1("Suggest cards");
  await page.waitForTimeout(500);
  eq(aiRuns().length, aiBefore, "Opening a book starts nothing");
  await page.locator(".rowg .btn.pri").click();
}

// The whole run shown ticked off, all rows, then on to Highlights.
async function waitForRunThenReview(rows) {
  await until(
    async () => {
      const shown = await page.locator(".chk").evaluateAll((els) => els.map((el) => [el.querySelector(".chkg").textContent, el.querySelector(".chkt").textContent]));
      return shown.length === rows.length && shown.every(([glyph]) => glyph === "✓") && JSON.stringify(shown.map((r) => r[1])) === JSON.stringify(rows);
    },
    `The run should finish with rows ${rows}`,
    60000,
  );
  await button(/^Continue/).click();
  await page.locator(".crumbt").waitFor();
}

async function goTab(name) {
  await page.locator(".top .seg .sg", { hasText: name }).click();
}

// `lines` are the file's lines added after `from`, compared in order with `expected(batch)`.
function checkAdded(rel, from, expected, what) {
  const now = readFile(rel);
  ok(now.startsWith(from), `${rel} no longer starts with what it held before ${what}`);
  const added = now.slice(from.length);
  ok(added === "" || added.endsWith("\n"), `${rel} should end with a newline`);
  const lines = added.split("\n").slice(0, -1);
  const batch = lines.length ? JSON.parse(lines[0]).batch : null;
  ok(batch, `${rel} got no lines from ${what}`);
  const wanted = expected(batch);
  for (let i = 0; i < Math.max(lines.length, wanted.length); i += 1) {
    eq(lines[i], wanted[i], `${rel}, line ${i + 1} added by ${what}`);
  }
  return batch;
}

// Expected Save-screen group for a highlight, from the highlight screen.
// The list starts on To do. Tests count rows through the whole book.
async function showAll() {
  await page.locator(".navhd .seg .sg", { hasText: /^All / }).click();
}

function highlightGroup(book, index) {
  const rows = book.saved(index).map((card) =>
    card.kind === "Cloze"
      ? { front: card.front, back: card.back, need: false }
      : { front: card.front, back: card.back, need: false },
  );
  return { head: book.source(index).text, rows };
}

function checkGroups(save, expected) {
  // A highlight whose cards were all removed asks what to do with it, and has no rows to compare
  const shown = save.groups.filter((g) => g.rows.length).map((g) => ({ head: g.head, rows: g.rows }));
  eq(shown, expected, "The Save screen's groups, against the screens they came from");
}

// Phase A: the whole flow

async function wholeFlow() {
  phase("A. The whole flow, on the real KOReader book");
  const book = new Book("pride", "Pride and Prejudice");
  const H = { edit: 0, qonly: 1, failed: 2, define: 3, unopened: 4, sourceOnly: 5, skip: 6 };

  await step("Plug in screen while unplugged", async () => {
    await page.goto(url);
    await seeH1("Plug in your PocketBook");
    await page.locator(".watch", { hasText: `Looking for ${mount}` }).waitFor();
    eq(await page.locator(".resume").count(), 0, "Nothing to resume on a first run");
  });

  await step("Reader appears and Reader check runs by itself", async () => {
    plug();
    await seeH1("Your reader is ready");
    const rows = await page.locator(".chk").evaluateAll((els) => els.map((el) => [el.querySelector(".chkg").textContent, el.querySelector(".chkt").textContent, el.querySelector(".chkp").textContent]));
    eq(rows.length, 5, "The Reader check has five rows");
    eq(rows.map((r) => r[0]), ["✓", "✓", "✓", "✓", "✓"], "Every Reader check row is ticked");
    ok(rows[3][1].includes("9") || rows[3][2].includes("9"), `The history row counts 9 books: ${rows[3]}`);
    ok(rows[4][1].includes("plugin"), `The last row is about the plugin: ${rows[4]}`);
    eq(await page.locator(".chip").count(), 0, "The check has no top bar");
  });

  await step("Pick a book lists the history", async () => {
    await button("Continue").click();
    await seeH1("What would you like to do?");
    await page.locator(".choice", { hasText: "Tidy up cards" }).waitFor();
    await page.locator(".choice", { hasText: "Make new cards" }).click();
    await seeH1("Pick a book");
    await until(async () => (await page.locator("button.bk").count()) === 9, "All 9 history books listed");
    const first = page.locator("button.bk").first();
    ok((await first.innerText()).includes("Pride and Prejudice"), "Most recent book first");
    ok((await first.innerText()).includes("7 to do of 7"), `Row counts 7 highlights: ${await first.innerText()}`);
    ok(await page.locator("button.bk", { hasText: "On Card" }).isDisabled(), "A book on the memory card can't be opened");
  });

  await step("Picking the book shows the options, and the button starts the run", async () => {
    await pickBook(book);
    await waitForRunThenReview(["Highlight cards", "Connections", "Key ideas you didn't highlight"]);
    const runs = aiRuns();
    eq(runs.filter((r) => r.stdin.includes("<highlight>")).length, 7, "One AI call per highlight");
    ok(runs.every((r) => r.tool === "claude"), "Claude is the default assistant");
    eq(await page.locator(".navlist .hl").count(), 7, "Seven highlights, not the page bookmark");
  });

  await step("Open a highlight and edit its card", async () => {
    const shown = await book.open(H.edit);
    book.checkMark(H.edit);
    eq(shown.chapter, "Chapter I.", "Chapter on screen");
    eq(shown.page, book.source(H.edit).pageno, "Page on screen");
    eq(shown.cards.length, 2, "The fake AI wrote two cards");
    ok(shown.cards.every((c) => c.src.startsWith("AI")), "They say they came from the AI");
    await typeInto(cardAt(0).locator(".ta.f"), "What must a rich single man be in want of?");
    await until(async () => (await book.read(H.edit)).cards[0].src === "AI, edited", "An edited card says so");
    const suggestion = book.suggested[book.cards[H.edit][0].id];
    ok(suggestion.front !== book.cards[H.edit][0].front, "The AI's own text is still remembered");
  });

  await step("Preview a card on the reader", async () => {
    await cardAt(0).getByRole("button", { name: "Preview" }).click();
    const labels = await cardAt(0).locator(".prevs .lab").allInnerTexts();
    eq(labels.map((l) => l.toLowerCase()), ["question", "answer", "context"], "The preview's three pages");
    ok((await cardAt(0).locator(".prevs").innerText()).includes("What must a rich single man be in want of?"), "The preview shows the edit");
    await cardAt(0).getByRole("button", { name: "Preview" }).click();
  });

  await step("Write a card by hand, and leave a blank one", async () => {
    await button("+ Question").click();
    await waitForCards(3);
    await until(() => cardAt(2).locator(".ta.f").evaluate((el) => el === document.activeElement), "The new card gets the cursor");
    await typeInto(cardAt(2).locator(".ta.f"), "Who is said to want a wife?");
    await typeInto(cardAt(2).locator(".ta.b"), "A single man with a good fortune.");
    await button("+ Question").click();
    await waitForCards(4);
    const shown = await book.read(H.edit);
    eq(shown.cards[2].src, "You", "A hand-written card says You");
    eq(shown.cards[3].front, "", "The blank card is empty");
  });

  await step("Make a cloze card with Hide selected words", async () => {
    await button("+ Cloze").click();
    await waitForCards(5);
    const card = cardAt(4);
    const field = card.locator(".ta.c");
    await typeInto(field, "A single man of large fortune must want a wife.");
    await field.evaluate((el) => {
      el.focus();
      el.setSelectionRange(16, 29);
    });
    const saved = page.waitForResponse((r) => r.request().method() === "PATCH" && r.url().includes("/api/cards/") && r.ok());
    await card.getByRole("button", { name: "Hide selected words" }).click();
    await saved;
    eq(await field.inputValue(), "A single man of {{c1::large fortune}} must want a wife.", "The words are marked in the field");
    await until(async () => (await card.locator(".clprev").innerText()) === "A single man of [...] must want a wife.", "The cloze preview hides the words");
    await field.blur();
    await book.read(H.edit);
  });

  await step("Press J and K to move between highlights", async () => {
    await page.locator(".pass").click({ position: { x: 2, y: 2 } });
    await page.keyboard.press("j");
    await until(async () => (await textOf(".crumbt")).includes("2 of 7"), "J moves to the next highlight");
    await page.keyboard.press("k");
    await until(async () => (await textOf(".crumbt")).includes("1 of 7"), "K moves back");
  });

  await step("Ask for questions only and answer none yet", async () => {
    await book.open(H.qonly);
    book.checkMark(H.qonly);
    const before = book.cards[H.qonly].length;
    await setCount(2);
    await setKind("qonly");
    await suggest("Suggest 2 cards");
    await waitForCards(before + 2);
    await waitAskFree();
    const shown = await book.read(H.qonly);
    const asked = shown.cards.slice(before);
    eq(asked.map((c) => [c.kind, c.back]), [["Question only", ""], ["Question only", ""]], "Two questions without answers");
    ok(await cardAt(before).locator(".ta.b").getAttribute("placeholder") === "Write the answer yourself", "The answer field asks for it");
    await setKind("qa");
  });

  await step("Questions with no answer stay out of the save", async () => {
    await page.locator(".top .btn.pri").click();
    await until(async () => (await readSave()).groups.length > 0, "The Save screen has groups");
    let save = await readSave();
    eq(save.errors.length, 0, "No error about the answers");
    eq(save.groups.flatMap((g) => g.rows).filter((r) => r.need).length, 0, "No row lacks an answer");
    ok((await page.locator(".saver").innerText()).includes("2 cards with no answer are not included."), "It counts what was left out");
    await button("Back to editing").click();
    await page.locator(".crumbt").waitFor();
    await book.open(H.qonly);
    await until(async () => (await textOf(".crumbt")).includes("2 of 7"), "The highlight opens");
    const shown = await book.read(H.qonly);
    const n = shown.cards.length;
    await typeInto(cardAt(n - 2).locator(".ta.b"), "He is the father of the family.");
    eq((await book.read(H.qonly)).cards[n - 2].src, shown.cards[n - 2].src, "Answering a blank question isn't an edit");
    await page.locator(".top .btn.pri").click();
    await until(async () => (await page.locator(".saver").innerText()).includes("1 card with no answer is not included."), "One answer still missing");
    await button("Back to editing").click();
    await page.locator(".crumbt").waitFor();
    await book.open(H.qonly);
    await typeInto(cardAt(n - 1).locator(".ta.b"), "Mr. Bennet, who teases his wife.");
    await book.read(H.qonly);
  });

  await step("A failed AI reply shows a plain sentence and keeps the cards", async () => {
    const shown = await book.open(H.failed);
    book.checkMark(H.failed);
    ok(shown.note, "This highlight has a note");
    await page.locator(".note", { hasText: shown.note }).waitFor();
    const before = shown.cards.length;
    await askPanel().locator("textarea.hint").fill("[[fake:malformed]]");
    await suggest("Suggest 2 cards");
    const error = await until(async () => (await readHighlight()).error, "The failure shows on the highlight", 20000);
    await waitAskFree();
    ok(!/traceback|exception|\{not json/i.test(error), `The failure reads as a sentence: ${error}`);
    eq((await book.read(H.failed)).cards.length, before, "No cards arrive from a bad reply, and none are lost");
  });

  await step("Asking again clears the failure", async () => {
    const before = book.cards[H.failed].length;
    await askPanel().locator("textarea.hint").fill("");
    await suggest("Suggest 2 cards");
    await waitForCards(before + 2);
    await waitAskFree();
    const shown = await book.read(H.failed);
    eq(shown.error, null, "The failure has gone");
  });

  await step("Remove a card and undo it", async () => {
    const before = await book.read(H.failed);
    await removeCard(1);
    await page.locator(".undo", { hasText: "Card removed" }).waitFor();
    eq((await readHighlight()).cards.map((c) => c.id), before.cards.filter((_, i) => i !== 1).map((c) => c.id), "The card has gone");
    await page.locator(".undo").getByRole("button", { name: "Undo" }).click();
    await waitForCards(before.cards.length);
    eq((await readHighlight()).cards, before.cards, "Undo brings it back as it was");
  });

  await step("A card removed for good is told to the AI", async () => {
    const shown = await book.read(H.failed);
    const gone = shown.cards[shown.cards.length - 1];
    await removeCard(shown.cards.length - 1);
    await book.read(H.failed);
    const runsBefore = aiRuns().length;
    const count = book.cards[H.failed].length;
    await setCount(1);
    await suggest("Suggest 1 card");
    await until(() => aiRuns().length > runsBefore, "The AI was asked");
    await until(async () => (await page.locator(".cards .card").count()) >= count, "The screen settles");
    await waitAskFree();
    ok(aiRuns().at(-1).stdin.includes(gone.front), "The removed card is in the prompt");
    await book.read(H.failed);
    await setCount(2);
  });

  await step("Selecting too many words offers no definition", async () => {
    await book.open(H.define);
    book.checkMark(H.define);
    await selectInPassage("Mrs. Bennet deigned not");
    const define = page.locator(".passft .btn");
    await until(async () => (await define.innerText()) === "Select 1 to 3 words to define them", "Four words is too many");
    ok(await define.isDisabled(), "So the button stays off");
  });

  await step("Double-clicking a word offers its definition cards", async () => {
    await doubleClickWord("Kitty");
    const define = page.locator(".passft .btn");
    await until(async () => (await define.innerText()) === 'Make definition cards for "Kitty"', "A double-clicked word is picked up");
    await page.evaluate(() => window.getSelection().removeAllRanges());
  });

  await step("Make definition cards from two selected words", async () => {
    const before = book.cards[H.define].length;
    await selectInPassage("Mrs. Bennet");
    const define = page.locator(".passft .btn");
    await until(async () => (await define.innerText()) === 'Make definition cards for "Mrs. Bennet"', "Two words are picked up");
    await define.click();
    await waitForCards(before + 2);
    const shown = await book.read(H.define);
    const made = shown.cards.slice(before);
    eq(made.map((c) => c.kind), ["Definition", "Definition"], "Both are definition cards");
    eq(made[0].front, "Define Mrs. Bennet", "One asks for the definition");
    eq(made[1].back, "Mrs. Bennet", "The other asks for the term");
    eq(made[0].back, made[1].front, "They mirror each other");
    await until(async () => (await define.innerText()) === "Select 1 to 3 words to define them", "The button resets");
  });

  await step("Rewrite a card with an instruction", async () => {
    const card = cardAt(0);
    await card.getByRole("button", { name: "Rewrite" }).click();
    await card.locator(".rw input").fill("make it about this week");
    const before = (await readHighlight()).cards[0];
    await card.locator(".rw button").click();
    await until(async () => (await readHighlight()).cards[0].front !== before.front, "The rewrite arrives", 20000);
    await card.locator(".rw").waitFor({ state: "detached" });
    const shown = await book.read(H.define);
    eq(shown.cards[0].src, "AI, rewritten", "A rewritten card says so");
    ok(aiRuns().at(-1).stdin.includes("make it about this week"), "The instruction reached the AI");
    book.suggested[shown.cards[0].id] = { front: shown.cards[0].front, back: shown.cards[0].back };
    await typeInto(card.locator(".ta.b"), "Change something small about this week.");
    await until(async () => (await book.read(H.define)).cards[0].src === "AI, edited", "Editing a rewrite marks it edited");
  });

  await step("Remove every card from one highlight", async () => {
    const shown = await book.open(H.sourceOnly);
    book.checkMark(H.sourceOnly);
    for (let i = shown.cards.length - 1; i >= 0; i -= 1) await removeCard(i);
    await book.read(H.sourceOnly);
    await page.locator(".cards .empty", { hasText: "No cards yet" }).waitFor();
  });

  await step("Skip with X, undo it, then skip with the button", async () => {
    const shown = await book.open(H.skip);
    book.checkMark(H.skip);
    ok(shown.cards.length > 0, "It has suggestions to drop");
    await page.locator(".pass").click({ position: { x: 2, y: 2 } });
    await page.keyboard.press("x");
    await page.locator(".col > .banner", { hasText: "Skipped. It will count as reviewed when you save." }).waitFor();
    await button("Undo skip").click();
    await page.locator(".col > .banner", { hasText: "Skipped." }).waitFor({ state: "detached" });
    await button("Skip").first().click();
    await page.locator(".col > .banner", { hasText: "Skipped." }).waitFor();
    book.skippedCards = shown.cards;
  });

  await step("The run's connections are there", async () => {
    await goTab("Connections");
    await page.locator(".crumbt", { hasText: "Connection ·" }).waitFor();
    eq(await page.locator(".navlist .ml").count(), 4, "Find connections and three from the run");
    const first = await readConnection();
    eq(first.links.map(squeeze), [book.source(0).text, book.source(1).text].map(squeeze), "The first cites highlights 1 and 2");
    book.connections[first.title] = first;
  });

  await step("All-highlights scope changes the count", async () => {
    await page.locator(".navlist .ml", { hasText: "+ Find connections" }).click();
    await page.locator("h2.mtitle", { hasText: "Find connections" }).waitFor();
    const summary = page.locator(".compt").first();
    const counts = () => page.locator(".cbx").evaluateAll((els) => els.map((el) => el.lastElementChild.textContent));
    await until(async () => (await summary.innerText()) === "Reading 6 highlights from 4 chapters", "Pending leaves out the skipped highlight");
    eq(await counts(), ["3 highlights", "2 highlights", "1 highlight", "0 highlights"], "Each chapter's pending count");
    await button("All, including reviewed").click();
    await until(async () => (await summary.innerText()) === "Reading 7 highlights from 4 chapters", "All counts it");
    eq(await counts(), ["3 highlights", "2 highlights", "1 highlight", "1 highlight"], "Each chapter's full count");
    await page.locator(".cbx").first().locator("input").uncheck();
    await until(async () => (await summary.innerText()) === "Reading 4 highlights from 3 chapters", "Unticking a chapter takes its highlights out");
  });

  await step("Find connections reads what the screen said", async () => {
    const runsBefore = aiRuns().length;
    await button("Find 2 connections").click();
    await until(async () => (await page.locator(".navlist .ml").count()) > 4, "New connections arrive", 20000);
    await page.locator(".crumbt", { hasText: "Connection ·" }).waitFor();
    const asked = aiRuns().slice(runsBefore);
    eq(asked.length, 1, "One AI call");
    const sent = asked[0].stdin.match(/<highlights>\n([\s\S]*?)\n<\/highlights>/)[1];
    eq(sent.match(/^\d+\. \[/gm).length, 4, "The AI got the four highlights the screen counted");
    for (const index of [3, 4, 5, 6]) ok(sent.includes(book.source(index).text), `Highlight ${index + 1} was sent`);
    ok(!sent.includes(book.source(0).text), "The unticked chapter wasn't sent");
  });

  await step("Remove a connection, open another and edit it", async () => {
    const listed = await page.locator(".navlist .ml .mlt").allInnerTexts();
    eq(listed.length, 6, "Five connections and the Find row");
    const removing = await readConnection();
    ok(removing.links.map(squeeze).includes(squeeze(book.source(3).text)), "The first new one cites highlight 4");
    await button("Remove").click();
    await until(async () => (await page.locator(".navlist .ml").count()) === 5, "It has gone");
    await until(async () => (await readConnection()).title === book.connections[Object.keys(book.connections)[0]].title, "The list's first one shows, already opened");
    await page.locator(".navlist .ml").nth(4).click();
    await until(async () => squeeze((await readConnection()).links[0]) === squeeze(book.source(4).text), "Highlights 5 and 6 open");
    const source = await readConnection();
    eq(source.links.map(squeeze), [4, 5].map((i) => squeeze(book.source(i).text)), "It cites highlights 5 and 6");
    await typeInto(page.locator(".card .ta.f"), "How do the Bennet girls and the neighbours judge each other?", "connections");
    book.connections[source.title] = await readConnection();
    await until(async () => !(await page.locator(".navlist .ml.on").innerText()).includes("Not opened"), "It counts as opened");
  });

  await step("Key ideas and Not for me", async () => {
    await goTab("Key ideas");
    await page.locator(".crumbt", { hasText: "Key idea ·" }).waitFor();
    const total = await page.locator(".navlist .ml").count();
    ok(total >= 3, `There are at least three key ideas: ${total}`);
    const first = await readIdea();
    ok(first.quote, "It quotes the book");
    await button("Not for me").click();
    await until(async () => (await page.locator(".navlist .ml").count()) === total - 1, "Not for me removes it");
    await until(async () => (await readIdea()).title !== first.title, "The next one opens");
    await typeInto(page.locator(".card .ta.b"), "That a fortune settles what a man wants.", "ideas");
    const kept = await readIdea();
    book.ideas[kept.title] = kept;
    book.removedIdea = first;
    await until(async () => !(await page.locator(".navlist .ml.on").innerText()).includes("Not opened"), "It counts as opened");
    eq(await page.locator(".navlist .ml", { hasText: "Not opened" }).count(), total - 2, "The rest stay unopened");
    book.ideasWaiting = total - 2;
  });

  await step("Companion notes show on a highlight", async () => {
    await goTab("Highlights");
    await page.locator(".crumbt", { hasText: /\d of \d/ }).waitFor();
    await page.locator(".navlist .hl").nth(H.edit).click();
    await page.locator(".cpn").waitFor();
    await page.locator(".cpnh").click();
    await until(async () => (await page.locator(".cpnr").count()) > 0, "The notes open");
    await page.locator(".cpnh").click();
  });

  let save;
  const indexes = [H.edit, H.qonly, H.failed, H.define];

  await step("Save review shows exactly what the screens showed", async () => {
    await page.locator(".top .btn.pri").click();
    await until(async () => (await readSave()).groups.length > 0, "The Save screen has groups");
    save = await readSave();
    for (const index of indexes) book.checkMark(index);
    const connection = Object.values(book.connections);
    eq(connection.length, 2, "Two connections were opened");
    const expected = [
      ...indexes.map((i) => highlightGroup(book, i)),
      ...connection.map((c) => ({ head: `Connection: ${c.title}`, rows: [{ front: c.front, back: c.back, need: false }] })),
      ...Object.values(book.ideas).map((i) => ({ head: `Key idea: ${i.title}`, rows: [{ front: i.front, back: i.back, need: false }] })),
    ];
    checkGroups(save, expected);
    const cards = expected.flatMap((g) => g.rows).length;
    eq(save.count, cards, "The big number counts the rows");
    eq(save.errors, [], "Nothing blocks it");
    eq(save.disabled, false, "The button is on");
    eq(save.button, "Save to reader", "The button's label");
    ok(!expected.flatMap((g) => g.rows).some((r) => r.front === ""), "The blank card isn't listed");
    ok(!expected.flatMap((g) => g.rows).some((r) => r.front === book.removedIdea.front), "The removed key idea isn't listed");
    const topLabel = await page.locator(".saver .bignum").innerText();
    eq(Number(topLabel), cards, "The count");
    ok(save.left.includes("1 highlight will stay to do."), `What stays: ${save.left}`);
    ok(save.left.includes("1 highlight with suggestions you haven't opened"), `The unopened highlight: ${save.left}`);
    ok(save.left.includes(`${book.skippedCards.length} cards on skipped highlights will not be saved`), `Skipped cards: ${save.left}`);
    ok(save.left.includes("2 connections you haven't opened"), `Connections left: ${save.left}`);
  });

  await step("The checkbox really changes the marked count", async () => {
    eq(save.checked, true, "It starts ticked");
    eq(save.steps[2], "Mark 6 highlights as reviewed", "Step 3 with it on");
    await page.locator(".cbx input").uncheck();
    await until(async () => (await readSave()).steps[2] === "Mark 5 highlights as reviewed", "Unticking marks one fewer");
    ok((await readSave()).left.includes("2 highlights will stay to do."), "And leaves one more to do");
    await page.locator(".cbx input").check();
    await until(async () => (await readSave()).steps[2] === "Mark 6 highlights as reviewed", "Ticking marks it again");
    eq((await readSave()).steps.length, 4, "Four steps on the screen");
    ok((await readSave()).steps[1].includes("inbox"), "Step 2 is the inbox");
    ok((await readSave()).steps[3].startsWith("Read back"), "Step 4 is the read back");
  });

  await step("Save lands on the Saved screen", async () => {
    await page.locator(".saver .btn.pri").click();
    await seeH1(`${save.count} cards on your reader`);
    const rows = await page.locator(".chk .chkt").allInnerTexts();
    eq(rows, ["Backed up on this computer", "Added to pbAnki's inbox", "6 highlights noted as reviewed for the plugin", "Read back from the reader"], "The Saved steps");
    ok((await page.locator(".lede").innerText()).includes("KOReader::Pride and Prejudice"), "It names the deck");
  });

  await step("The files hold those lines and nothing else", async () => {
    const connections = Object.values(book.connections);
    const ideas = Object.values(book.ideas);
    const reviewedBefore = originalReviewed();
    const batch = checkAdded(
      INBOX,
      "",
      (id) => [
        ...indexes.flatMap((i) => book.saved(i).map((card) => book.highlightLine(i, card, id))),
        ...connections.map((c) => book.connectionLine(c, id)),
        ...ideas.map((i) => book.ideaLine(i, id)),
      ],
      "the first save",
    );
    const reviewedBatch = checkAdded(
      REVIEWED,
      reviewedBefore,
      (id) => [...indexes.map((i) => book.reviewedLine(i, id)), book.markedEmpty(H.skip, id), book.markedEmpty(H.sourceOnly, id)],
      "the first save",
    );
    eq(batch, reviewedBatch, "Both files carry the same batch");
    const inbox = readFile(INBOX);
    ok(inbox.includes("{{c1::large fortune}}"), "The cloze card is in the inbox with its gap");
    ok(!inbox.trim().split("\n").some((line) => JSON.parse(line).position === book.source(H.unopened).pos0), "The unopened highlight's cards aren't in the inbox");
    ok(!inbox.includes(book.skippedCards[0].front), "A skipped highlight's card isn't in the inbox");
    ok(!inbox.includes(book.removedIdea.front), "The removed key idea isn't in the inbox");
    const lines = readFile(REVIEWED).trim().split("\n").filter((l) => l.startsWith('{"v":1') && l.includes(`"${batch}"`));
    ok(!lines.some((l) => l.includes(book.key(H.unopened))), "The unopened highlight isn't marked");
    const defined = JSON.parse(lines[indexes.indexOf(H.define)]);
    ok(!defined.cards.some((c) => c.front === "Define Mrs. Bennet"), "Definition cards stay out of the card history");
    ok(defined.cards.length > 0, "Its other cards are in the card history");
  });

  await step("Eject", async () => {
    const eject = button("Eject reader");
    await eject.click();
    await page.locator(".btn.pri", { hasText: "Safe to unplug" }).waitFor();
    ok(await page.locator(".btn.pri", { hasText: "Safe to unplug" }).isDisabled(), "It can't be pressed twice");
  });

  await step("The book now shows them as reviewed", async () => {
    await button("Keep working on this one").click();
    await page.locator(".navhd .seg .sg", { hasText: "Reviewed 6" }).waitFor();
    await showAll();
    await page.locator(".navlist .hl").nth(H.edit).click();
    await page.locator(".col > .banner", { hasText: "Reviewed. These are the cards you saved from it." }).waitFor();
    const fronts = await page.locator(".cards .card.ro .ta.f").evaluateAll((els) => els.map((el) => el.value));
    ok(fronts.includes("Who is said to want a wife?"), "Its saved cards show");
    await page.locator(".top .btn.pri").waitFor();
    // Carrying on opened the next highlight to do, which brings its two suggested cards in
    eq(await page.locator(".top .btn.pri").innerText(), "Save 2 cards", "Only the next highlight's cards are left to save");
  });

  return book;
}

// Phase B: unplugged part way

async function unpluggedFlow() {
  phase("B. Reader unplugged part way, server killed, save waits, replug");
  const book = new Book("pending", "Gardens, A Primer");
  const P1 = 2;
  const P2 = 3;
  const P3 = 4;
  let inboxBefore;
  let reviewedBefore;
  let beforeUnplug;
  let connections = [];

  await step("Pick a second book, with two highlights already reviewed", async () => {
    await page.locator(".top .brand").waitFor();
    await page.goto(`${url}/#/books`);
    await pickBook(book);
    await waitForRunThenReview(["Highlight cards", "Connections"]);
    await page.locator(".navhd .seg .sg", { hasText: "To do 3" }).waitFor();
    await showAll();
    eq(await page.locator(".navlist .hl").count(), 5, "Five highlights");
    inboxBefore = readFile(INBOX);
    reviewedBefore = readFile(REVIEWED);
  });

  await step("Edit a card while plugged in", async () => {
    await book.open(P1);
    book.checkMark(P1);
    await typeInto(cardAt(0).locator(".ta.b"), "If they stay cool and dry.");
    await until(async () => (await book.read(P1)).cards[0].src === "AI, edited", "It says edited");
  });

  await step("Unplug: the screen says so and stays open", async () => {
    beforeUnplug = python("snapshot", mount);
    unplug();
    await until(async () => (await readBar()).unplug.some((t) => t.includes("Your reader is unplugged")), "The unplugged strip shows");
    await until(async () => (await readBar()).chip.includes("Reader unplugged"), "The chip says so");
  });

  await step("Keep working: edit, write by hand and ask the AI", async () => {
    const shown = await book.open(P2);
    book.checkMark(P2);
    await typeInto(cardAt(0).locator(".ta.f"), "How long do seeds keep?");
    await until(async () => (await book.read(P2)).cards[0].src === "AI, edited", "It says edited");
    await button("+ Question").click();
    await waitForCards(shown.cards.length + 1);
    const typed = cardAt(shown.cards.length);
    await typeInto(typed.locator(".ta.f"), "Why sow thinly?");
    await typeInto(typed.locator(".ta.b"), "Thinning later is a chore nobody enjoys.");
    await setCount(1);
    await suggest("Suggest 1 card");
    await waitForCards(shown.cards.length + 2);
    await waitAskFree();
    await book.read(P2);
  });

  await step("Remove every card from the highlight the book doesn't contain", async () => {
    const shown = await book.open(P3);
    eq(shown.banner.some((t) => t.includes("couldn't be matched")), true, "It says the text wasn't matched");
    for (let i = shown.cards.length - 1; i >= 0; i -= 1) await removeCard(i);
    await book.read(P3);
  });

  await step("Open both connections and edit one", async () => {
    await goTab("Connections");
    await page.locator(".crumbt", { hasText: "Connection ·" }).waitFor();
    connections = [await readConnection()];
    eq(connections[0].links.map(squeeze), [P1, P2].map((i) => squeeze(book.source(i).text)), "The first cites the Seeds highlights");
    await page.locator(".navlist .ml").nth(2).click();
    await until(async () => (await readConnection()).title !== connections[0].title, "The second opens");
    eq((await readConnection()).links.map(squeeze), [P2, P3].map((i) => squeeze(book.source(i).text)), "The second cites the highlight with no cards");
    await typeInto(page.locator(".card .ta.b"), "Patience, and words the book never printed.", "connections");
    connections[1] = await readConnection();
    await until(async () => !(await page.locator(".navlist .ml.on").innerText()).includes("Not opened"), "It counts as opened");
  });

  await step("Key ideas ask for a Companion on this book", async () => {
    await goTab("Key ideas");
    await page.locator("h2.mtitle", { hasText: "This needs a Book Companion" }).waitFor();
  });

  async function checkNothingLost(what) {
    await goTab("Highlights");
    await page.locator(".crumbt", { hasText: /\d of \d/ }).waitFor();
    for (const index of [P1, P2, P3]) {
      const was = book.cards[index];
      const now = await book.open(index);
      eq(now.cards, was, `Highlight ${index + 1} ${what}`);
    }
    await goTab("Connections");
    await page.locator(".crumbt", { hasText: "Connection ·" }).waitFor();
    await page.locator(".navlist .ml").nth(1).click();
    await until(async () => (await readConnection()).title === connections[0].title, "The first connection opens");
    eq(await readConnection(), connections[0], `The first connection ${what}`);
    await page.locator(".navlist .ml").nth(2).click();
    await until(async () => (await readConnection()).title === connections[1].title, "The second connection opens");
    eq(await readConnection(), connections[1], `The second connection ${what}`);
  }

  await step("Reload while unplugged: nothing lost", async () => {
    await page.reload();
    await page.locator(".top .brand").waitFor();
    await until(async () => (await readBar()).unplug.some((t) => t.includes("Your reader is unplugged")), "The strip is back after the reload");
    await checkNothingLost("after a reload");
  });

  await step("Typing then leaving at once loses nothing", async () => {
    const field = page.locator(".card .ta.b");
    const typed = "Patience, and words the book never once printed.";
    await field.fill(typed);
    await page.reload();
    await page.locator(".top .brand").waitFor();
    await until(async () => (await readConnection()).back === typed, "Text typed just before a reload is kept");
    connections[1] = await readConnection();
    await goTab("Highlights");
    await page.locator(".crumbt", { hasText: /\d of \d/ }).waitFor();
    await book.open(P2);
    const before = book.cards[P2];
    const back = "Thinning later is a chore that nobody at all enjoys.";
    await cardAt(before.length - 2).locator(".ta.b").fill(back);
    await showAll();
    await page.locator(".navlist .hl").nth(P1).click();
    await page.locator(".crumbt", { hasText: `${P1 + 1} of 5` }).waitFor();
    eq((await book.open(P2)).cards[before.length - 2].back, back, "Text typed just before moving on is kept");
    await goTab("Connections");
    await page.locator(".crumbt", { hasText: "Connection ·" }).waitFor();
    await until(async () => (await readConnection()).title === connections[1].title, "The connection picked before is still picked");
  });

  await step("Kill the server: the page says so", async () => {
    await killServer();
    await until(async () => (await readBar()).unplug.some((t) => t.includes("pbAnki stopped answering")), "The page notices the server has gone");
    ok((await readBar()).unplug.join(" ").includes("Your work is safe"), "It says the work is safe");
  });

  await step("Type while the server is down: the text stays on screen", async () => {
    const field = page.locator(".card .ta.f");
    await field.fill("Which words does the book never print?");
    await field.blur();
    await until(() => page.evaluate(() => (localStorage.getItem("pbanki-unsent-edits") || "").includes("never print")), "The browser keeps a copy of what was typed");
    eq(await field.inputValue(), "Which words does the book never print?", "The typed text stays");
  });

  await step("Restart the server: the typed text saves by itself", async () => {
    await startServer();
    await until(async () => !(await readBar()).unplug.some((t) => t.includes("stopped answering")), "The page finds the server again", 30000);
    await until(async () => (await readConnection()).front === "Which words does the book never print?", "The text reaches the server without touching the field", 15000);
    connections[1] = await readConnection();
    await until(async () => !(await readBar()).unplug.some((t) => t.includes("hasn't reached")), "The page stops saying it hasn't reached pbAnki");
  });

  await step("Reload on the new server: nothing lost", async () => {
    await page.reload();
    await page.locator(".top .brand").waitFor();
    await until(async () => (await readBar()).unplug.some((t) => t.includes("Your reader is unplugged")), "Still unplugged");
    await checkNothingLost("after the server was killed");
    eq(await page.locator(".resume").count(), 0, "No resume box in a book");
  });

  let save;

  await step("Save review while unplugged: unticked marks one fewer", async () => {
    await page.locator(".top .btn.pri").click();
    await until(async () => (await readSave()).groups.length > 0, "The Save screen has groups");
    save = await readSave();
    checkGroups(save, [
      highlightGroup(book, P1),
      highlightGroup(book, P2),
      ...connections.map((c) => ({ head: `Connection: ${c.title}`, rows: [{ front: c.front, back: c.back, need: false }] })),
    ]);
    eq(save.button, "Back up and wait for reader", "The button says what happens");
    ok(await page.locator(".saver", { hasText: "Your reader is unplugged. Saving will back up your cards" }).isVisible(), "The note says so too");
    eq(save.steps[2], "Mark 3 highlights as reviewed", "Three marked with the box ticked");
    await page.locator(".cbx input").uncheck();
    await until(async () => (await readSave()).steps[2] === "Mark 2 highlights as reviewed", "Unticking marks one fewer");
  });

  await step("Save waits for the reader and the reader's folder stays untouched", async () => {
    await page.locator(".saver .btn.pri").click();
    await until(async () => (await readSave()).waiting, "The waiting box shows");
    const now = await readSave();
    ok(now.waiting.includes("Plug in your PocketBook to finish"), `It asks for the reader: ${now.waiting}`);
    ok(now.waiting.includes("Step 1 is done"), `It says step 1 is done: ${now.waiting}`);
    eq(now.count, save.count, "The count stays on screen");
    eq(now.groups.map((g) => g.head), save.groups.map((g) => g.head), "The groups stay on screen");
    eq(python("snapshot", away), beforeUnplug, "Nothing was written while it was away");
  });

  await step("Kill the server again while waiting: the save is still there", async () => {
    await killServer();
    await startServer();
    await page.reload();
    await until(async () => (await readSave()).waiting, "Still waiting after a restart");
    const now = await readSave();
    eq(now.count, save.count, "Same count");
    eq(now.groups.map((g) => g.head), save.groups.map((g) => g.head), "Same groups");
    await sleep(2500);
    eq(python("snapshot", away), beforeUnplug, "Nothing written yet");
  });

  await step("Replug: the save finishes by itself", async () => {
    plug();
    await seeH1(`${save.count} cards on your reader`);
    const rows = await page.locator(".chk .chkt").allInnerTexts();
    ok(rows.includes("2 highlights noted as reviewed for the plugin"), `The marked count: ${rows}`);
  });

  await step("The files hold those lines, once, after the earlier ones", async () => {
    const lines = (id) => [
      ...[P1, P2].flatMap((i) => book.saved(i).map((card) => book.highlightLine(i, card, id))),
      ...connections.map((c) => book.connectionLine(c, id)),
    ];
    const batch = checkAdded(INBOX, inboxBefore, lines, "the save that waited");
    eq(checkAdded(REVIEWED, reviewedBefore, (id) => [P1, P2].map((i) => book.reviewedLine(i, id)), "the save that waited"), batch, "Both files carry the same batch");
    ok(!readFile(REVIEWED).includes(book.key(P3).replace(/"/g, '\\"') + '","cards":[]'), "The unticked source isn't marked");
  });

  await step("The unticked source stays to do", async () => {
    await button("Keep working on this one").click();
    await page.locator(".navhd .seg .sg", { hasText: "To do 1" }).waitFor();
    await showAll();
    await page.locator(".navlist .hl").nth(P3).click();
    await page.locator(".crumbt", { hasText: "5 of 5" }).waitFor();
    eq(await page.locator(".col > .banner", { hasText: "Reviewed" }).count(), 0, "It isn't reviewed");
  });
}

// Phase C: a failed save

async function failedSaveFlow() {
  phase("C. A failed save with Try again, and connections over reviewed highlights");
  const book = new Book("mostly_done", "How to Spend a Day");
  const LAST = 3;
  let inboxBefore;
  let reviewedBefore;
  const connections = [];
  let idea = null;

  await step("Pick a book with three of four highlights reviewed", async () => {
    await page.goto(`${url}/#/books`);
    await pickBook(book);
    await until(async () => (await page.locator(".chk", { hasText: "Done · 1 of 1 highlights" }).count()) === 1, "The run covers the one pending highlight", 30000);
    await until(() => button(/^Continue/).isVisible(), "The run finishes", 30000);
    await button(/^Continue/).click();
    await page.locator(".crumbt").waitFor();
    await page.locator(".navhd .seg .sg", { hasText: "To do 1" }).waitFor();
    inboxBefore = readFile(INBOX);
    reviewedBefore = readFile(REVIEWED);
  });

  await step("Edit the one card to do", async () => {
    await book.open(LAST);
    book.checkMark(LAST);
    await typeInto(cardAt(0).locator(".ta.f"), "How do most people treat their working hours?");
    await until(async () => (await book.read(LAST)).cards[0].src === "AI, edited", "It says edited");
  });

  await step("All-highlights scope turns Find on", async () => {
    await goTab("Connections");
    await page.locator(".navlist .ml", { hasText: "+ Find connections" }).click();
    await page.locator("h2.mtitle", { hasText: "Find connections" }).waitFor();
    const summary = page.locator(".compt").first();
    const find = page.locator("button.go");
    await until(async () => (await summary.innerText()) === "Reading 1 highlight from 3 chapters", "Pending counts only the one to do");
    ok(await find.isDisabled(), "One highlight can't make a connection");
    await button("All, including reviewed").click();
    await until(async () => (await summary.innerText()) === "Reading 4 highlights from 3 chapters", "All counts every highlight");
    ok(await find.isEnabled(), "Now it can");
    await find.click();
    await page.locator(".crumbt", { hasText: "Connection ·" }).waitFor({ timeout: 20000 });
    connections.push(await readConnection());
    await until(async () => !(await page.locator(".navlist .ml.on").innerText()).includes("Not opened"), "It counts as opened");
  });

  await step("Open the key idea from the Companion", async () => {
    await goTab("Key ideas");
    const has = await until(async () => (await page.locator(".crumbt", { hasText: "Key idea ·" }).count()) > 0 || (await page.locator(".navlist .empty").count()) > 0, "The list settles");
    ok(has, "Key ideas settled");
    if (await page.locator(".crumbt", { hasText: "Key idea ·" }).count()) {
      idea = await readIdea();
      await until(async () => !(await page.locator(".navlist .ml.on").innerText()).includes("Not opened"), "It counts as opened");
    }
  });

  const pbankiDir = path.join(mount, "applications/pbanki");
  const lockFiles = () => {
    for (const rel of [INBOX, REVIEWED]) fs.chmodSync(path.join(mount, rel), 0o444);
    fs.chmodSync(pbankiDir, 0o555);
  };
  const unlockFiles = () => {
    fs.chmodSync(pbankiDir, 0o755);
    for (const rel of [INBOX, REVIEWED]) fs.chmodSync(path.join(mount, rel), 0o644);
  };
  book.unlock = unlockFiles;

  let save;

  await step("Save fails on a reader that can't be written", async () => {
    await goTab("Highlights");
    await page.locator(".crumbt", { hasText: /\d of \d/ }).waitFor();
    await book.read(LAST);
    await page.locator(".top .btn.pri").click();
    await until(async () => (await readSave()).groups.length > 0, "The Save screen has groups");
    save = await readSave();
    checkGroups(save, [
      highlightGroup(book, LAST),
      ...connections.map((c) => ({ head: `Connection: ${c.title}`, rows: [{ front: c.front, back: c.back, need: false }] })),
      ...(idea ? [{ head: `Key idea: ${idea.title}`, rows: [{ front: idea.front, back: idea.back, need: false }] }] : []),
    ]);
    lockFiles();
    cleanups.push(unlockFiles);
    await page.locator(".saver .btn.pri").click();
    await until(async () => (await readSave()).errors.length > 0, "The failure shows");
    const now = await readSave();
    ok(now.errors[0].includes("Could not write to the reader"), `It says what failed: ${now.errors[0]}`);
    ok(now.errors[0].includes("Your cards are safe on this computer."), `It says the cards are safe: ${now.errors[0]}`);
    eq(now.button, "Try again", "The button becomes Try again");
    eq(now.count, save.count, "The count stays");
    eq(readFile(INBOX), inboxBefore, "The inbox is untouched");
    eq(readFile(REVIEWED), reviewedBefore, "reviewed.jsonl is untouched");
  });

  await step("Try again while it still can't be written keeps it failed", async () => {
    await button("Try again").click();
    await until(() => button("Try again").isEnabled(), "The attempt finishes");
    const now = await readSave();
    ok(now.errors[0].includes("Could not write to the reader"), "The same failure shows");
    await page.reload();
    await until(async () => (await readSave()).errors.length > 0, "The failure survives a reload");
    eq((await readSave()).count, save.count, "So do the cards");
  });

  await step("Try again once the reader can be written goes through", async () => {
    unlockFiles();
    cleanups.pop();
    await button("Try again").click();
    await seeH1(`${save.count} ${save.count === 1 ? "card" : "cards"} on your reader`);
  });

  await step("The files hold those lines, once", async () => {
    const lines = (id) => [
      ...book.saved(LAST).map((card) => book.highlightLine(LAST, card, id)),
      ...connections.map((c) => book.connectionLine(c, id)),
      ...(idea ? [book.ideaLine(idea, id)] : []),
    ];
    const batch = checkAdded(INBOX, inboxBefore, lines, "the save that failed first");
    const line = JSON.parse(readFile(INBOX).trim().split("\n").at(-1));
    ok(line.author.includes("Arnold Bennett"), "The author is on the line");
    eq(checkAdded(REVIEWED, reviewedBefore, (id) => [book.reviewedLine(LAST, id)], "the save that failed first"), batch, "Both files carry the same batch");
    eq(book.source(LAST).note, "Exactly my week.", "This highlight has a note");
    ok(JSON.parse(readFile(REVIEWED).trim().split("\n").at(-1)).cards.every((c) => c.note === "Exactly my week."), "The note is in the card history");
  });
}

function originalReviewed() {
  return fs.readFileSync(path.join(root, "reviewed.original.jsonl"), "utf-8");
}

// Final checks on the whole reader

function checkReaderUnchangedElsewhere() {
  const after = python("snapshot", mount);
  const allowed = new Set([INBOX, REVIEWED]);
  const changed = [];
  for (const name of new Set([...Object.keys(snapshotBefore), ...Object.keys(after)])) {
    if (snapshotBefore[name] !== after[name] && !allowed.has(name)) changed.push(name);
  }
  eq(changed, [], "Files on the reader that changed apart from the two allowed");
  ok(INBOX in after, "inbox.jsonl exists");
  ok(after[REVIEWED] !== snapshotBefore[REVIEWED], "reviewed.jsonl changed");
  ok(!fs.existsSync(away), "The fake reader is back where it belongs");
}

function keepOutput(pride) {
  fs.rmSync(OUTPUT, { recursive: true, force: true });
  fs.mkdirSync(path.join(OUTPUT, "sidecar"), { recursive: true });
  fs.copyFileSync(path.join(mount, INBOX), path.join(OUTPUT, "inbox.jsonl"));
  fs.copyFileSync(path.join(mount, REVIEWED), path.join(OUTPUT, "reviewed.jsonl"));
  fs.copyFileSync(path.join(mount, "English/book.sdr/metadata.epub.lua"), path.join(OUTPUT, "sidecar/metadata.epub.lua"));
  fs.copyFileSync(path.join(mount, "English/book.epub"), path.join(OUTPUT, "book.epub"));
  const marked = readFile(REVIEWED).split("\n").filter((line) => line.includes(`"file":"${pride.device}"`)).map(JSON.parse);
  fs.writeFileSync(
    path.join(OUTPUT, "manifest.json"),
    `${JSON.stringify(
      {
        book: "book.epub",
        book_device_path: pride.device,
        sidecar: "sidecar/metadata.epub.lua",
        sidecar_device_path: "/mnt/ext1/English/book.sdr/metadata.epub.lua",
        other_books_in_the_files: Object.values(reader.books).filter((p) => readFile(INBOX).includes(`"file":"${p}"`) && p !== pride.device),
        marked_for_this_book: marked,
      },
      null,
      2,
    )}\n`,
  );
}

const cleanups = [];

async function finish() {
  for (const cleanup of cleanups) {
    try {
      cleanup();
    } catch {
      // Keep cleaning
    }
  }
  if (browser) await browser.close().catch(() => {});
  if (server) await killServer().catch(() => {});
  if (process.env.E2E_KEEP) console.log(`Temporary folder kept: ${root}`);
  else fs.rmSync(root, { recursive: true, force: true });
}

async function main() {
  fs.rmSync(SHOTS, { recursive: true, force: true });
  fs.mkdirSync(SHOTS, { recursive: true });
  fs.writeFileSync(path.join(root, "reviewed.original.jsonl"), fs.readFileSync(path.join(away, REVIEWED)));

  port = await freePort();
  await startServer();
  browser = await chromium.launch({ headless: true });
  page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(15000);
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 500) serverErrors.push(`${response.status()} ${response.url()}`);
  });

  const pride = await wholeFlow();
  await unpluggedFlow();
  await failedSaveFlow();

  phase("D. The whole reader, and the page");
  await step("Only inbox.jsonl and reviewed.jsonl changed on the reader", async () => {
    checkReaderUnchangedElsewhere();
    eq(pageErrors, [], "Errors thrown in the page");
    eq(serverErrors, [], "Server failures seen by the page");
  });
  keepOutput(pride);
  console.log(`\nAll ${stepNumber} steps passed. Files kept in ${OUTPUT}, screenshots in ${SHOTS}.`);
}

let code = 0;
try {
  await main();
} catch (error) {
  code = 1;
  console.error(`\nFAILED in "${phaseName}", step ${stepNumber}:\n${error.message}`);
  if (!(error instanceof Failed)) console.error(error.stack);
  if (page) await page.screenshot({ path: path.join(SHOTS, "failed.png") }).catch(() => {});
  if (fs.existsSync(serverLog)) console.error(`\nServer log, last lines:\n${fs.readFileSync(serverLog, "utf-8").split("\n").slice(-15).join("\n")}`);
} finally {
  await finish();
}
process.exit(code);
