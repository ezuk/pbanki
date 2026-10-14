# pbAnki

Anki flashcard app for PocketBook e-readers.

## What is this?

Port of Anki to PocketBook devices using:
- Anki's official rslib backend for full collection compatibility
- Slint UI framework with software rendering for e-ink displays
- inkview-rs bindings for PocketBook SDK integration

## Building

Requires Rust (the version is pinned in `rust-toolchain.toml`) and `protoc`
(`brew install protobuf` on macOS).

The repo is a Cargo workspace:
- `common/`: shared app logic, Anki backend, and Slint UI
- `dev/`: desktop build for development
- root crate: the PocketBook app

### Desktop (for development)

```bash
cd dev
cargo run
```

Place your Anki collection at `./dev/pbanki/collection/`

### Checks

```bash
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo test
```

### PocketBook

```bash
make          # debug build
make release  # release build
```

Requires `cargo-zigbuild` (`cargo install --locked cargo-zigbuild`), `zig`,
and the `armv7-unknown-linux-gnueabi` Rust target.

## Installation

With the device connected over USB (it mounts as `PB629` or similar):

1. Copy `target/armv7-unknown-linux-gnueabi/release/pbanki` to `applications/pbanki/pbanki`
2. Copy the launcher `pocketbook/pbanki.app` to `applications/pbanki.app`
3. Copy your Anki collection to `applications/pbanki/collection/collection.anki2`
4. (Optional) Create configuration file at: `applications/pbanki/config.toml`
5. Launch from Applications menu

The launcher writes the app's output to `applications/pbanki/log.txt`.

For a test collection, run `cargo run -p common --example sample_collection -- <dir>`.

## Making cards in KOReader

Copy `koreader/pbanki.koplugin` to `applications/koreader/plugins/` and restart KOReader.
Highlight text, then tap **Make Anki card**. Saved cards are queued in
`applications/pbanki/inbox.jsonl`, and pbAnki imports them into the **KOReader**
deck when it starts or when you return to the deck list.

## Configuration

pbAnki uses a TOML configuration file. On first launch, a default config will be created at `/mnt/ext1/applications/pbanki/config.toml` (or `./pbanki/config.toml` for desktop builds).

### Configuration Options

```toml
[general]
# Collection path (relative to app directory or absolute)
collection_path = "/mnt/ext1/applications/pbanki/collection"

[display]
# Card text size in screen pixels
font_size = 20

[ankiweb]
# AnkiWeb synchronization settings
# Leave empty to disable sync

# AnkiWeb username (email)
username = ""

# Password (plain text - will be encrypted in future versions)
# WARNING: Do not share this file if password is filled
password = ""

# Session token (populated after successful login)
# This avoids storing password long-term
token = ""

# Sync automatically on app start
auto_sync = false

# Sync automatically after session ends
sync_on_exit = false
```

## Project Structure

```
src/        # PocketBook build (uses inkview-rs)
common/     # Shared app logic: Anki backend, card layout, KOReader inbox
dev/        # Desktop build and the PocketBook screen emulator
ui/         # Slint UI and the bundled JetBrains Mono font
koreader/   # KOReader plugin for making cards from highlights
pocketbook/ # Device launcher script
```

### Emulator

`cargo run -p dev --bin emulator -- <app dir> <output dir> <steps...>` renders the app
as it appears on a PocketBook Verse (758×1024, 3× scale, 16 grays) and saves PNG
screenshots. Steps are `shot:<name>`, `tap:<x>,<y>`, `swipe:<x>,<y1>,<y2>` and
`key:<left|right|up|down|pageup|pagedown|menu|return>`.

## Current Features

- Deck list with new/learn/due counts; collapse state is saved like desktop Anki
- Cards laid out in JetBrains Mono, paged to fit the screen exactly
- Bold, italic, line breaks, lists, cloze highlights and the front/back divider
- Type-in cards show the expected answer
- Page turns by tapping the left/right third, swiping, or the page buttons
- Again/Hard/Good/Easy with intervals
- Cards made in KOReader, with a Context screen showing the quote and its source
- Configurable collection path

## TODOs

- [ ] **AnkiWeb Sync** - Implement synchronization with AnkiWeb
- [ ] **MathJax Rendering** - Add support for LaTeX/MathJax formulas in cards
- [ ] **Image Support** - Display images embedded in cards from media folder
- [ ] **UI Polishing** - Improve e-ink optimized interface and user experience

## License

See [LICENSE](LICENSE)
