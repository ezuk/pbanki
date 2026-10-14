#!/bin/sh
# Builds the Applications menu icons from icon.svg.
# Requires resvg and ImageMagick. The firmware draws icons at their pixel size and
# reads only uncompressed BMPs; built-in icons are about 50 px across.
set -e
cd "$(dirname "$0")"
SIZE=52
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p icons
resvg -w $SIZE -h $SIZE icon.svg "$tmp/icon.png"
magick "$tmp/icon.png" -background white -flatten \
    -colorspace Gray -type Grayscale -depth 8 "$tmp/normal.png"

magick "$tmp/normal.png" -compress None BMP3:icons/pbanki.bmp
magick "$tmp/normal.png" -negate -compress None BMP3:icons/pbanki_f.bmp
