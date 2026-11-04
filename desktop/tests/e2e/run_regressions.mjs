#!/usr/bin/env node
// Browser checks of behaviour that only shows in a real page: saving through outages, keeping your
// place, keyboard focus and odd addresses. Same setup as run_e2e.mjs, see README.md here. Every check
// runs even when one before it fails, and the exit code is 0 only when all pass.

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = process.env.E2E_REPO ? path.resolve(process.env.E2E_REPO) : path.resolve(HERE, "../../..");
const TESTS = path.join(REPO, "desktop/tests");

const require = createRequire(process.env.E2E_PLAYWRIGHT_DIR ? path.join(path.resolve(process.env.E2E_PLAYWRIGHT_DIR), "x.js") : import.meta.url);
const { chromium } = require("playwright");

const PYTHON = `
import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path[:0] = [${JSON.stringify(path.join(TESTS, "e2e"))}, ${JSON.stringify(TESTS)}, ${JSON.stringify(path.join(REPO, "desktop"))}]
import dev_server
from fixtures.make_reader import build_reader, make_epub, epub_highlight, lua_file, KOREADER

root = Path(sys.argv[1])
mount = root / "PB629"
fake = build_reader(mount)
(root / "assistant").mkdir()
dev_server.make_assistant(root / "assistant")
(root / "home").mkdir()

# A long book
count = 150
device = "/mnt/ext1/English/Big.epub"
local = mount / "English/Big.epub"
words = "lorem ipsum dolor sit amet " * 12
chapters = [(f"Chap {c}", [f"Paragraph {c} {i} {words}" for i in range(count // 10 + 1)]) for c in range(10)]
make_epub(local, "Big", "Dr. Bartholomew Alexander Featherstonehaugh-Cholmondeley and Associates of the Institute", chapters)
notes = [epub_highlight(i % 10 + 1, i // 10 + 1, f"Paragraph {i % 10} {i // 10} {words}",
                        f"2026-10-02 00:{i // 60:02d}:{i % 60:02d}", chapter=f"Chap {i % 10}", pageno=i + 1) for i in range(count)]
local.with_suffix(".sdr").mkdir()
(local.with_suffix(".sdr") / "metadata.epub.lua").write_text(lua_file("x", {
    "annotations": notes, "doc_path": device, "percent_finished": 0.9, "doc_pages": count,
    "doc_props": {"title": "A Very Long Book With A Very Long Title Indeed, Being The Whole Works",
                  "authors": "Dr. Bartholomew Alexander Featherstonehaugh-Cholmondeley and Associates of the Institute"}}),
    encoding="utf-8")
history = mount / KOREADER / "history.lua"
books = [device, *fake.books.values()]
history.write_text(lua_file("./history.lua", [{"time": 1793900000 - 1000 * i, "file": p} for i, p in enumerate(books)]), encoding="utf-8")
settings = SimpleNamespace(companion_seconds=5, ai="believable", ai_delay=float(sys.argv[2]))
print(json.dumps({"env": dev_server.environment(root, mount, settings), "books": fake.books}))
`;

const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pbanki-regress-")));
const built = spawnSync("python3", ["-c", PYTHON, root, "0.4"], { encoding: "utf-8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
if (built.status !== 0) throw new Error(built.stderr);
const { env: baseEnv } = JSON.parse(built.stdout);
const mount = baseEnv.PBANKI_MOUNT;
if (mount.startsWith("/Volumes") || root.includes("Application Support")) throw new Error("The test must stay in temporary folders");
const env = { ...baseEnv, PYTHONDONTWRITEBYTECODE: "1" };

function freePort() {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.listen(0, "127.0.0.1", () => {
      const found = probe.address().port;
      probe.close(() => resolve(found));
    });
  });
}

const port = await freePort();
const url = `http://127.0.0.1:${port}`;
let server = null;

async function startServer() {
  server = spawn("python3", ["-u", path.join(REPO, "desktop/run.py"), "--no-open", "--port", String(port)], { env, stdio: ["ignore", "pipe", "ignore"] });
  server.exited = new Promise((done) => server.on("exit", done));
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("The server didn't start")), 20000);
    server.stdout.on("data", (chunk) => {
      if (String(chunk).includes("running at")) {
        clearTimeout(timer);
        resolve();
      }
    });
  });
}

async function killServer() {
  server.kill("SIGKILL");
  await server.exited;
  server = null;
}

