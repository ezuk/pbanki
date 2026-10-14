#!/bin/sh
# Launches pbAnki from the PocketBook Applications menu.

APP_DIR=/mnt/ext1/applications/pbanki
cd "$APP_DIR" || exit 1

# Firmware 6.11 refuses to load libinkview.so without this
export GLIBC_TUNABLES="${GLIBC_TUNABLES}${GLIBC_TUNABLES:+:}glibc.rtld.execstack=2"
export RUST_BACKTRACE=1

# Keep earlier runs; start over once the log passes 1 MB
LOG="$APP_DIR/log.txt"
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1000000 ]; then
    mv "$LOG" "$APP_DIR/log.old.txt"
fi
echo "=== pbAnki started $(date)" >> "$LOG"
"$APP_DIR/pbanki" >> "$LOG" 2>&1
echo "=== exit code $?" >> "$LOG"
sync
