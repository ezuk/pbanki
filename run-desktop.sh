#!/bin/sh
# Starts the pbAnki desktop companion.
#   ./run-desktop.sh             your real reader and real AI
#   ./run-desktop.sh --practice  a fake reader and made-up AI answers
set -e
desktop="$(cd "$(dirname "$0")" && pwd)/desktop"

if [ "$1" != "--practice" ]; then
    exec python3 "$desktop/run.py" "$@"
fi

port=${2:-8766}
(
    until curl -s -o /dev/null "http://127.0.0.1:$port/"; do sleep 0.2; done
    "${PBANKI_OPEN_CMD:-open}" "http://127.0.0.1:$port"
) &
exec python3 "$desktop/tests/e2e/dev_server.py" --port "$port"