async function call(method, address, body) {
  const response = await fetch(url + address, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  return { status: response.status, data: await response.json().catch(() => null) };
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function until(check, message, ms = 8000) {
  const end = Date.now() + ms;
  for (;;) {
    if (await check()) return;
    if (Date.now() > end) throw new Error(message);
    await sleep(40);
  }
}

function eq(actual, expected, message) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${message}\n    expected ${b}\n    actual   ${a}`);
}

function ok(value, message) {
  if (!value) throw new Error(message);
}

await startServer();
const browser = await chromium.launch({ headless: true });
const listed = (await call("GET", "/api/books")).data.books;
const idOf = (pattern) => listed.find((row) => pattern.test(row.title)).id;
const BIG = idOf(/Very Long/);
const PRIDE = idOf(/Pride/);
const GARDENS = idOf(/Gardens/);
const TIME = idOf(/Spend a Day/);
const SCAN = idOf(/Scanned/);
const bookOf = async (id) => (await call("POST", `/api/books/${id}/open`)).data;
const newCard = async (book, hid, kind = "qa") => (await call("POST", `/api/books/${book}/highlights/${hid}/cards`, { kind })).data.card;
async function serverCard(book, hid, id) {
  const found = (await call("GET", `/api/books/${book}`)).data.highlights.find((h) => h.id === hid).cards.find((c) => c.id === id);
  return found || null;
}

const context = () => browser.newContext({ viewport: { width: 1100, height: 800 } });

async function open(pageContext, hash) {
  const page = await pageContext.newPage();
  page.problems = [];
  page.on("pageerror", (error) => page.problems.push(error.message));
  await page.goto(`${url}/${hash}`);
  return page;
}

const crumb = async (page) => (await page.locator(".crumbt").innerText()).split("·").pop().trim();
const bars = (page) => page.locator(".unplug").allInnerTexts();
const button = (page, name) => page.getByRole("button", { name, exact: false });

const checks = [];
const check = (title, run) => checks.push({ title, run });
let scope = null; // Holds the context, closed after each check

async function fresh(hash) {
  scope = await context();
  const page = await open(scope, hash);
  await page.locator(".brand, .h1").first().waitFor();
  return page;
}

// Keeping your place

check("Tabs and Back to editing keep the highlight you were on", async () => {
  const book = await bookOf(BIG);
  await call("POST", `/api/books/${BIG}/highlights/${book.highlights[80].id}/open`);
  await newCard(BIG, book.highlights[80].id).then((card) => call("PATCH", `/api/cards/${card.id}`, { front: "Q?", back: "A" }));
  const page = await fresh(`#/book/${BIG}/highlights`);
  await page.locator(".hl").nth(80).click();
  await until(async () => (await crumb(page)) === "81 of 150", "Row 81 opens");
  await button(page, "Connections").click();
  await page.locator(".mtitle, .ml").first().waitFor();
  await button(page, "Highlights").click();
  await page.locator(".crumbt").waitFor();
  eq(await crumb(page), "81 of 150", "After the Connections tab");
  await page.locator(".top .btn.pri").click();
  await button(page, "Back to editing").click();
  await page.locator(".crumbt").waitFor();
  eq(await crumb(page), "81 of 150", "After Back to editing");
});

check("A book's Highlights filter stays with that book", async () => {
  const page = await fresh(`#/book/${BIG}/highlights`);
  await page.locator(".nav .seg .sg").nth(2).click();
  await page.evaluate((id) => (location.hash = `#/book/${id}/highlights`), SCAN);
  await page.waitForFunction(() => document.querySelector(".crumbt") && !document.querySelector(".crumbt").innerText.includes("of 150"));
  eq(await page.locator(".nav .seg .sg.on").innerText().then((t) => t.split(" ")[0]), "All", "The other book's filter");
  ok((await page.locator(".hl.on").count()) === 1, "The highlight on screen is in the list");
});

// The hint

check("A finished suggestion leaves the hint being typed elsewhere alone", async () => {
  const book = await bookOf(PRIDE);
  const page = await fresh(`#/book/${PRIDE}/highlights/${book.highlights[1].id}`);
  await button(page, "Suggest").first().click();
  await page.keyboard.press("j");
  await page.locator(".hint").click();
  await page.keyboard.type("hint for the next one", { delay: 20 });
  await until(async () => (await call("GET", `/api/books/${PRIDE}`)).data.highlights[1].cards.length > 0, "The first request finishes");
  await sleep(300);
  eq(await page.locator(".hint").inputValue(), "hint for the next one", "The hint");
  await page.keyboard.press("Tab");
  await page.locator(".hint").evaluate((el) => el.blur());
  await page.keyboard.press("k");
  await until(async () => (await crumb(page)).startsWith("2 "), "Back on the first");
  eq(await page.locator(".hint").inputValue(), "", "Another highlight's box is its own");
});

