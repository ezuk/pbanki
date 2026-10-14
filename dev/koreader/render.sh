#!/bin/sh
# Runs the KOReader plugin headless and saves screenshots to docs/screenshots/koreader.
# Needs Docker. Set ASSISTANT_DIR to an AI Assistant plugin checkout with a
# configuration.lua for real AI suggestions; without it, AI is unavailable.
# Usage: dev/koreader/render.sh [script.lua]   (default: screens.lua)
set -e
cd "$(dirname "$0")/../.."
repo=$(pwd)
here=dev/koreader
cache=target/koreader-emulator
version=v2026.07.1
script=${1:-screens.lua}
out=docs/screenshots/koreader
mkdir -p "$cache/shots" "$out"

case $(uname -m) in arm64|aarch64) arch=arm64 ;; *) arch=x86_64 ;; esac
if [ ! -d "$cache/koreader" ]; then
    gh release download "$version" -R koreader/koreader -D "$cache" -p "koreader-linux-$arch-$version.tar.xz"
    mkdir -p "$cache/unpacked" && tar xJf "$cache"/koreader-linux-*.tar.xz -C "$cache/unpacked"
    mv "$cache/unpacked/lib/koreader" "$cache/koreader"
fi
[ -f "$cache/book.epub" ] || curl -sL -o "$cache/book.epub" https://www.gutenberg.org/ebooks/1342.epub.noimages
docker image inspect pbanki-koreader >/dev/null 2>&1 || docker build -q -t pbanki-koreader "$here"

assistant_mount=
[ -n "$ASSISTANT_DIR" ] && assistant_mount="-v $ASSISTANT_DIR:/plugins/assistant.koplugin:ro"
find "$cache/shots" -name '*.png' -delete

# shellcheck disable=SC2086
docker run --rm \
    -v "$repo/$cache/koreader:/ko-ro:ro" \
    -v "$repo/$here/uxdriver.koplugin:/plugins/uxdriver.koplugin:ro" \
    -v "$repo/koreader/pbanki.koplugin:/plugins/pbanki.koplugin:ro" \
    $assistant_mount \
    -v "$repo/$here:/work:ro" -v "$repo/$cache/book.epub:/book/book.epub:ro" \
    -v "$repo/$cache/shots:/shots" \
    -e UX_SCRIPT="/work/$script" -e UX_DELAY=1.5 \
    pbanki-koreader sh -c '
        cp -r /ko-ro /ko && cp -r /plugins/* /ko/plugins/ && rm -rf /ko/plugins/assistant.koplugin/tests
        cp /work/settings.reader.lua /ko/settings.reader.lua && cp /book/book.epub /tmp/book.epub
        mkdir -p /app/pbanki
        cd /ko && PBANKI_DIR=/app/pbanki EMULATE_READER_W=758 EMULATE_READER_H=1024 EMULATE_READER_DPI=212 \
            timeout 600 xvfb-run -a -s "-screen 0 758x1024x24" ./reader.lua /tmp/book.epub 2>&1 \
            | grep -E "uxdriver|ERROR" || true'

# 16 grey levels, as on the PocketBook panel
for f in "$cache"/shots/*.png; do
    python3 -I -c 'import sys; from PIL import Image; Image.open(sys.argv[1]).convert("L").point(lambda v: v // 17 * 17).save(sys.argv[2], optimize=True)' "$f" "$out/$(basename "$f")"
done
echo "Screenshots written to $out"
