# pbAnki

**Turn what you read into flashcards, and study them on the same e-reader.**

pbAnki is two pieces that work together on a PocketBook e-reader. A KOReader plugin turns your highlights into flashcards, with an AI writing them for you if you want. The pbAnki app is Anki, rebuilt for e-ink, where you study those cards with Anki's real scheduler. Everything syncs with AnkiWeb, so the same cards show up in Anki on your phone and computer.

<p align="center"><img src="docs/screenshots/hero.png" alt="Four screens: highlight a passage in KOReader, the AI suggests two cards, study one in pbAnki, then see the passage it came from"></p>

Most people who read seriously have the same problem. You highlight a passage because it matters, and a month later you can't remember what it said. Spaced repetition fixes that, but writing cards is slow, and doing it on a phone or laptop means leaving the book. With pbAnki you never leave the reader. Highlight, tap **Create flashcard**, keep the cards you like, and keep reading. Next time you open pbAnki, they're waiting in a deck named after the book, and each one can show you the passage it came from.

Built and tested on a **PocketBook Verse (PB629)**, firmware 6.11. Every screenshot here is a real render at that screen's resolution (758 × 1024, 16 shades of grey): the app's from the emulator in this repo, and the plugin's from KOReader itself running headless.

pbAnki started as a fork of [ananyatimalsina/pbanki](https://github.com/ananyatimalsina/pbanki), which got Anki's scheduler running on PocketBook. This fork rebuilds the study screens for e-ink, adds the KOReader plugin and tests, and fixes sync.

## Contents

- [Part 1: making cards in KOReader](#part-1-making-cards-in-koreader)
- [Part 2: studying in pbAnki](#part-2-studying-in-pbanki)
- [Install on a PocketBook](#install-on-a-pocketbook)
- [Building](#building)

## Part 1: making cards in KOReader

### From one highlight

Select a passage in [KOReader](https://koreader.rocks) and tap **Create flashcard**. KOReader saves it as a normal highlight, asks how many cards you want, and opens the card editor. Tap a card to change it, ✕ to drop it, or **+ Card** to write your own.

<table>
  <tr>
    <td><img src="docs/screenshots/koreader/01_highlight_menu.png" width="240" alt="KOReader's highlight menu with a Create flashcard button"></td>
    <td><img src="docs/screenshots/koreader/02_card_count.png" width="240" alt="Choosing how many cards to suggest"></td>
    <td><img src="docs/screenshots/koreader/04_suggested.png" width="240" alt="The card editor with two AI-suggested cards"></td>
  </tr>
  <tr>
    <td>Highlight, then Create flashcard.</td>
    <td>Pick how many cards (1 by default).</td>
    <td>The AI's suggestions, ready to edit or save.</td>
  </tr>
</table>

### An AI that writes cards the way you do

Suggestions come from the [AI Assistant](https://github.com/omer-faruq/assistant.koplugin) plugin, using whichever model you choose. The prompt is tuned for good flashcards. Questions are short and specific, answers are a few words, each card asks about something different, and a list in the text becomes one card that asks for the list.

The AI reads more than the highlight. It sees the book, the chapter, about 150 words on each side of the passage, and the note you left on the highlight, which steers what it asks about. It also sees the last cards you saved from the same book, including how you rewrote its earlier suggestions, and copies your style. Tap **Suggest more** to add up to 10 more cards below the ones you have. It avoids repeating what you kept and learns from the ones you removed.

### A whole book's highlights in one sitting

If you'd rather keep reading and make cards later, highlight as usual. Then open **Tools → pbAnki flashcards → Flashcards from highlights**. The plugin walks through every highlight you haven't reviewed yet, one screen each. Save the cards or skip the highlight. Here the AI decides how many cards each highlight deserves: one for a focused passage, more for a dense one, up to a limit you set. The next highlight's suggestions load in the background while you work on the current one, so you rarely wait.

<table>
  <tr>
    <td><img src="docs/screenshots/koreader/09_menu.png" width="240" alt="The pbAnki flashcards menu in KOReader"></td>
    <td><img src="docs/screenshots/koreader/10_batch_first.png" width="240" alt="Flashcards from highlights: highlight 1 of 3 with suggestions"></td>
    <td><img src="docs/screenshots/koreader/14_batch_done.png" width="240" alt="All highlights reviewed, with the number of cards saved"></td>
  </tr>
  <tr>
    <td>The plugin's menu.</td>
    <td>Highlight 1 of 3, with its cards.</td>
    <td>Done: every highlight reviewed.</td>
  </tr>
</table>

The plugin remembers which highlights you've reviewed in the book's own KOReader settings, so each one comes up once. The menu can make them all new again.

### Cloze cards

A cloze card is a sentence with a gap: "A single man of large fortune must be in want of a [...]". Tick **Cloze** when you ask for suggestions and the AI writes these instead of questions. For batch mode, turn on **Suggest cloze cards** in the menu.

You can make them by hand too. On the card's front, the **{{…}} Cloze** button opens a gap at the cursor. Type the hidden words and tap it again to close the gap. In pbAnki they become regular Anki cloze cards that still remember their book and passage.

<table>
  <tr>
    <td><img src="docs/screenshots/koreader/20_cloze_on.png" width="240" alt="The Cards to suggest dialog with Cloze ticked"></td>
    <td><img src="docs/screenshots/koreader/23_cloze_button_closed.png" width="240" alt="Writing a card front with the Cloze button"></td>
    <td><img src="docs/screenshots/koreader/25_cloze_cards.png" width="240" alt="Cloze cards in the card editor, hidden parts in brackets"></td>
  </tr>
  <tr>
    <td>Tick Cloze before you ask.</td>
    <td>The Cloze button writes the marks for you.</td>
    <td>AI and hand-written cloze cards together.</td>
  </tr>
</table>

### Offline, or without AI

Everything works without AI. Turn suggestions off (in flight mode, say) and the editor opens straight onto a blank card: the front on one full screen with a large keyboard, then the back on the next.

<table>
  <tr>
    <td><img src="docs/screenshots/koreader/16_ai_off.png" width="240" alt="Writing a card's front by hand"></td>
    <td><img src="docs/screenshots/koreader/17_ai_off_back.png" width="240" alt="Writing the card's back, with the front shown above"></td>
  </tr>
</table>

### From KOReader to pbAnki

Saved cards go into a small inbox file. Next time pbAnki opens, it files them under **KOReader**, one sub-deck per book, such as `KOReader::Pride and Prejudice`. Study the KOReader deck to mix every book, or open one book's deck. Each card keeps the full highlighted passage, the book, author, chapter and page, and the **Context** button on the answer shows them.

<table>
  <tr>
    <td><img src="docs/screenshots/decks_koreader.png" width="240" alt="The KOReader deck with a sub-deck for Pride and Prejudice"></td>
    <td><img src="docs/screenshots/koreader_answer.png" width="240" alt="A KOReader card's answer, with a Context button"></td>
    <td><img src="docs/screenshots/context.png" width="240" alt="The context screen: the quote, then book, author, chapter and page"></td>
  </tr>
  <tr>
    <td>One deck per book.</td>
    <td>A card from KOReader.</td>
    <td>Context: where it came from.</td>
  </tr>
</table>

## Part 2: studying in pbAnki

pbAnki runs Anki's own Rust backend, so scheduling, deck options and sync behave as they do in desktop Anki. The screens are new and drawn for a 6-inch e-ink panel.

<table>
  <tr>
    <th>Before</th>
    <th>pbAnki now</th>
  </tr>
  <tr>
    <td><img src="docs/screenshots/before/answer.png" width="300" alt="Original answer screen: question and answer run together and the Easy button is cut off"></td>
    <td><img src="docs/screenshots/answer.png" width="300" alt="New answer screen: question, divider, answer, and four rating buttons that fit"></td>
  </tr>
</table>

### Text sized for e-ink

The original UI rendered at three times the intended size, so about four short lines fit on a card and long cards spread over mostly empty pages. Text is now set in [JetBrains Mono](https://www.jetbrains.com/lp/mono/) at 20 px, adjustable from 12 to 40 px in Settings. Every character has the same width, which lets pbAnki work out exactly how much fits on a page: 59 × 24 characters at the default size.

<table>
  <tr>
    <th>Before</th>
    <th>pbAnki now</th>
  </tr>
  <tr>
    <td><img src="docs/screenshots/before/question.png" width="300" alt="Original question screen"></td>
    <td><img src="docs/screenshots/question.png" width="300" alt="New question screen"></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/before/long_card.png" width="300" alt="Original: one sentence split over two mostly empty pages"></td>
    <td><img src="docs/screenshots/long_card.png" width="300" alt="New: a long answer fills a single page"></td>
  </tr>
</table>

### Cards that keep their formatting

The original stripped cards to plain text. Question and answer ran together ("What is the capital of France? Paris") and line breaks vanished ("novelPride and Prejudice"). pbAnki lays out the card's HTML, keeping bold, italic, line breaks, paragraphs, lists and the divider between front and back. Cloze gaps are bold. Cards sit in the middle of the screen, and the answer opens on the page where the back begins.

<table>
  <tr>
    <td><img src="docs/screenshots/formatting_answer.png" width="240" alt="Bold, italic and a line break preserved"></td>
    <td><img src="docs/screenshots/cloze_question.png" width="240" alt="Cloze card with the active gap in bold"></td>
    <td><img src="docs/screenshots/cloze_answer.png" width="240" alt="Cloze answer with the revealed word in bold"></td>
  </tr>
</table>

### A deck list you can read at a glance

Columns are labelled, due counts are bold and zeros are grey. Every row is a full-width tap target. Collapsing a deck hides its sub-decks and is saved in the collection, as in desktop Anki. **Everything**, at the top, studies all your decks in one session, going through the top-level decks in list order with each deck's own daily limits.

<table>
  <tr>
    <th>Before</th>
    <th>pbAnki now</th>
    <th>Studying Everything</th>
  </tr>
  <tr>
    <td><img src="docs/screenshots/before/decks_expanded.png" width="240" alt="Original deck list"></td>
    <td><img src="docs/screenshots/decks_expanded.png" width="240" alt="New deck list with labelled columns and an Everything row"></td>
    <td><img src="docs/screenshots/everything.png" width="240" alt="Studying Everything, with the total of all decks"></td>
  </tr>
</table>

### Studying

- Tap the question to see the answer, then tap the answer to rate it Good. Again, Hard and Easy have their own buttons. On a long answer, a tap turns the page first.
- Rated too fast? Swipe from right to left, or press the **prev** button, to undo. Undo also brings back a card you just suspended.
- The **next** button does what a tap does. **Prev** turns back a page, or undoes on a card's first page.
- **Suspend** takes a card out of review. **Edit** fixes a typo without losing the card's schedule. You get the front on one screen and the back on the next, typed with the PocketBook keyboard.
- Buttons are outlined and invert while pressed, which shows clearly on e-ink.
- Type-in cards are skipped (buried until tomorrow), because typing answers on e-ink is no fun.

<table>
  <tr>
    <td><img src="docs/screenshots/edit_front.png" width="240" alt="Edit card: the front on its own screen"></td>
    <td><img src="docs/screenshots/edit_back.png" width="240" alt="Edit card: the back on its own screen, with Save"></td>
    <td><img src="docs/screenshots/edited.png" width="240" alt="The study screen showing the edited question"></td>
  </tr>
</table>

### Sync with AnkiWeb

Tap **Sync** on the deck list, or turn on **Sync on startup**. pbAnki turns on Wi-Fi if it's off and shows each step in a status bar. Cards from KOReader are imported first, so they go up in the same sync and appear in Anki on your other devices.

If AnkiWeb can't merge the two collections (after a reset on another device, for example), pbAnki asks which one to keep and confirms before replacing anything. Before AnkiWeb's copy replaces the reader's, the reader's collection is backed up to `collection/backups/before-download-<time>.colpkg`, which desktop Anki can import. Media files aren't synced yet.

<table>
  <tr>
    <td><img src="docs/screenshots/sync_running.png" width="240" alt="Syncing with AnkiWeb"></td>
    <td><img src="docs/screenshots/sync_done.png" width="240" alt="Synced with AnkiWeb"></td>
    <td><img src="docs/screenshots/sync_full.png" width="240" alt="AnkiWeb asks which collection to keep"></td>
  </tr>
</table>

### Settings

Text size has a live preview. You type your AnkiWeb login on the PocketBook keyboard, and **Validate** tries it against AnkiWeb before you can save. A wrong password shows up right there, not at your first sync.

<table>
  <tr>
    <td><img src="docs/screenshots/settings.png" width="240" alt="Settings screen"></td>
    <td><img src="docs/screenshots/login_failed.png" width="240" alt="Validate failed: AnkiWeb rejected the username or password"></td>
    <td><img src="docs/screenshots/login_passed.png" width="240" alt="Validate passed: Save is enabled"></td>
  </tr>
</table>

### Under the hood

- One Cargo workspace with a single lock file. The Anki dependency is pinned to an exact commit.
- 67 Rust tests, covering card layout, config, the KOReader inbox and the study flow against real Anki collections, plus a spec for the plugin's card prompts. The original had none.
- CI checks formatting, runs Clippy and the tests, and cross-builds for the PocketBook on every push.
- Fixes to the original: sync could never log in with the default config (an empty token counted as a real one), settings were always saved to the device path, the sync status got stuck after a failure, and a page size of 0 crashed the app.
- The launcher works around firmware 6.11 refusing to load `libinkview.so`.
- If the collection can't be opened, pbAnki says why instead of quitting.

## Install on a PocketBook

Connect the e-reader over USB; it mounts as a drive (for example `PB629`).

1. Build the app (see [Building](#building)) or download a release.
2. Copy `target/armv7-unknown-linux-gnueabi/release/pbanki` to `applications/pbanki/pbanki`.
3. Copy the launcher `pocketbook/pbanki.app` to `applications/pbanki.app`.
4. Copy your Anki collection to `applications/pbanki/collection/collection.anki2`.
5. Open **pbAnki** from the Applications menu.

The launcher appends the app's output to `applications/pbanki/log.txt`, which is the first place to look if something goes wrong.

### Menu icon

Not ready yet. Firmware 6.x lists apps from `system/config/desktop/view.json`, and the icon files are in `pocketbook/icons/`, but the entries tried so far stopped apps from launching. Leave `view.json` alone for now; pbAnki shows up in the Applications menu without an icon.

### KOReader plugin

Copy `koreader/pbanki.koplugin` to `applications/koreader/plugins/` and restart KOReader. Highlight text, then tap **Create flashcard**, or open **Tools → pbAnki flashcards → Flashcards from highlights**. The same menu turns AI suggestions and cloze suggestions on and off, sets the most cards the AI may write per highlight in batch mode (1 to 10, default 3), picks the AI model, and can make all of a book's highlights new again. Batch mode can also be put on a gesture.

Cards are queued in `applications/pbanki/inbox.jsonl` and imported when pbAnki starts or when you return to the deck list. If a line can't be imported, it is kept in `inbox.failed.jsonl` rather than dropped.

AI suggestions need the AI Assistant plugin with a version that has the flashcards API (`queryFlashcards`). The AI model menu opens Assistant's own provider and model picker; until you pick one, Assistant's main provider is used. When a suggestion is needed and the reader is offline, Wi-Fi is turned on.

## Configuration

pbAnki creates `applications/pbanki/config.toml` on first launch (`./pbanki/config.toml` in the desktop build). The Settings screen edits it for you.

```toml
[general]
collection_path = "/mnt/ext1/applications/pbanki/collection"

[display]
# Card text size in screen pixels, 12 to 40
font_size = 20

[ankiweb]
username = ""
# Stored in plain text: don't share this file once it's filled in
password = ""
# Session token, saved when Validate passes
token = ""
# Sync when the app starts (Settings: Sync on startup)
auto_sync = false
sync_on_exit = false
```

## Building

Requires Rust (the version is pinned in `rust-toolchain.toml`) and `protoc` (`brew install protobuf` on macOS).

```bash
# Desktop build, for development
cd dev && cargo run          # uses ./pbanki/collection/

# PocketBook build
make release                 # needs zig, cargo-zigbuild and the armv7-unknown-linux-gnueabi target

# Checks
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo test
make clippy-device           # lints the PocketBook-only code
```

For a sample collection with a few cards of each kind: `cargo run -p common --example sample_collection -- <dir>`.

### Emulator

`cargo run -p dev --bin emulator -- <app dir> <output dir> <steps...>` runs the real UI headlessly as it appears on a PocketBook Verse and saves PNG screenshots. Steps are `shot:<name>`, `tap:<x>,<y>`, `swipe:<x1>,<y1>,<x2>,<y2>`, `key:<left|right|up|down|pageup|pagedown|menu|return>`, `type:<text>` (what the next keyboard prompt returns) and `wait:<ms>`.

`docs/screenshots/render.sh` regenerates every screenshot in this README. The "before" images in `docs/screenshots/before/` were rendered from the original code and are kept as they are.

`dev/koreader/render.sh` regenerates the KOReader screenshots in `docs/screenshots/koreader/`. It runs the official KOReader Linux release headless in Docker at the PocketBook's screen size, with this plugin installed, and plays `dev/koreader/screens.lua`: a list of steps that select text, press buttons and take screenshots. Set `ASSISTANT_DIR` to an AI Assistant checkout with a `configuration.lua` to include real AI suggestions.

## Project structure

```
src/         PocketBook entry point (inkview-rs, keyboard, Wi-Fi)
common/      App logic: Anki backend, card layout, KOReader inbox, settings
dev/         Desktop build, the emulator and the KOReader screenshot runner
ui/          Slint UI and the bundled JetBrains Mono font
koreader/    KOReader plugin and its specs (lua koreader/spec/cardgen_spec.lua)
pocketbook/  Device launcher, app icon and icon build script
docs/        README screenshots and the script that renders them
```

## Roadmap

- [x] AnkiWeb sync of the collection
- [ ] Media sync
- [ ] Open a card's source passage in KOReader
- [ ] Close the collection cleanly when the firmware closes the app
- [ ] Applications menu icon
- [ ] Images from the collection's media folder
- [ ] MathJax / LaTeX

## Credits and license

- Original pbAnki by [Ananya Timalsina](https://github.com/ananyatimalsina/pbanki).
- Built on [Anki](https://apps.ankiweb.net)'s Rust backend, [Slint](https://slint.dev) and [inkview-rs](https://github.com/ananyatimalsina/inkview-rs).
- [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) is bundled under the SIL Open Font License (`ui/fonts/OFL.txt`).

pbAnki is released under the [MIT License](LICENSE).