check("Typing more after asking keeps the new text, and the hint isn't carried to other books", async () => {
  const book = await bookOf(GARDENS);
  const page = await fresh(`#/book/${GARDENS}/highlights/${book.highlights[2].id}`);
  await page.locator(".hint").fill("first hint");
  await page.keyboard.press(process.platform === "darwin" ? "Meta+Enter" : "Control+Enter");
  await page.locator(".hint").blur();
  await page.keyboard.press(process.platform === "darwin" ? "Meta+Enter" : "Control+Enter");
  await page.locator(".hint").click();
  await page.keyboard.type(" and more");
  await until(async () => (await call("GET", `/api/books/${GARDENS}`)).data.highlights[2].cards.length > 0, "The request finishes");
  await sleep(300);
  eq(await page.locator(".hint").inputValue(), "first hint and more", "Text typed since stays");
  await page.evaluate((id) => (location.hash = `#/book/${id}/highlights`), TIME);
  await page.waitForFunction(() => document.querySelector(".bookt") && !document.querySelector(".bookt").innerText.includes("Gardens"));
  await page.locator(".hint").first().waitFor();
  eq(await page.locator(".hint").inputValue(), "", "Another book's box");
});

check("A double click on Suggest sends one request and shows no error", async () => {
  const book = await bookOf(PRIDE);
  const page = await fresh(`#/book/${PRIDE}/highlights/${book.highlights[4].id}`);
  const statuses = [];
  page.on("response", (r) => r.url().includes("/suggest") && statuses.push(r.status()));
  await page.locator(".go").first().dblclick();
  await sleep(1200);
  eq(statuses.filter((s) => s === 409), [], "Refused requests");
  eq(await bars(page), [], "Bars on screen");
});

// Cards with nothing to answer

check("A blank-answer question and a cloze hiding nothing block the save", async () => {
  const book = await bookOf(TIME);
  const pending = book.highlights.filter((h) => h.status !== "reviewed");
  const hid = pending[0].id;
  const page = await fresh(`#/book/${TIME}/highlights/${hid}`);
  await button(page, "+ Cloze").click();
  await page.locator(".ta.c").first().waitFor();
  await page.goto(`${url}/#/book/${TIME}/save`);
  await page.locator(".srneed").first().waitFor();
  eq(await page.locator(".srneed").first().innerText(), "No words hidden", "The mark");
  ok(await page.locator(".saver .btn.pri").isDisabled(), "Save is off");
  await page.locator(".saver .errb .lnk").click();
  await until(async () => (await page.evaluate(() => location.hash)).includes(hid), "Go to it goes there");
  await page.locator(".card .x").first().click();
});

check("Hide selected words over hidden text changes nothing and says why", async () => {
  const book = await bookOf(GARDENS);
  const hid = book.highlights[3].id;
  const card = await newCard(GARDENS, hid, "cloze");
  await call("PATCH", `/api/cards/${card.id}`, { front: "Plain {{c1::hidden}} words here" });
  const page = await fresh(`#/book/${GARDENS}/highlights/${hid}`);
  const field = page.locator(`#f-${card.id}`);
  await field.evaluate((el) => {
    el.focus();
    el.setSelectionRange(2, 14);
  });
  await button(page, "Hide selected words").click();
  eq(await field.inputValue(), "Plain {{c1::hidden}} words here", "The field");
  ok((await bars(page)).join(" ").includes("already hidden"), "It says why");
});

// Keyboard and focus

check("Rewrite takes the cursor, and Enter sends it", async () => {
  const book = await bookOf(GARDENS);
  const hid = book.highlights[4].id;
  const card = await newCard(GARDENS, hid);
  await call("PATCH", `/api/cards/${card.id}`, { front: "Original question?", back: "Answer" });
  const page = await fresh(`#/book/${GARDENS}/highlights/${hid}`);
  await page.locator(".card").filter({ has: page.locator(`#f-${card.id}`) }).getByRole("button", { name: "Rewrite" }).click();
  await page.keyboard.type("make it short");
  const input = page.locator(".rw .in");
  eq(await input.inputValue(), "make it short", "The box got the typing");
  await page.keyboard.press("Enter");
  await until(async () => (await page.locator(".rw .in").count()) === 0, "Enter sends it and the panel closes");
});

