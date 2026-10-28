#!/bin/sh
# Regenerates the README screenshots with the PocketBook emulator.
# Uses the sample collection plus fixture/inbox.jsonl (one KOReader card).
# Tap coordinates are device pixels on the 758x1024 screen.
set -e
cd "$(dirname "$0")/../.."
out=docs/screenshots
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cargo build -q -p dev --bin emulator
cargo run -q -p common --example sample_collection -- "$tmp/app/collection" >/dev/null
cp "$out/fixture/inbox.jsonl" "$tmp/app/"

emulate() {
    target/debug/emulator "$tmp/app" "$out" "$@" >/dev/null
}

# Deck list rows, the study screen's button row and the settings controls
everything=300,182
expand=42,302
koreader=300,242
basics=300,362
card_types=300,422
languages=300,482
show=379,946
good=489,946
easy=678,946
context=678,850
edit=295,850
edit_next=560,542
settings=555,50
username=490,183
password=490,252
validate=627,320
dark=626,705
settings_save=564,944
# Keyboard keys used by the edit screens
key_e=189,743
key_h=417,823
key_a=38,823
key_v=379,903
key_sym=57,983
key_backspace=701,903

emulate shot:decks tap:$expand shot:decks_expanded
emulate tap:$everything shot:everything
emulate tap:42,242 shot:decks_koreader
emulate tap:$koreader shot:koreader_question tap:$show shot:koreader_answer \
    tap:$context shot:context tap:150,946 tap:$easy shot:finished
# Holds e for its popup, closes it on an empty cell, then changes "want?" to "have?"
emulate tap:42,242 tap:$koreader tap:$edit shot:edit_front \
    hold:$key_e shot:keyboard_popup tap:113,743 tap:$key_sym shot:keyboard_symbols tap:$key_sym \
    tap:$key_backspace swipe:701,903,630,903 tap:$key_h tap:$key_a tap:$key_v tap:$key_e \
    swipe:152,983,90,983 shot:edit_typed tap:$edit_next shot:edit_back tap:$edit_next shot:edited
emulate tap:$expand tap:$basics tap:$show tap:$good tap:$edit shot:edit_formatted
emulate tap:$expand tap:$basics shot:question tap:$show shot:answer \
    tap:$good shot:formatting_question tap:$show shot:formatting_answer \
    tap:$good tap:$show shot:long_card
emulate tap:$expand tap:$card_types shot:cloze_question tap:$show shot:cloze_answer
emulate tap:$expand tap:$languages tap:$show shot:accents
emulate tap:$settings shot:settings \
    type:reader@example.com tap:$username type:wrong tap:$password shot:login_edited \
    tap:$validate wait:300 shot:login_failed \
    type:correct-password tap:$password tap:$validate wait:300 shot:login_passed
emulate tap:$settings tap:$dark shot:settings_dark tap:$settings_save shot:decks_dark \
    tap:$expand tap:$basics tap:$show shot:answer_dark tap:$edit shot:edit_dark
PBANKI_EMULATOR_12H=1 target/debug/emulator "$tmp/app" "$out" tap:$everything shot:clock_12h >/dev/null

# AnkiWeb sync, against the emulator's stand-in for AnkiWeb
sync=378,50
toggle=626,388
save=564,944
keep_ankiweb=194,256
replace=550,256
login="tap:$settings type:reader@example.com tap:$username type:correct-password tap:$password tap:$validate wait:300"
fresh() {
    rm -rf "$tmp/sync"
    mkdir -p "$tmp/sync"
    cp -R "$tmp/app/collection" "$tmp/sync/"
}
fresh
target/debug/emulator "$tmp/sync" "$out" tap:$sync wait:100 shot:sync_logged_out >/dev/null
# shellcheck disable=SC2086
target/debug/emulator "$tmp/sync" "$out" $login tap:$toggle shot:settings_sync tap:$save \
    tap:$sync wait:200 shot:sync_running wait:1500 shot:sync_done >/dev/null
# Sync on startup runs as the app opens
cat > "$tmp/sync/config.toml" <<'TOML'
[general]
collection_path = ""
[display]
font_size = 20
[ankiweb]
username = "reader@example.com"
password = "correct-password"
token = "emulator-token"
auto_sync = true
TOML
target/debug/emulator "$tmp/sync" "$out" wait:600 shot:sync_startup >/dev/null
fresh
# shellcheck disable=SC2086
PBANKI_EMULATOR_SYNC=full target/debug/emulator "$tmp/sync" "$out" $login tap:$save \
    tap:$sync wait:1500 shot:sync_full tap:$keep_ankiweb shot:sync_confirm \
    tap:$replace wait:200 shot:sync_downloading wait:3000 shot:sync_downloaded >/dev/null
fresh
# shellcheck disable=SC2086
PBANKI_EMULATOR_SYNC=fail target/debug/emulator "$tmp/sync" "$out" $login tap:$save \
    tap:$sync wait:1500 shot:sync_failed >/dev/null

# A file where the collection folder should be
mkdir "$tmp/broken"
touch "$tmp/broken/collection"
target/debug/emulator "$tmp/broken" "$out" shot:error >/dev/null

echo "Screenshots written to $out"
