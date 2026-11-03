# pbAnki Desktop Companion: implementation plan

A local web app, served by a small Python program on the Mac, for making flashcards in bulk from a book's highlights. The approved design is in `desktop/design/` (`mock.dc.html`, `companion.css`, `mock-data.js`). When this plan and the mock disagree about how something looks or reads, the mock wins. When they disagree about what touches the reader, this plan wins.

## 1. Ground rules

1. **The reader is the user's everyday device.** The app only ever writes these paths on it:
   - `applications/pbanki/inbox.jsonl` (append)
   - `applications/pbanki/reviewed.jsonl` (append)
   - `<book folder>/<book stem>.companion.md` (only after the user clicks Copy to reader)
   - `applications/koreader/plugins/pbanki.koplugin/*` (only after the user clicks Update plugin)

   It never writes `.sdr` folders, `history.lua`, `settings.reader.lua`, `system/` or anything else. It never launches KOReader or any program on the reader. A test asserts this by snapshotting a fake reader before and after a whole session.
2. **Writes to the reader are proven, not assumed.** Its FAT disk has silently dropped newly created files and folders after a clean copy, and a read in the same mount session can be served from the Mac's cache (`assistant.koplugin/.claude/skills/deploy-plugin/SKILL.md`, incidents of 2026-07-12 and 2026-09-29). So all writes go through `device.py`: write, flush that file with `F_FULLFSYNC`, wait `PBANKI_SYNC_DELAY` seconds, **unmount and mount the disk again** (`diskutil`), read back and compare, re-copy only what vanished, up to 3 rounds, then fail loudly. Do not call `os.sync()`: on this Mac it was still blocked after 3 minutes. Paths outside `/Volumes` (a test's fake reader) skip the remount. `device.check_allowed` enforces ground rule 1 in code.
3. **Work is never lost.** Everything the user types or the AI writes goes to SQLite on the Mac immediately. The whole working screen functions with the reader unplugged, from a local cache of the book's highlights and passages.
4. **No third-party Python packages.** Python 3.11+ standard library only, so `python3 desktop/run.py` works with no install step. The browser code uses vendored Preact + htm, with no build step and nothing fetched from the internet at run time (fonts are vendored too).
5. **Local only.** The server binds `127.0.0.1`, rejects requests whose `Host` is not `127.0.0.1:<port>` or `localhost:<port>`, and rejects state-changing requests whose `Origin` is present and not the app's own. Book text is untrusted input to the AI, so the AI runs with no tools (see section 6).
6. **Plain English in the UI**, matching the mock's wording. Comments are short and describe the code. Match the plugin's naming where the same idea exists (`pending`, `reviewed`, `cardgen`).

## 2. Layout

```
desktop/
  PLAN.md
  run.py                     entry point: parses --port/--no-open, holds the data folder, starts the server, opens the browser
  design/                    approved mock, reference only, never served
  pbanki_desktop/
    __init__.py
    paths.py                 app-support paths and env overrides
    lua.py                   parser for KOReader's Lua data files
    reader.py                mount detection, reader check, history, book records, highlights
    epub.py                  spine, table of contents, xpointer and text lookup, passages
    companion.py             sidecar finding, sections, slicer port, chapter notes, create and copy
    prompts.py               prompt folders, Standard seeding, mtime watching
    cardgen.py               port of the plugin's cardgen.lua plus desktop request kinds
    checks.py                card checks (stand-alone words, near-duplicates)
    ai.py                    assistants, models, running claude and codex
    runs.py                  background suggestion runs, queue, pause, stop
    store.py                 SQLite schema and all queries
    save.py                  the save pipeline and the plugin update
    service.py               application logic that ties the modules together
    server.py                HTTP routes, SSE, static files
  prompts/Standard/
    connections.md
    ideas.md
    rewrite.md
  web/
    index.html
    companion.css            copied from design/, then extended only where needed
    fonts/                   vendored woff2 and their licences
    vendor/preact-htm.js
    js/                      app.js, api.js, and one module per screen
  tests/
    fixtures/                fake reader builder, a real KOReader sidecar, fake AI commands
    test_*.py                unittest, run with: python3 -m unittest discover -s desktop/tests
    e2e/                     browser tests and screenshots, run through Playwright
```