check("Letters typed right after + Question land in the new field", async () => {
  const book = await bookOf(BIG);
  const page = await fresh(`#/book/${BIG}/highlights/${book.highlights[100].id}`);
  await page.route("**/api/books/*/highlights/*/cards", async (route) => {
    await sleep(200);
    await route.continue();
  });
  await button(page, "+ Question").click();
  await page.keyboard.type("jokes about xylophones", { delay: 20 });
  await until(async () => (await page.locator(".ta.f").last().inputValue()) === "jokes about xylophones", "Every letter arrives", 3000);
  eq(await crumb(page), "101 of 150", "J and K stayed out of it");
});

check("J works from a dropdown and from a read-only saved card", async () => {
  const book = await bookOf(TIME);
  const savedOne = book.highlights.find((h) => h.saved.length);
  const editable = book.highlights.find((h) => h.canEdit);
  ok(savedOne, "The fixture has a highlight with saved cards");
  const page = await fresh(`#/book/${TIME}/highlights/${editable.id}`);
  const before = await crumb(page);
  await page.locator(".comp .sel").first().focus();
  await page.keyboard.press(editable.index === 0 ? "j" : "k");
  await until(async () => (await crumb(page)) !== before, "The key moves from a dropdown");
  await page.goto(`${url}/#/book/${TIME}/highlights/${savedOne.id}`);
  await page.locator(".card.ro textarea").first().click();
  const now = await crumb(page);
  await page.keyboard.press(savedOne.index === 0 ? "j" : "k");
  await until(async () => (await crumb(page)) !== now, "The key moves from a read-only card");
});

// Drawers

check("Escape closes a drawer, and Back afterwards doesn't reopen it", async () => {
  const page = await fresh(`#/book/${GARDENS}/connections`);
  await button(page, "Highlights").click();
  await page.locator(".crumbt").waitFor();
  await until(async () => (await page.evaluate(() => location.hash)).split("/").length === 5, "The highlight is in the address");
  const hash = await page.evaluate(() => location.hash);
  await page.locator(".top .lnk").filter({ hasText: "Settings" }).click();
  await page.locator(".drawer").waitFor();
  await page.keyboard.press("Escape");
  await until(async () => (await page.locator(".drawer").count()) === 0, "Escape closes it");
  eq(await page.evaluate(() => location.hash), hash, "The address is back");
  await page.goBack();
  await sleep(200);
  eq(await page.locator(".drawer").count(), 0, "No drawer after Back");
  ok(!(await page.evaluate(() => location.hash)).includes("drawer"), "Back left the screen");
  ok((await page.evaluate(() => location.hash)).includes("connections"), "Back went to the screen before");
});

// Saving through trouble

const OUTAGE_TEXT = "WRITTEN WHILE DOWN";

check("Text typed in an outage survives moving away, saves by itself, and never overwrites newer text", async () => {
  const book = await bookOf(GARDENS);
  const hid = book.highlights.find((h) => h.canEdit).id;
  const card = await newCard(GARDENS, hid);
  const page = await fresh(`#/book/${GARDENS}/highlights/${hid}`);
  const field = page.locator(`#f-${card.id}`);
  await field.waitFor();
  await killServer();
  try {
    await until(async () => (await bars(page)).join(" ").includes("stopped answering"), "The page notices");
    await field.click();
    await page.keyboard.type(OUTAGE_TEXT);
    await sleep(700);
    ok((await bars(page)).join(" ").includes("safe on this computer"), "It says the work is safe");
    await button(page, "Next").click();
    await until(async () => (await field.count()) === 0, "Moved on");
    await button(page, "Previous").click();
    await field.waitFor();
    eq(await field.inputValue(), OUTAGE_TEXT, "The text is back on screen");
  } finally {
    await startServer();
  }
  await until(async () => (await serverCard(GARDENS, hid, card.id))?.front === OUTAGE_TEXT, "It reaches the server by itself", 15000);
  await until(async () => (await bars(page)).length === 0, "Every bar clears", 15000);
  await field.fill("Newer text");
  await page.locator(".hint").click();
  await until(async () => (await serverCard(GARDENS, hid, card.id))?.front === "Newer text", "The newer text saves");
  await page.reload();
  await field.waitFor();
  await sleep(500);
  eq((await serverCard(GARDENS, hid, card.id)).front, "Newer text", "The newer text stays");
});

