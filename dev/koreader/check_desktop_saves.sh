#!/bin/sh
# Proves in headless KOReader that the plugin applies reviewed.jsonl when a book opens.
# Needs Docker and lua. Works in target/koreader-desktop-saves and exits non-zero on any failure.
# Usage: dev/koreader/check_desktop_saves.sh                 run the check against the fixture
#        dev/koreader/check_desktop_saves.sh --make-fixture  make desktop/tests/fixtures/real_sidecar again
set -e
cd "$(dirname "$0")/../.."
cache=target/koreader-emulator
[ -d "$cache/koreader" ] && [ -f "$cache/book.epub" ] || { echo "Run dev/koreader/render.sh once first."; exit 1; }
docker image inspect pbanki-koreader >/dev/null 2>&1 || docker build -q -t pbanki-koreader dev/koreader

exec lua - "$@" <<'EOF'
local REPO = io.popen("pwd"):read("*l")
local CACHE = "target/koreader-emulator"
local WORK = "target/koreader-desktop-saves"
local FIXTURE = "desktop/tests/fixtures/real_sidecar"
local DEVICE_BOOK = "/mnt/ext1/English/book.epub"
local SIDECAR = WORK .. "/English/book.sdr/metadata.epub.lua"
local REVIEWED = WORK .. "/pbanki/reviewed.jsonl"

local function sh(cmd)
    local ok = os.execute(cmd)
    if not ok then error("command failed: " .. cmd) end