`paths.py` honours these for tests and for the user's convenience:
`PBANKI_DESKTOP_HOME` (default `~/Library/Application Support/pbAnki`), `PBANKI_MOUNT` (default `/Volumes/PB629`), `PBANKI_CLAUDE_BIN`, `PBANKI_CODEX_BIN`, `PBANKI_SYNC_DELAY`, `PBANKI_ASSISTANT_DIR` (default `~/Documents/GitHub/assistant.koplugin`), `PBANKI_TERMINAL_CMD` (test hook replacing the Terminal launch).

## 3. What the reader looks like

Facts checked against `assistant.koplugin/backups/*/koreader` and the plugin source:

- Mount `/Volumes/PB629`. Device paths start `/mnt/ext1/`, which maps to the mount. Paths on other roots (such as `/mnt/ext2/`) are not reachable; list those books dimmed with "On the memory card".
- KOReader lives at `applications/koreader`, pbAnki at `applications/pbanki`, the plugin at `applications/koreader/plugins/pbanki.koplugin`.
- `applications/koreader/history.lua` is `return { [1] = { ["time"] = N, ["file"] = "/mnt/ext1/English/X.epub" }, ... }`.
- `applications/koreader/settings.reader.lua` holds `document_metadata_folder`: `"doc"` (default) puts settings at `<book stem>.sdr/metadata.<ext>.lua` beside the book, where `<ext>` is the book's extension. `"dir"` uses `applications/koreader/docsettings/<device path>.sdr/`. `"hash"` cannot be resolved without KOReader's partial MD5, so implement that algorithm or report the book as having unreadable settings; do not guess.
- Sidecar fields used: `annotations` (list of `{datetime, drawer, text, note, chapter, pageno, pos0, pos1, page, ...}`), `pbanki_reviewed` (table of `key = true`), `pbanki_card_history` (list of `{front, back, suggested?, quote, note?}`), `pbanki_prompt`, `percent_finished`, `doc_pages`, `doc_props` (`title`, `authors`, with newlines between authors), `stats` as a fallback for title and authors.
- A pending highlight is exactly what the plugin's `pendingHighlights` selects: `drawer` set, `text` non-empty, key not reviewed. The key is exactly the plugin's `highlightKey`: `tostring(datetime) .. "|" .. pos`, with `pos` being `pos0` for reflowable books and `tostring(pos0.page)` for fixed-layout ones.
- A book's real file name can differ from the Calibre name. The Companion sidecar is `<book stem>.companion.md` beside the book, the same rule as `assistant_companion.lua`. An empty file counts as absent.

`reader.py` never raises on a malformed book; it returns the book with an `error` string, shown in plain words on its row.

### Reader check screen

Rows, in order, each `{title, path, ok, level}`: connected, KOReader found, pbAnki found, KOReader history read (with count), and one extra row: "pbAnki plugin can read desktop saves". This last row reads the plugin's `main.lua` on the reader and looks for `reviewed.jsonl`. If absent, the row is a warning with an **Update plugin** button, because without it saved highlights keep coming up as new on the reader. It does not block the rest of the app. The row is also a warning when an update started and was never proven, since the file can then hold the text and still be cut off, or later files can be old. A mark in `<home>` records this: it is made before the first write and removed once the update is proven.

## 4. Passages (`epub.py`)

`open_epub(path)` reads `META-INF/container.xml`, the OPF (spine order, title, author) and the table of contents (`nav.xhtml` or `toc.ncx`). Parsing tolerates bad markup: fall back to `html.parser` when XML parsing fails. Zip entries are read by exact name only and never extracted to disk.

Text extraction yields ordered blocks (paragraphs, headings, list items, blockquotes) with text and character offsets per spine document, skipping `script`, `style`, and `head`.

`locate(highlight)` finds where a highlight sits:

1. Read `DocFragment[N]` from `pos0` (crengine's N-th spine item, 1-based) when present. Check this against the real KOReader sidecar fixture.
2. Normalise both highlight and document into a word sequence the same way the plugin's `words()` does (lowercase, curly quotes and dashes unified, soft hyphens removed), keeping a map back to original offsets.
3. Search that document's word sequence. With several matches, choose the one nearest the element the rest of `pos0` points at; when that cannot be resolved, the first match. If the fragment has no match, search the whole book.
4. No match, or a non-ePub book: return `found: false`. The screen shows the highlight text alone with "This book's text couldn't be matched, so the AI sees only the highlight." Cards still work.

`passage(loc)` returns for display: `before2`, `before`, `pre`, `mark`, `post`, `after`, `endCh`, where `pre`/`post` are the rest of the highlight's own paragraph and `before`/`after` the neighbouring paragraphs, as in the mock. Show more widens by one paragraph each way. A highlight spanning several paragraphs keeps its breaks.

`ai_context(loc, mode)` returns `(before, after)` text for `600 words each side` (default), `The chapter` or `The whole book`. Chapter boundaries come from the table of contents, falling back to the spine document.

Chapters: each highlight's chapter is its annotation `chapter` (or the table-of-contents entry for its location). A table-of-contents entry that only holds chapters, like a part or the book's title in Standard Ebooks and collections, is not itself a chapter: its sub-entries are, to any depth. An entry is such a part when its sub-entries start in more than one spine document or in a different one from it, or, within one document, when it has under 150 words of its own and its sub-entries average 1000 words or more; a title starting with a number or "Chapter" is always a chapter, with its sub-entries as sections. The chapter number is its 1-based position among chapters, and a part has none. Chapter context and passages end at the next chapter or part. Display name: the title as written when it already starts with a number, a Roman numeral or "Chapter"; otherwise `N · Title` in lists and `Chapter N: Title` on cards. Connections always name both number and title.

## 5. Prompts (`prompts.py`)

Folder: `<home>/prompts/<Name>/`, with the same file rules as `prompts.lua` (`questions.md`/`question.md`, `cloze.md`, `definitions.md`/`definition.md`) plus `connections.md`, `ideas.md`, `rewrite.md`. A missing or blank file means Standard's. `normalize()` matches `prompts.lua`'s exactly (test it against Lua).

On start, Standard is rewritten from the repo: `koreader/pbanki.koplugin/prompts/Standard/*` and `desktop/prompts/Standard/*`, with a README explaining that edits to Standard are undone. `prompts/*` folders from the repo other than Standard (today `Self Help`) are copied once if not already present, and never overwritten. Prompt files are re-read for every AI call, so edits apply at once; a watcher records each file's mtime to report "reloaded N s ago" and notifies the page. Default prompt for a book: its `pbanki_prompt` from the sidecar when that folder exists here, else the last prompt used, else Standard.

Actions: list folders and their files, each marked "this prompt" or "uses Standard", with the path shown always the one in the chosen folder; read one; open it in the default text editor (`open -t`); reveal it in Finder (`open -R`). Opening a "uses Standard" file in another folder first copies Standard's text to that folder's file, because edits to Standard are undone. Revealing one copies nothing, and shows the folder if the file isn't there. Folder names from requests are checked against the listing, so they cannot escape the prompts folder.

## 6. AI (`cardgen.py`, `ai.py`, `runs.py`)

`cardgen.py` ports `cardgen.lua` function for function (`build_messages`, `parse_reply`, `parse_cards`, `extract_json`, `parse_definition`, `quotes_text`, `expand_cloze`, `was_edited`, `pick_recent`, `recent_section`, `count_instruction`, `append_history`). Parity tests run the Lua module under `lua` on shared inputs and compare the output byte for byte, so the two cannot drift apart.

Desktop additions to the reply format, questions and cloze requests only, added only when Companion notes are sent: an optional top-level `"later_research"` string, filled only when the notes' "Since publication" lines question what this highlight claims.

Kinds beyond the plugin:

- **Connections** (`connections.md`). Input: numbered highlights with number-and-title chapter, plus the sliced Companion when present. Reply: `{"connections":[{"title","front","back","sources":[1,3]}]}`. Answers citing fewer than two valid highlight numbers are dropped.
- **Key ideas** (`ideas.md`). Input: the sliced Companion, all highlights with chapter. Reply: `{"chapters":[{"chapter","big","points":[{"text","covered":true,"idea":0}]}],"ideas":[{"title","chapter","note","quote","front","back"}]}`. Each `quote` is checked word for word against the book with `quotes_text`; a quote that isn't found is dropped and the section hidden. Ideas from chapters past the reader's progress are dropped.
- **Rewrite** (`rewrite.md`). One card plus the optional instruction. Reply: `{"front","back"}`.
- **Definition**. Uses `definitions.md`, the selected term as the highlight text and the passage around it. Cards are `Define <term>` → definition and definition → `<term>`, stripping any "define" the AI already added.

Companion text sent per highlight follows the setting: `Overview + chapter` (default) sends the leading register note, section 1 and this chapter's block; `Whole Companion` sends the full text; `Off` sends nothing. All go through the slicer at the highlight's position with `keep_current_synopsis`, as the plugin does. Connections and key ideas always read the whole Companion, sliced to the book's `percent_finished`.

`ai.py` commands, verified on this machine:

```
claude -p --model <id> --setting-sources "" --strict-mcp-config --disable-slash-commands
       --tools "" --no-session-persistence --output-format json --system-prompt <system>
       (user message on stdin)
codex exec -m <id> --skip-git-repo-check --sandbox read-only --ephemeral
       --ignore-user-config --ignore-rules --disable <each feature in ai.py's _CODEX_OFF>
       -c mcp_servers={} -c web_search="disabled" -c project_doc_max_bytes=0
       -c skills.include_instructions=false -o <file> -
       (system and user joined on stdin, answer read from <file>)
```

Plain `codex exec --sandbox read-only` leaves the model a working shell that can read any file on the Mac, and loads the user's hooks and MCP servers. Codex 0.160.1 has no switch that sends an empty tool list, so the features are turned off one by one. What remains (its `exec` tool, which fails closed with `code_mode_host` off, and sub-agents, which fail under `--ephemeral`) can't run commands, read files or reach the network, checked with a real run. `test_ai.py` checks the list against what the installed Codex really sends to a local stand-in for the API, so a Codex update that adds a tool fails a test.

`--bare` must not be used: it ignores the signed-in account and fails with "Not logged in". For Claude, `is_error` true in the JSON is a failure and `result` is the answer. Both run with the working directory set to an empty temporary folder, without a shell, with a timeout (default 300 s) and can be cancelled. Failures become plain sentences stored on the highlight and shown there, never a raw stack trace.

Assistant detection reports path and version from `--version`. Models: Claude Code `claude-haiku-5-5` Haiku 5.5, `claude-sonnet-5-5` Sonnet 5.5, `claude-opus-5-5` Opus 5.5, `claude-fable-5-1` Fable 5.1; Codex `gpt-6.1-sol` GPT-6.1 Sol (Codex). Menus list only enabled assistants; at least one stays on. Defaults: highlights Sonnet 5.5, connections Opus 5.5.

`runs.py` keeps one run per book, in order: highlight cards, connections, key ideas (when a Companion exists and the box is on). Highlight work runs up to 3 AI calls at a time. Requests carry the plugin's `max` semantics (the AI chooses 1 to n) for bulk runs and `count` for "Suggest N". Runs can pause, resume and stop; finished work is kept. A highlight that already has cards, or is reviewed or skipped, is not requested again. A run interrupted by quitting does not resume by itself; reopening the book offers it again. Picking a book starts a run automatically when the setting is on and nothing has run for this book yet. Each result updates SQLite and pushes an event. Cards that arrive for a highlight that has since been reviewed, skipped or removed from the reader are dropped, for runs, Suggest N and Definition alike. The first row of a run's progress says how many highlights failed, once it has gone through them all.

Key idea chapters are stored in the book's `Chapter N: Title` form when the chapter matches one the book lists, whichever form the AI wrote, and as written otherwise.

## 7. Book Companion (`companion.py`)

- `find(book)` checks beside the book, then `<home>/companions/`, reporting "on the reader" or "on this computer".
- `slice_text` ports `assistant_companion_slicer.lua`, with parity tests against Lua.
- `chapter_block(text, chapter)` matches `###` headings to the chapter by normalised title, ignoring `<!-- pos: -->` markers. An unmatched chapter gets `found: false` and no notes panel.
- Notes panel: before key ideas have run, `big` is the block's first sentence and `points` its bullet lines, untagged. After they run, the AI's `chapters` data replaces it, adding ✓ and ○ and links to key ideas.
- Later research warning comes only from the AI's `later_research` field. It is never inferred from word matching.
- Create: copy the ePub to `<home>/companion-work/<book stem>/<book file name>`, so the skill's beside-the-book copy lands on the Mac and not on the reader. Write a `.command` script there that runs `cd <assistant dir>; claude "/book-companion '<local epub path>'"`, using `shlex.quote` throughout, and start it with `open -a Terminal`. Watch that folder for `<book stem>.companion.md`. Never start it automatically. Create is refused while one is running or a finished file awaits the choice below, so a second click can't open another Terminal or move the finished file aside. The script removes itself when its session ends, which is what ends "running". Show the assistant folder in Settings; if the folder or skill is missing, say so and disable the button.
- When the file appears, ask: **Copy to reader** (proven write, beside the book) or **Keep it on this computer** (copy to `<home>/companions/`).
- Until a Copy to reader is proven, a mark in the book's work folder says the file beside the book can't be trusted, and `find` ignores it. Copy can run again to repair it, even after Keep.

## 8. SQLite (`store.py`)

`<home>/pbanki.db`, WAL mode, one connection per thread, schema version in `PRAGMA user_version`.

- `books(id, device_path, title, author, stem, ext, data_json, last_seen)`: `id` is the first 16 hex digits of SHA-1 of the device path. `data_json` caches chapters and highlights with passages, so the screens work unplugged. A highlight that leaves the reader stays cached while it has draft cards or a draft connection cites it. A settings file that can't be read keeps the cached title and author, so cards stay in the same deck.
- `highlight_state(book_id, hid, key, opened, skipped, error, later_research, chapter_notes_seen)`: `hid` is the first 16 hex digits of SHA-1 of the key, used in URLs.
- `cards(id, book_id, hid, kind, source, front, back, suggested_front, suggested_back, position, state, created_at, updated_at)`. `kind` is `qa|cloze|qonly|def`. `state` is `draft|removed|saved`. Removed cards feed the `rejected` list and restore on Undo, unless their highlight has left the reader.
- `connections(id, book_id, title, front, back, source, sources_json, opened, state)` with `state` as above.
- `ideas(id, book_id, chapter, title, note, quote, front, back, source, opened, state)`.
- `chapter_notes(book_id, chapter, big, points_json)` from key ideas.
- `save_batches(id, book_id, created_at, status, inbox_json, reviewed_json, summary_json)`: `status` is `backed_up|written|verified`.
- `saved_cards(batch_id, hid, front, back, ...)`: for showing what was saved from a highlight.
- `runs(book_id, status, options_json, progress_json, updated_at)`.
- `settings(key, value_json)`.

Editing a card marks AI-written ones `AI, edited`, except that answering a question the AI left blank isn't an edit, same as `wasEdited`. Reviewed status for display is the sidecar's `pbanki_reviewed`, plus keys in the reader's `reviewed.jsonl`, plus keys in batches not yet verified on the reader. With the reader unplugged, or the file failing to read for any reason other than not being there, `reader_reviewed(book_id, key)` keeps its keys as last seen, plus those of batches verified since. Local saved cards do not count on their own, so "Review this book's highlights again" on the reader makes them pending here too, the next time the reader is plugged in.

## 9. Save (`save.py`)

The preview groups exactly what the mock shows: only cards from highlights the user opened, connections opened, and key ideas opened. Blocked while any `qonly` card has no answer. A blank front is not saved. A batch waiting for the reader or failed stays on this screen, as in the mock: its cards and marks are added to whatever is new.

Skipping excludes a highlight's cards. The preview says how many that is, and the save turns them `removed`, since marking the highlight reviewed means no later save could include them.

Inbox lines match `InboxCard` exactly: `front, back, quote, book, author, chapter, page, file, position`, plus `batch`, which pbAnki's parser ignores (check with a Rust test) and which lets a retry detect lines already written.

- Highlight card: same fields as the plugin's `saveCards`, with `position` the `pos0` (xpointer) or page, `file` the device path.
- Connection card: `quote` is one line per source, `Chapter N: Title · p. 19: "text"`; `chapter` is the chapter names joined with ` · `; `page` absent; `position` empty.
- Key idea card: `chapter` set, `quote` empty, `page` absent, `position` empty.

`reviewed.jsonl` lines:

```json
{"v":1,"batch":"<id>","file":"/mnt/ext1/English/X.epub","key":"<datetime>|<pos>","cards":[{"front":"…","back":"…","suggested":{"front":"…","back":"…"},"quote":"…","note":"…"}]}
```

One line per highlight marked: with cards, skipped (`cards: []`), and connection-only sources when the checkbox is on (`cards: []`). Definition cards are left out of `cards`, as the plugin leaves them out of card history.

Pipeline for one batch, each step recorded so a crash resumes safely:

1. Write the batch to SQLite: `backed_up`. Drafts become `saved` here so they can't be saved twice.
2. If the reader is unplugged, stop and show the mock's waiting state. A background watcher continues when the mount appears, and also on app start.
3. Re-read `inbox.jsonl` and `reviewed.jsonl`, and work out the new contents, appending only lines whose batch isn't already there: `written` once `device.write_files` has been handed both files together. Bytes before the first difference are never rewritten, so a write cut off part way can't damage older lines. A retry of a `written` batch also leaves out lines pbAnki has already imported, found by comparing against the notes in a copy of `applications/pbanki/collection/collection.anki2`, since an import removes the inbox and a failed check doesn't stop the user opening pbAnki. A last line that is cut off and isn't valid JSON is dropped. `save` finishes every book's older batches first.
4. `device.write_files` does the remount and read-back: `verified`. Failure reports its plain sentence, with the batch kept safe and a Try again button. The mount watcher holds `device.remount_lock`, so this brief absence isn't shown as unplugged.
5. Refresh the book's status. Report the counts shown on the Saved screen.

Eject uses `diskutil eject <mount>`, and reports failure in plain words ("Something on this Mac is still using the reader").

Plugin update copies `koreader/pbanki.koplugin/**` to the reader's plugin folder under ground rule 2, skipping files whose bytes already match, and leaves `prompts/` on the reader to the plugin itself. Runs only on the button.

## 10. KOReader plugin

In `PbAnki:init`, after settings load, call `self:applyDesktopSaves()`:

1. If `reviewed.jsonl` is missing, stop. This must cost almost nothing, since it runs on every book open.
2. Read it line by line with the pure function `cardgen.parseDesktopSaves(text, file, decode)`, which returns the entries for this book and the remaining raw lines. Malformed lines are kept and logged, never dropped.
3. For this book's entries: set `pbanki_reviewed[key] = true`; add cards via `cardgen.historyEntry` semantics with `appendHistory`, skipping entries already in history (same front, back, quote), so a repeat is harmless. Then `doc_settings:flush()`.
4. Rewrite the file with the remaining lines, to a temp file then `os.rename`. If none remain, delete it.

Wrapped in `pcall` with a logged warning, because a bad file must never stop a book from opening. Add specs to `koreader/spec/` (run with plain `lua`), and prove it end to end in real headless KOReader through `dev/koreader/render.sh`'s Docker setup.

## 11. HTTP API (`server.py`, JSON everywhere)

`hid`, `cid` etc. are opaque ids. Errors are `{ "error": "plain sentence" }` with a 4xx or 5xx status.

```
GET  /api/state                      reader status and checks, settings, assistants, models, prompts
GET  /api/events                     SSE: {"topic":"reader"|"book:<id>"|"prompts"|"settings"}
GET  /api/books                      rows for Pick a book
GET  /api/books/<id>                 full working state: book, chapters, highlights with status and passages,
                                     cards, connections, ideas, companion, run, counts, reader flags
POST /api/books/<id>/open            mark current, refresh from reader, start the automatic run if due
POST /api/books/<id>/run             {action:"start"|"pause"|"resume"|"stop", options?}
POST /api/books/<id>/highlights/<hid>/open
POST /api/books/<id>/highlights/<hid>/skip        {skipped}
POST /api/books/<id>/highlights/<hid>/cards       {kind}, adds a blank card
POST /api/books/<id>/highlights/<hid>/suggest     {count, kind, model, prompt, sees, companion, hint}
POST /api/books/<id>/highlights/<hid>/define      {term, model}
GET  /api/books/<id>/highlights/<hid>/passage?more=N
PATCH  /api/cards/<cid>              {front?, back?}
DELETE /api/cards/<cid>
POST /api/cards/<cid>/restore
POST /api/cards/<cid>/rewrite        {instruction, model}
POST /api/books/<id>/connections/find   {chapters, scope, count, model, prompt, hint}
POST /api/connections/<id>/open
PATCH  /api/connections/<id>         {front?, back?}
DELETE /api/connections/<id>
POST /api/ideas/<id>/open
PATCH  /api/ideas/<id>               {front?, back?}
DELETE /api/ideas/<id>
GET  /api/books/<id>/save            preview, including which highlights get marked and what stays
POST /api/books/<id>/save            {mark_connection_sources}
POST /api/books/<id>/save/retry
POST /api/reader/eject
POST /api/reader/plugin/update
PATCH /api/settings                  {mount?, auto_suggest?, assistants?, model?, meta_model?, companion_send?, ...}
GET  /api/prompts/<name>             files and text
POST /api/prompts/open               {name, file}
POST /api/prompts/reveal             {name, file}
POST /api/books/<id>/companion/create
POST /api/books/<id>/companion/copy
POST /api/books/<id>/companion/keep
```

The server watches the mount every 2 s in one background thread and pushes `reader` events.

One copy of the program uses a data folder at a time, held with a lock the system releases however the program ends. A second copy says where the first one is, opens it in the browser and exits. Closing the Terminal window quits like Ctrl-C. A second Ctrl-C quits at once, ending any AI commands and removing their temporary folders.

## 12. Browser app (`web/`)

Preact + htm from `web/vendor/preact-htm.js`. A single store object holds `state`, `books`, the open `book` and local UI fields. SSE events refetch what changed, without overwriting a field the user is typing in. Edits save with a 300 ms debounce and flush on blur. Screens use the mock's class names and markup, so `companion.css` applies unchanged. The mock's top "Mock controls" bar, its Additions screen, its "Mock:" links, its numbered `ad` badges and its canned data do not ship.

Screens and panels, with mock names: Plug in, Reader check, Pick a book, Suggest cards, Highlights, Connections, Key ideas, Save cards, Saved, plus drawers Prompt file, Settings, Create a Book Companion. Every visible behaviour in the mock's "What I added" list ships: automatic start, resume unplugged, Skip, card kinds, Show more, notes, how much the AI reads, reader preview with Question/Answer/Context, card checks, Rewrite, all-highlights scope, the save review, Eject, keyboard shortcuts (J, K, ⌘↵, X, ignored while typing), Companion panel, later research warning, Key ideas, Create, read back.

The app routes by URL hash so reload keeps your place. Layout works from 1100 px wide up; narrower windows stack rather than clip. Empty, loading and failure states use the mock's voice.

`checks.py`, surfaced as `warn` on a card: a front containing a standalone `this|that|these|those|such|it` as the mock describes ("leans on this and may not stand alone"), and two cards on a highlight with Jaccard word overlap of 0.7 or more ("asks nearly the same thing as another card"). Cloze cards skip the first check, because the prompt allows pointing words when the back names them.

## 13. Testing

- Unit tests per module with `unittest`. Parity tests against the Lua originals for `cardgen`, `prompts.normalize` and the slicer.
- **Real fixtures, not guesses:** generate a sidecar with real KOReader in Docker (`dev/koreader`, existing image and `target/koreader-emulator/book.epub`), creating highlights in that real ePub, and keep it as `tests/fixtures/real_sidecar/` so the Lua parser, keys, xpointers and passage lookup are tested against what KOReader really writes.
- Fake reader builder: a folder tree with `history.lua`, books, sidecars and a Companion. Fake `claude` and `codex` scripts that return valid JSON, malformed JSON, errors and slow replies on request.
- End to end: Playwright drives the real server against the fake reader through the complete flow, unplugged and replugged, then asserts on the bytes of `inbox.jsonl` and `reviewed.jsonl` and that nothing else on the fake reader changed.
- One real smoke run against each AI command on one highlight.
- Real pbAnki import: a Rust test feeding a produced `inbox.jsonl` through `import_inbox` (key idea cards with an empty quote, connection cards, cloze, the extra `batch` field).
- Real plugin pickup: headless KOReader opens a book with a produced `reviewed.jsonl` and we assert `pbanki_reviewed` and history in the resulting sidecar.
- Proof page of numbered screenshots, published as an artifact, per the project's habit.

## 14. Build order

1. Plugin change and real KOReader fixture.
2. Reader and ePub layer; AI layer; store and save, in parallel on separate files, against the interfaces above.
3. Service and server.
4. Browser app.
5. Independent reviews, then fixes.
6. End-to-end run, visual comparison with the mock, real AI smoke, proof page, README.

## 15. Not in this version

- Editing cards already in pbAnki's collection.
- Writing to `.sdr` files, even though it would make reviewed status take effect immediately.
- Cards with images.
- Finding a reader mounted anywhere other than the configured path, beyond a Choose folder field.
- Windows and Linux. Python parts are portable, but `open`, `diskutil` and Terminal launching are macOS only.