check("Text typed in an outage survives closing the tab", async () => {
  const book = await bookOf(GARDENS);
  const hid = book.highlights.find((h) => h.canEdit).id;
  const card = await newCard(GARDENS, hid);
  const page = await fresh(`#/book/${GARDENS}/highlights/${hid}`);
  await page.locator(`#f-${card.id}`).waitFor();
  await killServer();
  try {
    await page.locator(`#f-${card.id}`).fill("typed during outage");
    await sleep(100);
    await page.close();
  } finally {
    await startServer();
  }
  const again = await open(scope, `#/book/${GARDENS}/highlights/${hid}`);
  await again.locator(`#f-${card.id}`).waitFor();
  await until(async () => (await serverCard(GARDENS, hid, card.id))?.front === "typed during outage", "The next load sends it");
  eq(await again.evaluate(() => localStorage.getItem("pbanki-unsent-edits")), "{}", "Nothing left to send");
});

check("Text typed in an outage saves while the field still has the cursor", async () => {
  const book = await bookOf(GARDENS);
  const hid = book.highlights.find((h) => h.canEdit).id;
  const card = await newCard(GARDENS, hid);
  const page = await fresh(`#/book/${GARDENS}/highlights/${hid}`);
  const field = page.locator(`#f-${card.id}`);
  await field.waitFor();
  await killServer();
  try {
    await field.click();
    await page.keyboard.type("kept");
    await sleep(600);
  } finally {
    await startServer();
  }
  await until(async () => (await serverCard(GARDENS, hid, card.id))?.front === "kept", "It reaches the server without leaving the field", 15000);
  await until(async () => (await bars(page)).length === 0, "Every bar clears", 15000);
  ok(await field.evaluate((el) => el === document.activeElement), "The cursor stayed");
});

check("Unplugging and plugging in the reader keeps the cursor in the field", async () => {
  const book = await bookOf(GARDENS);
  const hid = book.highlights.find((h) => h.canEdit).id;
  const card = await newCard(GARDENS, hid);
  const page = await fresh(`#/book/${GARDENS}/highlights/${hid}`);
  const field = page.locator(`#f-${card.id}`);
  await field.click();
  const away = `${mount}.away`;
  fs.renameSync(mount, away);
  let plugged = false;
  try {
    await page.locator(".unplug").filter({ hasText: "unplugged" }).waitFor({ timeout: 15000 });
    ok(await field.evaluate((el) => el === document.activeElement), "The cursor stayed when it went");
    fs.renameSync(away, mount);
    plugged = true;
    await page.locator(".unplug").filter({ hasText: "unplugged" }).waitFor({ state: "detached", timeout: 15000 });
    ok(await field.evaluate((el) => el === document.activeElement), "The cursor stayed when it came back");
  } finally {
    if (!plugged) fs.renameSync(away, mount);
  }
  await call("DELETE", `/api/cards/${card.id}`);
});

check("A reload right after a second edit keeps the second edit", async () => {

  const book = await bookOf(BIG);
  const hid = book.highlights[110].id;
  for (const delay of [20, 60, 120]) {
    const card = await newCard(BIG, hid);
    const page = await fresh(`#/book/${BIG}/highlights/${hid}`);
    const field = page.locator(`#f-${card.id}`);
    await field.waitFor();
    const first = page.waitForRequest((r) => r.method() === "PATCH");
    await field.fill("first");
    await first;
    await sleep(delay);
    await field.fill("second");
    await page.reload();
    await until(async () => (await serverCard(BIG, hid, card.id))?.front === "second", `Second text kept at ${delay} ms`, 4000);
    await page.close();
    await scope.close();
  }
  scope = await context();
});

check("Removing a card straight after typing: the edit goes first, with no false error, and Undo keeps it", async () => {
  const book = await bookOf(BIG);
  const hid = book.highlights[111].id;
  const card = await newCard(BIG, hid);
  const page = await fresh(`#/book/${BIG}/highlights/${hid}`);
  const order = [];
  page.on("request", (r) => ["PATCH", "DELETE"].includes(r.method()) && order.push(r.method()));
  await page.route(new RegExp(`/api/books/${BIG}$`), async (route) => {
    await sleep(300);
    await route.continue();
  });
  await page.locator(`#f-${card.id}`).click();
  await page.keyboard.type("aaa");
  await sleep(350);
  await page.keyboard.type("bbb");
  await page.locator(".card .x").first().click();
  await page.locator(".undo").waitFor();
  eq(order[order.length - 1], "DELETE", "The last request");
  eq(await bars(page), [], "Bars on screen");
  await page.locator(".undo .lnk").click();
  await page.locator(`#f-${card.id}`).waitFor();
  eq(await page.locator(`#f-${card.id}`).inputValue(), "aaabbb", "Undo brings back the edit");
});