end
local function readFile(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local s = f:read("*a")
    f:close()
    return s
end
local function writeFile(path, s)
    local f = assert(io.open(path, "wb"))
    f:write(s)
    f:close()
end

-- Opens the book in a fresh KOReader with `driver`. Returns its log.
local function openBook(label, driver)
    local log_path = WORK .. "/logs/" .. label .. ".log"
    sh(table.concat({
        "docker run --rm",
        "-v '" .. REPO .. "/" .. CACHE .. "/koreader:/ko-ro:ro'",
        "-v '" .. REPO .. "/dev/koreader/uxdriver.koplugin:/plugins/uxdriver.koplugin:ro'",
        "-v '" .. REPO .. "/koreader/pbanki.koplugin:/plugins/pbanki.koplugin:ro'",
        "-v '" .. REPO .. "/dev/koreader:/work:ro'",
        "-v '" .. REPO .. "/" .. WORK .. "/English:/mnt/ext1/English'",
        "-v '" .. REPO .. "/" .. WORK .. "/pbanki:/app/pbanki'",
        "-v '" .. REPO .. "/" .. WORK .. "/out:/out'",
        "-e UX_SCRIPT=/work/" .. driver .. " -e UX_DELAY=1.5",
        [[pbanki-koreader sh -c '
            cp -r /ko-ro /ko && cp -r /plugins/* /ko/plugins/
            cp /work/settings.reader.lua /ko/settings.reader.lua
            cd /ko && PBANKI_DIR=/app/pbanki EMULATE_READER_W=758 EMULATE_READER_H=1024 EMULATE_READER_DPI=212 \
                timeout -k 5 120 xvfb-run -a -s "-screen 0 758x1024x24" ./reader.lua ]] .. DEVICE_BOOK .. [[ 2>&1
            cp /ko/history.lua /out/history.lua' > ']] .. log_path .. "' 2>&1",
    }, " "))
    return readFile(log_path)
end

local function prepare()
    sh("rm -rf '" .. WORK .. "'")
    sh("mkdir -p '" .. WORK .. "/English' '" .. WORK .. "/pbanki' '" .. WORK .. "/out' '" .. WORK .. "/logs'")
end

if arg[1] == "--make-fixture" then
    prepare()
    sh("cp " .. CACHE .. "/book.epub " .. WORK .. "/English/book.epub")
    local log = openBook("make_fixture", "make_fixture.lua")
    for line in log:gmatch("[^\n]+") do
        if line:find("uxdriver", 1, true) or line:find("  text=", 1, true) then print(line) end
    end
    assert(not log:find("failed:", 1, true), "driver step failed")
    sh("mkdir -p " .. FIXTURE)
    sh("cp " .. SIDECAR .. " " .. FIXTURE .. "/metadata.epub.lua")
    sh("cp " .. WORK .. "/out/history.lua " .. FIXTURE .. "/history.lua")
    sh("cp " .. CACHE .. "/book.epub " .. FIXTURE .. "/book.epub")
    print("Fixture written to " .. FIXTURE)
    os.exit(0)
end

-- Check

local passed, failed = 0, 0
local function check(ok, what)
    if ok then
        passed = passed + 1
        print("  PASS " .. what)
    else
        failed = failed + 1
        print("  FAIL " .. what)
    end
end

-- A value as text, with table keys sorted
local function dump(v, indent)
    indent = indent or ""
    if type(v) == "string" then return string.format("%q", v):gsub("\\\n", "\\n") .. "" end
    if type(v) ~= "table" then return tostring(v) end
    local keys = {}
    for k in pairs(v) do table.insert(keys, k) end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    local lines = {}
    for _idx, k in ipairs(keys) do
        table.insert(lines, indent .. "  [" .. dump(k) .. "] = " .. dump(v[k], indent .. "  "))
    end
    if #lines == 0 then return "{}" end
    return "{\n" .. table.concat(lines, ",\n") .. "\n" .. indent .. "}"
end

local function jsonString(s)
    s = s:gsub('[%c"\\]', function(c)
        if c == '"' then return '\\"' end
        if c == "\\" then return "\\\\" end
        if c == "\n" then return "\\n" end
        return string.format("\\u%04x", c:byte())
    end)
    return '"' .. s .. '"'
end

local function highlightKey(item)
    return tostring(item.datetime) .. "|" .. tostring(item.pos0)
end

local function annotationKeys(sidecar)
    local keys = {}
    for _idx, item in ipairs(sidecar.annotations) do
        table.insert(keys, highlightKey(item) .. "|" .. tostring(item.note))
    end
    return table.concat(keys, "\n")
end

local function logValue(log, name)
    return log:match("uxdriver opened[^\n]- " .. name .. "=(%d+)")
end

local function fixtureBook()
    sh("rm -rf '" .. WORK .. "/English' '" .. WORK .. "/pbanki'")
    sh("mkdir -p '" .. WORK .. "/English/book.sdr' '" .. WORK .. "/pbanki'")
    sh("cp " .. FIXTURE .. "/book.epub " .. WORK .. "/English/book.epub")
    sh("cp " .. FIXTURE .. "/metadata.epub.lua " .. SIDECAR)
end

local fixture = dofile(FIXTURE .. "/metadata.epub.lua")
local highlights = {}
for _idx, item in ipairs(fixture.annotations) do
    if item.drawer then table.insert(highlights, item) end
end
local with_cards, skipped = highlights[3], highlights[5]
local key_cards, key_skipped = highlightKey(with_cards), highlightKey(skipped)

-- One card with a \u escape, one with raw UTF-8 including a character beyond \uffff
local LINE_CARDS = '{"v":1,"batch":"b1","file":' .. jsonString(DEVICE_BOOK) .. ',"key":' .. jsonString(key_cards)
    .. ',"cards":[{"front":"What does Mrs. Bennet call a single man of large fortune\\u2019s arrival?",'
    .. '"back":"A fine thing for our girls","suggested":{"front":"Why is it fine?","back":"Money 💰"},'
    .. '"quote":' .. jsonString(with_cards.text) .. ',"note":' .. jsonString(with_cards.note or "") .. '},'
    .. '{"front":"Four or five {{c1::thousand}} a year — whose income?","back":"","quote":'
    .. jsonString(with_cards.text) .. '}]}'
local LINE_SKIPPED = '{"v":1,"batch":"b1","file":' .. jsonString(DEVICE_BOOK) .. ',"key":'
    .. jsonString(key_skipped) .. ',"cards":[]}'
local LINE_OTHER = '{"v":1,"batch":"b1","file":"/mnt/ext1/English/Other Book.epub","key":"2026-01-01 10:00:00|/body/p",'
    .. '"cards":[{"front":"Other","back":"Book","quote":"Not this one"}]}'
local LINE_BAD = '{"v":1,"file":"/mnt/ext1/English/book.epub","key":"cut off in the mid'
local ORIGINAL = table.concat({ LINE_CARDS, LINE_OTHER, LINE_BAD, LINE_SKIPPED }, "\n") .. "\n"
local LEFT = LINE_OTHER .. "\n" .. LINE_BAD .. "\n"

local function showFile(label)
    local text = readFile(REVIEWED)
    print("  " .. label .. ":")
    print(text and text:gsub("[^\n]+", "    %0") or "    (no file)")
end

prepare()
print("Highlights in the fixture: " .. #highlights .. ", marking two of them:")
print("  with cards: " .. key_cards)
print("  skipped:    " .. key_skipped)

fixtureBook()
writeFile(REVIEWED, ORIGINAL)

print("\nRun 1: first open, with two lines for this book, one for another book, one malformed")
local log = openBook("run1", "desktop_saves.lua")
local after = dofile(SIDECAR)
print(log:match("[^\n]*uxdriver opened[^\n]*") or "(no driver line)")
print("  pbanki_reviewed = " .. dump(after.pbanki_reviewed, "  "))
print("  pbanki_card_history = " .. dump(after.pbanki_card_history, "  "))
showFile("reviewed.jsonl afterwards")
print(log:match("[^\n]*pbanki: unreadable line[^\n]*") or "(no warning line)")

check(log:find("uxdriver saved", 1, true), "the book opened and ran")
check(logValue(log, "on_disk_reviewed") == "2", "the sidecar on disk had both keys before any close")
check(logValue(log, "on_disk_history") == "2", "the sidecar on disk had the cards before any close")
check(logValue(log, "pending") == tostring(#highlights - 2), "KOReader counts two fewer pending highlights")
check(after.pbanki_reviewed[key_cards] == true, "the highlight with cards is marked reviewed")
check(after.pbanki_reviewed[key_skipped] == true, "the skipped highlight is marked reviewed")
local reviewed_count = 0
for _key in pairs(after.pbanki_reviewed) do reviewed_count = reviewed_count + 1 end
check(reviewed_count == 2, "nothing else is marked reviewed")
local history = after.pbanki_card_history
check(#history == 2, "the card history has both cards")
check(history[1].front == "What does Mrs. Bennet call a single man of large fortune\226\128\153s arrival?",
    "a \\u escape in a card decodes to the right characters")
check(history[1].suggested and history[1].suggested.back == "Money \240\159\146\176",
    "raw UTF-8 beyond \\uffff is kept")
check(history[1].quote == with_cards.text and history[1].note == with_cards.note, "quote and note are kept")
check(history[2].front:find("\226\128\148", 1, true) ~= nil, "raw UTF-8 in a card is kept")
check(history[2].suggested == nil and history[2].note == nil, "absent fields stay absent")
check(readFile(REVIEWED) == LEFT, "reviewed.jsonl keeps only the other book's line and the malformed line")
check(readFile(REVIEWED .. ".tmp") == nil, "no temp file left")
check(log:find("pbanki: unreadable line kept in reviewed.jsonl", 1, true), "the malformed line was logged")
check(annotationKeys(after) == annotationKeys(fixture), "the highlights themselves are unchanged")

print("\nRun 2: opening a second time must change nothing")
local before = after
log = openBook("run2", "desktop_saves.lua")
after = dofile(SIDECAR)
print(log:match("[^\n]*uxdriver opened[^\n]*") or "(no driver line)")
showFile("reviewed.jsonl afterwards")
local changed = {}
local keys = {}
for k in pairs(before) do keys[k] = true end
for k in pairs(after) do keys[k] = true end
for k in pairs(keys) do
    if dump(before[k]) ~= dump(after[k]) then table.insert(changed, k) end
end
table.sort(changed)
print("  sidecar fields that differ from run 1: " .. (#changed > 0 and table.concat(changed, ", ") or "none"))
check(log:find("uxdriver saved", 1, true), "the book opened and ran")
check(dump(after.pbanki_reviewed) == dump(before.pbanki_reviewed), "pbanki_reviewed is the same")
check(dump(after.pbanki_card_history) == dump(before.pbanki_card_history), "pbanki_card_history is the same")
check(readFile(REVIEWED) == LEFT, "reviewed.jsonl is the same, byte for byte")
check(annotationKeys(after) == annotationKeys(fixture), "the highlights are the same")

print("\nRun 3: the same lines again, as after a crash before the file was rewritten")
writeFile(REVIEWED, ORIGINAL)
before = after
log = openBook("run3", "desktop_saves.lua")
after = dofile(SIDECAR)
check(log:find("uxdriver saved", 1, true), "the book opened and ran")
check(#after.pbanki_card_history == 2, "no duplicate cards in the history")
check(dump(after.pbanki_reviewed) == dump(before.pbanki_reviewed), "pbanki_reviewed is the same")
check(readFile(REVIEWED) == LEFT, "reviewed.jsonl is down to the same two lines")

print("\nRun 4: only this book's lines, so the file is deleted")
fixtureBook()
writeFile(REVIEWED, LINE_SKIPPED .. "\n")
log = openBook("run4", "desktop_saves.lua")
after = dofile(SIDECAR)
check(log:find("uxdriver saved", 1, true), "the book opened and ran")
check(after.pbanki_reviewed[key_skipped] == true, "the highlight is marked reviewed")
check(after.pbanki_card_history == nil, "no cards, so no card history")
check(readFile(REVIEWED) == nil, "reviewed.jsonl is deleted")

print("\nRun 5: no file at all")
fixtureBook()
log = openBook("run5", "desktop_saves.lua")
after = dofile(SIDECAR)
print(log:match("[^\n]*uxdriver no_file[^\n]*") or "(no driver line)")
check(log:find("uxdriver saved", 1, true), "the book opened and ran")
check(after.pbanki_reviewed == nil and after.pbanki_card_history == nil, "nothing was added to the sidecar")
check(readFile(REVIEWED) == nil, "no file was made")

print("\nRun 6: a file of garbage must not stop the book opening")
fixtureBook()
local GARBAGE = "\0\255\254 not json at all\n\n\128\129\130\n"
writeFile(REVIEWED, GARBAGE)
log = openBook("run6", "desktop_saves.lua")
after = dofile(SIDECAR)
showFile("reviewed.jsonl afterwards")
check(log:find("uxdriver saved", 1, true), "the book opened and ran")
check(after.pbanki_reviewed == nil, "nothing was marked reviewed")
check(readFile(REVIEWED) == GARBAGE, "the file is untouched")

print(string.format("\n%d passed, %d failed", passed, failed))
os.exit(failed == 0 and 0 or 1)
EOF
