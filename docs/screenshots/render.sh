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
expand=42,242
koreader=300,182
basics=300,302
card_types=300,362
languages=300,422
show=379,946
good=489,946
easy=678,946
context=678,850
settings=640,50
username=490,183
password=490,252
validate=627,320

emulate shot:decks tap:$expand shot:decks_expanded
emulate tap:$koreader shot:koreader_question tap:$show shot:koreader_answer \
    tap:$context shot:context tap:150,946 tap:$easy shot:finished
emulate tap:$expand tap:$basics shot:question tap:$show shot:answer \
    tap:$good shot:formatting_question tap:$show shot:formatting_answer \
    tap:$good tap:$show shot:long_card
emulate tap:$expand tap:$card_types shot:cloze_question tap:$show shot:cloze_answer
emulate tap:$expand tap:$languages tap:$show shot:accents
emulate tap:$settings shot:settings \
    type:reader@example.com tap:$username type:wrong tap:$password shot:login_edited \
    tap:$validate wait:300 shot:login_failed \
    type:correct-password tap:$password tap:$validate wait:300 shot:login_passed

# A file where the collection folder should be
mkdir "$tmp/broken"
touch "$tmp/broken/collection"
target/debug/emulator "$tmp/broken" "$out" shot:error >/dev/null

echo "Screenshots written to $out"