check("A very long paste saves", async () => {
  const book = await bookOf(BIG);
  const hid = book.highlights[112].id;
  const card = await newCard(BIG, hid);
  const page = await fresh(`#/book/${BIG}/highlights/${hid}`);
  const text = "x".repeat(70000);
  await page.locator(`#f-${card.id}`).fill(text);
  await until(async () => (await serverCard(BIG, hid, card.id))?.front.length === 70000, "All of it reaches the server");
  eq(await bars(page), [], "Bars on screen");
});

check("Seven tabs all stay responsive", async () => {
  scope = await context();
  const pages = [];
  for (let i = 0; i < 7; i += 1) pages.push(await open(scope, "#/books"));
  await pages[6].locator(".h1").first().waitFor({ timeout: 8000 });
  const status = await Promise.race([pages[0].evaluate(async () => (await fetch("/api/state")).status), sleep(3000).then(() => "no answer")]);
  eq(status, 200, "A request from the first tab");
});

// Addresses

check("Odd addresses show a screen, never a blank page", async () => {
  const list = ["#/book/%E0%A4%A/suggest", "#/%E0%A4%A", "#/suggest", "#/highlights", "#/saved", "#/constructor", "#/toString", "#/__proto__", "#/valueOf", "#/hasOwnProperty",
    `#/book/${GARDENS}/constructor`, `#/book/${GARDENS}/toString`, `#/book/${GARDENS}/__proto__`, "#/books?drawer=__proto__", "#/books?drawer=constructor"];
  scope = await context();
  for (const hash of list) {
    const page = await open(scope, hash);
    await page.locator(".h1, .brand").first().waitFor({ timeout: 5000 }).catch(() => {});
    await sleep(150);
    const text = (await page.locator("#root").innerText()).trim();
    ok(text.length > 0, `${hash} is blank`);
    ok(!text.includes("[object Object]") && !text.includes("Something went wrong"), `${hash} shows: ${text.slice(0, 60)}`);
    eq(page.problems, [], `${hash} threw`);
    await page.close();
  }
});

check("Back and Forward between books during refetches never stick on Opening the book", async () => {
  const page = await fresh(`#/book/${GARDENS}/highlights`);
  await page.locator(".crumbt").waitFor();
  await page.evaluate((id) => (location.hash = `#/book/${id}/highlights`), PRIDE);
  await page.waitForFunction(() => document.querySelector(".bookt")?.innerText.includes("Pride"));
  await page.route(new RegExp(`/api/books/${GARDENS}$`), async (route) => {
    await sleep(300);
    await route.continue();
  });
  await page.route(new RegExp(`/api/books/${PRIDE}$`), async (route) => {
    await sleep(300);
    await route.continue();
  });
  await call("POST", `/api/books/${PRIDE}/highlights/${(await bookOf(PRIDE)).highlights[0].id}/open`);
  await call("POST", `/api/books/${PRIDE}/run`, { action: "stop" });
  await page.goBack();
  await until(async () => page.evaluate(() => document.querySelector(".bookt")?.innerText.includes("Gardens")), "The book before opens", 6000);
  await page.goForward();
  await until(async () => page.evaluate(() => document.querySelector(".bookt")?.innerText.includes("Pride")), "Forward opens the book again", 6000);
});

// Screens

check("A very long author doesn't push the title out of the bar", async () => {
  const page = await fresh(`#/book/${BIG}/highlights`);
  const bar = await page.locator(".top").boundingBox();
  const title = await page.locator(".bookbtn .bookt").boundingBox();
  ok(title.y >= bar.y, `The title starts at ${title.y}, the bar at ${bar.y}`);
  ok((await page.locator(".bookbtn").innerText()).includes("change book"), "change book still shows");
});

check("A prompt picked for a book survives a reload", async () => {
  const book = await bookOf(TIME);
  const hid = book.highlights.find((h) => h.status !== "reviewed").id;
  const page = await fresh(`#/book/${TIME}/highlights/${hid}`);
  const select = page.locator(".comp .opt").filter({ hasText: "Prompt" }).locator("select");
  const first = await select.inputValue();
  const other = first === "Standard" ? "Self Help" : "Standard";
  await select.selectOption(other);
  await sleep(300);
  await page.reload();
  await select.waitFor();
  eq(await select.inputValue(), other, "The dropdown");
});

