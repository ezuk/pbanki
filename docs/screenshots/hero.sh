#!/bin/sh
# Builds hero.png, the README's opening strip, from existing screenshots.
# Run after render.sh and dev/koreader/render.sh. Needs ImageMagick 7.
set -e
cd "$(dirname "$0")"
font=../../ui/fonts/JetBrainsMono-Bold.ttf
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

panel() {
    magick "$2" -resize 380x -bordercolor '#1b1b19' -border 2 \
        \( -size 384x64 xc:white -font "$font" -pointsize 22 -fill '#1b1b19' \
            -gravity west -annotate +4+0 "$3" \) \
        +swap -gravity center -append "$tmp/$1.png"
}
panel 1 koreader/01_highlight_menu.png "1  Highlight in KOReader"
panel 2 koreader/04_suggested.png "2  AI writes the cards"
panel 3 koreader_question.png "3  Study in pbAnki"
panel 4 context.png "4  See where it came from"
magick -size 40x580 xc:white -font "$font" -pointsize 34 -fill '#6b6a64' \
    -gravity center -annotate +0+30 "›" "$tmp/arrow.png"
magick "$tmp/1.png" "$tmp/arrow.png" "$tmp/2.png" "$tmp/arrow.png" "$tmp/3.png" \
    "$tmp/arrow.png" "$tmp/4.png" -background white -gravity center +append \
    -bordercolor white -border 24 +repage -depth 8 hero.png
echo "Wrote docs/screenshots/hero.png"