check("The Save mark choice stays with its book", async () => {
  const page = await fresh(`#/book/${GARDENS}/save`);
  const box = page.locator(".saver input[type=checkbox]");
  await box.waitFor();
  await box.uncheck();
  await page.evaluate((id) => (location.hash = `#/book/${id}/save`), PRIDE);
  await page.waitForFunction(() => !document.querySelector(".saver input")?.checked === false);
  ok(await box.isChecked(), "The other book's box is ticked");
});

check("Suggest again can be left, and comes back to the finished run", async () => {
  const book = await bookOf(PRIDE);
  await call("POST", `/api/books/${PRIDE}/run`, { action: "start", options: { connections: false, ideas: false } });
  await until(async () => !(await call("GET", `/api/books/${PRIDE}`)).data.run.active, "The run ends", 30000);
  const page = await fresh(`#/book/${PRIDE}/suggest`);
  await button(page, "Suggest again").click();
  await button(page, "Skip, write them myself").or(button(page, "Back to the last run")).first().waitFor();
  await page.evaluate((id) => (location.hash = `#/book/${id}/highlights`), PRIDE);
  await page.locator(".crumbt").waitFor();
  await page.goBack();
  await button(page, "Review highlights").waitFor();
  ok(book.highlights.length > 0, "The book has highlights");
});

check("A finished run gives way to the options while highlights are waiting", async () => {
  let id = null;
  for (const row of listed.filter((item) => !item.none)) {
    const opened = await call("POST", `/api/books/${row.id}/open`);
    if (opened.data?.suggest?.due > 0) {
      id = row.id;
      break;
    }
  }
  ok(id, "A book has highlights waiting");
  await call("POST", `/api/books/${id}/run`, { action: "start", options: { highlights: false, connections: true, ideas: false } });
  await until(async () => (await call("GET", `/api/books/${id}`)).data.run.phase === "done", "The run ends", 30000);
  ok((await call("GET", `/api/books/${id}`)).data.suggest.due > 0, "Highlights are still waiting");
  const page = await fresh(`#/book/${id}/suggest`);
  await page.locator(".opt", { hasText: "AI reads" }).waitFor();
  await button(page, "Back to the last run").click();
  await button(page, "Review highlights").waitFor();
  await button(page, "Suggest again").click();
  await page.locator(".opt", { hasText: "AI reads" }).waitFor();
});

check("Suggest again replaces suggestions instead of finding nothing waiting", async () => {
  const id = PRIDE;
  const book = await bookOf(id);
  await until(async () => !(await call("GET", `/api/books/${id}`)).data.run.active, "No run going", 30000);
  const page = await fresh(`#/book/${id}/suggest`);
  const again = button(page, "Suggest again");
  if (await again.count()) await again.click();
  const start = page.locator(".rowg .btn.pri");
  await start.waitFor();
  ok((await start.innerText()).startsWith("Suggest again for"), `The button says ${await start.innerText()}`);
  await page.getByText("replaces the suggestions").waitFor();
  await start.click();
  await until(async () => {
    const run = (await call("GET", `/api/books/${id}`)).data.run;
    return !run.active && run.phase === "done";
  }, "The run ends", 30000);
  const rows = (await call("GET", `/api/books/${id}`)).data.run.rows;
  ok(!rows[0].path.includes("No highlights were waiting"), rows[0].path);
  ok(book.highlights.length > 0, "The book has highlights");
});

check("Key ideas can be started on a finished book, and don't claim nothing's left before looking", async () => {
  const page = await fresh(`#/book/${TIME}/ideas`);
  const book = await bookOf(TIME);
  await call("POST", `/api/books/${TIME}/run`, { action: "stop" });
  if (!book.ideas.length) ok(!(await page.locator(".navlist").innerText()).includes("No key ideas left"), "It hasn't looked yet");
  const start = page.locator(".navhd .btn");
  await start.click();
  await until(async () => (await call("GET", `/api/books/${TIME}`)).data.run.progress?.ideas?.state === "done", "Key ideas run", 15000);
  const after = (await call("GET", `/api/books/${TIME}`)).data;
  eq(after.run.options.highlights, false, "Highlights weren't asked for");
});

check("A book with nothing pending offers key ideas instead of cards for 0 highlights", async () => {
  const book = await bookOf(TIME);
  for (const h of book.highlights.filter((item) => item.canEdit)) await call("POST", `/api/books/${TIME}/highlights/${h.id}/skip`, { skipped: true });
  await call("POST", `/api/books/${TIME}/run`, { action: "stop" });
  const page = await fresh(`#/book/${TIME}/suggest`);
  const again = button(page, "Suggest again");
  if (await again.count()) await again.click();
  const start = page.locator(".rowg .btn.pri");
  await start.waitFor();
  await page.locator(".cbx input").last().check();
  eq(await start.innerText(), "Look for key ideas", "The button");
  ok(!(await start.isDisabled()), "The button is on");
  await page.locator(".cbx input").last().uncheck();
  ok(await start.isDisabled(), "With nothing ticked there's nothing to do");
  await page.locator(".cbx input").last().check();
});

check("A save waiting for the reader says what it leaves out, and the Saved screen says what's still here", async () => {
  const book = await bookOf(BIG);
  const editable = book.highlights.filter((h) => h.canEdit);
  // Answers left blank by earlier checks would block this save
  for (const h of book.highlights) {
    for (const card of h.cards) await call("PATCH", `/api/cards/${card.id}`, card.front.trim() ? { back: "An answer" } : { front: "A question", back: "An answer" });
  }
  await call("POST", `/api/books/${BIG}/highlights/${editable[0].id}/open`);
  const first = await newCard(BIG, editable[0].id);
  await call("PATCH", `/api/cards/${first.id}`, { front: "Saved first?", back: "Yes" });
  const away = `${mount}.away`;
  fs.renameSync(mount, away);
  let plugged = false;
  try {
    await until(async () => !(await call("GET", "/api/state")).data.reader.connected, "The app sees it unplugged");
    const page = await fresh(`#/book/${BIG}/save`);
    await page.locator(".saver .btn.pri").click();
    await page.locator(".waitb").waitFor();
    await call("POST", `/api/books/${BIG}/highlights/${editable[1].id}/open`);
    const second = await newCard(BIG, editable[1].id);
    await call("PATCH", `/api/cards/${second.id}`, { front: "Written after?", back: "Yes" });
    await page.reload();
    await page.locator(".waitb").waitFor();
    ok((await page.locator(".saver").innerText()).includes("isn't part of that save"), "It says the newer card stays out");
    fs.renameSync(away, mount);
    plugged = true;
    await page.locator(".center .h1").filter({ hasText: "on your reader" }).waitFor({ timeout: 15000 });
    ok((await page.locator(".center").innerText()).includes("1 more card"), "The Saved screen mentions it");
  } finally {
    if (!plugged) fs.renameSync(away, mount);
  }
});

check("The Plug in screen's check time counts, and its refused path stays for correcting", async () => {
  const away = `${mount}.away`;
  fs.renameSync(mount, away);
  try {
    const page = await fresh("#/plug");
    await page.locator(".watch").waitFor();
    const seen = new Set();
    for (let i = 0; i < 16; i += 1) {
      seen.add(await page.locator(".watch").innerText());
      await sleep(300);
    }
    ok(seen.size > 1, `The line never moved: ${[...seen]}`);
    const field = page.locator("input.in");
    const before = await field.inputValue();
    await field.fill("relative/path");
    await field.press("Enter");
    await until(async () => (await bars(page)).join(" ").includes("full path"), "It says why");
    eq(await field.inputValue(), "relative/path", "The field keeps what was typed");
    await button(page, "Choose folder").click();
    await until(async () => (await bars(page)).length === 0, "Choose folder clears the old error");
    await call("PATCH", "/api/settings", { mount: before });
  } finally {
    fs.renameSync(away, mount);
    await call("PATCH", "/api/settings", { mount });
  }
});

let failures = 0;
try {
  for (const { title, run } of checks) {
    if (process.env.E2E_ONLY && !title.includes(process.env.E2E_ONLY)) continue;
    try {
      await run();
      console.log(`  ok    ${title}`);
    } catch (error) {
      failures += 1;
      console.log(`  FAIL  ${title}\n    ${error.message}`);
      if (!server) await startServer();
    } finally {
      if (scope) await scope.close().catch(() => {});
      scope = null;
    }
  }
} finally {
  await browser.close().catch(() => {});
  if (server) await killServer().catch(() => {});
  fs.rmSync(root, { recursive: true, force: true });
}
console.log(failures ? `\n${failures} of ${checks.length} checks failed.` : `\nAll ${checks.length} checks passed.`);
process.exit(failures ? 1 : 0);
