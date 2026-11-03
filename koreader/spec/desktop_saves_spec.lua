-- Run from the repo root: lua koreader/spec/desktop_saves_spec.lua
package.path = "koreader/pbanki.koplugin/?.lua;" .. package.path
local cardgen = require("cardgen")

local function eq(got, want, msg)
    assert(got == want, msg .. ": expected [" .. tostring(want) .. "] got [" .. tostring(got) .. "]")
end

local FILE = "/mnt/ext1/English/X.epub"
local NULL = function() end

local A = '{"v":1,"file":"' .. FILE .. '","key":"k1","cards":[card]}'
local B = '{"v":1,"file":"' .. FILE .. '","key":"k2","cards":[]}'
local OTHER = '{"v":1,"file":"/mnt/ext1/English/Y.epub","key":"k9","cards":[]}'
local BAD = "{not json"

local CARD = { front = "Q", back = "A", suggested = { front = "Q0", back = "A0" }, quote = "text", note = "mine" }
local decoded = {
    [A] = { v = 1, batch = "b1", file = FILE, key = "k1", cards = { CARD } },
    [B] = { v = 1, file = FILE, key = "k2", cards = {} },
    [OTHER] = { v = 1, file = "/mnt/ext1/English/Y.epub", key = "k9", cards = {} },
    ["no-cards"] = { v = 1, file = FILE, key = "k3" },
    ["nulls"] = { v = 1, file = FILE, key = "k4", cards = { { front = "F", back = NULL, quote = NULL } } },
    ["null-extras"] = { v = 1, file = FILE, key = "k5",
        cards = { { front = "F", back = "B", suggested = NULL, quote = NULL, note = NULL } } },
    ["v2"] = { v = 2, file = FILE, key = "k6", cards = {} },
    ["no-version"] = { file = FILE, key = "k6", cards = {} },
    ["no-key"] = { v = 1, file = FILE, cards = {} },
    ["empty-key"] = { v = 1, file = FILE, key = "", cards = {} },
    ["no-file"] = { v = 1, key = "k6", cards = {} },
    ["bad-cards"] = { v = 1, file = FILE, key = "k6", cards = NULL },
    ["bad-card"] = { v = 1, file = FILE, key = "k6", cards = { "text" } },
    ["no-front"] = { v = 1, file = FILE, key = "k6", cards = { { back = "B" } } },
    ["number"] = 7,
}
-- Throws on bad input, as KOReader's json module does
local function decode(s)
    local v = decoded[s]
    if v == nil then error("bad json") end
    return v
end
-- Returns nil on bad input
local function softDecode(s) return decoded[s] end

-- parseDesktopSaves
local text = table.concat({ A, BAD, OTHER, "", "   ", B }, "\n") .. "\n"
local saves, remaining, malformed = cardgen.parseDesktopSaves(text, FILE, decode)
eq(#saves, 2, "this book's lines")
eq(saves[1].key, "k1", "first key")
eq(saves[2].key, "k2", "second key")
eq(saves[1].cards[1].front, "Q", "card front")
eq(saves[1].cards[1].suggested.back, "A0", "card suggestion")
eq(saves[1].cards[1].note, "mine", "card note")
eq(#saves[2].cards, 0, "skipped highlight has no cards")
eq(#remaining, 2, "kept lines")
eq(remaining[1], BAD, "malformed line kept in place, unchanged")
eq(remaining[2], OTHER, "other book's line kept unchanged")
eq(#malformed, 1, "malformed reported")
eq(malformed[1], BAD, "malformed raw line reported")

saves, remaining, malformed = cardgen.parseDesktopSaves(A, FILE, decode)
eq(#saves, 1, "last line without a newline")
eq(#remaining, 0, "nothing left")

saves, remaining, malformed = cardgen.parseDesktopSaves("", FILE, decode)
eq(#saves + #remaining + #malformed, 0, "empty file")

saves, remaining, malformed = cardgen.parseDesktopSaves(BAD .. "\n" .. A .. "\n", FILE, softDecode)
eq(#saves, 1, "decoder returning nil: good line read")
eq(malformed[1], BAD, "decoder returning nil: bad line kept")

saves = cardgen.parseDesktopSaves("no-cards", FILE, decode)
eq(#saves[1].cards, 0, "missing cards list is no cards")

saves, remaining = cardgen.parseDesktopSaves("null-extras", FILE, decode)
eq(saves[1].cards[1].suggested, nil, "null suggestion dropped")
eq(saves[1].cards[1].quote, nil, "null quote dropped")
eq(saves[1].cards[1].note, nil, "null note dropped")

for _idx, line in ipairs{ "v2", "no-version", "no-key", "empty-key", "no-file", "bad-cards", "bad-card",
        "no-front", "nulls", "number" } do
    saves, remaining, malformed = cardgen.parseDesktopSaves(line, FILE, decode)
    eq(#saves, 0, line .. ": not applied")
    eq(remaining[1], line, line .. ": kept")
    eq(malformed[1], line, line .. ": reported")
end

-- newHistoryEntries
local save = { key = "k", cards = {
    { front = "Q", back = "A", suggested = { front = "Q0", back = "A0" }, quote = "text", note = "mine" },
    { front = "Q", back = "A", quote = "text" },
    { front = "Q2", back = "A2", quote = "text", note = "" },
} }
local entries = cardgen.newHistoryEntries(nil, { save })
eq(#entries, 2, "repeat within a batch skipped")
eq(entries[1].suggested.front, "Q0", "suggestion carried")
eq(entries[1].quote, "text", "quote carried")
eq(entries[1].note, "mine", "note carried")
eq(entries[2].note, nil, "blank note left out, as historyEntry does")

entries = cardgen.newHistoryEntries({ { front = "Q", back = "A", quote = "text" } }, { save })
eq(#entries, 1, "already in history skipped")
eq(entries[1].front, "Q2", "only the new card")
eq(#cardgen.newHistoryEntries({ { front = "Q", back = "other", quote = "text" } }, { save }), 2,
    "a different back is a new card")
eq(#cardgen.newHistoryEntries({ { front = "Q", back = "A", quote = "other" } }, { save }), 2,
    "a different quote is a new card")

-- applyDesktopSaves: main.lua with KOReader's modules stubbed

local dir = os.tmpname()
os.remove(dir)
assert(os.execute("mkdir '" .. dir .. "'"))
local REVIEWED = dir .. "/reviewed.jsonl"

local real_getenv = os.getenv
os.getenv = function(name)
    if name == "PBANKI_DIR" then return dir end
    return real_getenv(name)
end

local warnings = {}
local write_fails = false
local function stub(name, value)
    package.preload[name] = function() return value end
end
for _idx, name in ipairs{ "ui/widget/buttontable", "cardeditor", "dispatcher", "ui/widget/inputdialog",
        "ui/widget/infomessage", "ui/network/manager", "ui/widget/notification", "ui/size",
        "ui/widget/spinwidget", "ui/trapper", "ui/uimanager", "libs/libkoreader-lfs",
        "apps/filemanager/filemanagerutil" } do
    stub(name, {})
end
stub("ui/widget/container/widgetcontainer", { extend = function(_self, t) return t end })
stub("json", { decode = decode })
stub("logger", { warn = function(...) table.insert(warnings, table.concat({ ... }, " ", 1, 2)) end })
stub("gettext", function(s) return s end)
stub("ffi/util", { template = function(s) return s end })
stub("util", { writeToFile = function(data, path)
    if write_fails then return nil, "disk full" end
    local f = assert(io.open(path, "wb"))
    f:write(data)
    f:close()
    return true
end })

local PbAnki = dofile("koreader/pbanki.koplugin/main.lua")
os.getenv = real_getenv

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

-- A plugin on an open book, with settings that count how often they are flushed
local function plugin(data, flush_fails)
    data = data or {}
    local settings = { data = data, flushes = 0 }
    function settings:readSetting(key) return self.data[key] end
    function settings:saveSetting(key, value) self.data[key] = value end
    function settings:flush()
        self.flushes = self.flushes + 1
        return not flush_fails and "sidecar" or nil
    end
    local p = setmetatable({ ui = { document = { file = FILE }, doc_settings = settings } }, { __index = PbAnki })
    return p, settings
end

-- No file: nothing happens
local p, settings = plugin()
p:applyDesktopSaves()
eq(settings.flushes, 0, "no file: no flush")
eq(next(settings.data), nil, "no file: no settings")
eq(readFile(REVIEWED), nil, "no file: none made")

-- Only other books' lines: file and settings untouched
writeFile(REVIEWED, OTHER .. "\n")
p, settings = plugin()
p:applyDesktopSaves()
eq(settings.flushes, 0, "other book: no flush")
eq(readFile(REVIEWED), OTHER .. "\n", "other book: file unchanged")

-- Mixed file
warnings = {}
writeFile(REVIEWED, table.concat({ A, BAD, OTHER, B }, "\n") .. "\n")
p, settings = plugin({ pbanki_reviewed = { old = true } })
p:applyDesktopSaves()
eq(settings.data.pbanki_reviewed.k1, true, "marked with cards")
eq(settings.data.pbanki_reviewed.k2, true, "marked skipped")
eq(settings.data.pbanki_reviewed.old, true, "earlier mark kept")
eq(settings.data.pbanki_reviewed.k9, nil, "other book not marked")
eq(#settings.data.pbanki_card_history, 1, "history gained the card")
eq(settings.data.pbanki_card_history[1].front, "Q", "history front")
eq(settings.flushes, 1, "flushed once")
eq(readFile(REVIEWED), BAD .. "\n" .. OTHER .. "\n", "only other book and malformed lines remain")
eq(readFile(REVIEWED .. ".tmp"), nil, "temp file gone")
eq(#warnings, 1, "malformed line logged")
assert(warnings[1]:find("unreadable line", 1, true), "malformed warning text")

-- Opening again with what is left changes nothing
local first_history = settings.data.pbanki_card_history
warnings = {}
p:applyDesktopSaves()
eq(settings.flushes, 1, "second open: no flush")
eq(settings.data.pbanki_card_history, first_history, "second open: history untouched")
eq(readFile(REVIEWED), BAD .. "\n" .. OTHER .. "\n", "second open: file unchanged")
eq(#warnings, 1, "second open: still logged")

-- Same lines again, as after a crash before the rewrite: no duplicate history
writeFile(REVIEWED, A .. "\n")
p:applyDesktopSaves()
eq(#settings.data.pbanki_card_history, 1, "repeat adds no history")
eq(readFile(REVIEWED), nil, "file deleted when nothing remains")

-- Skipped highlights alone leave the history unset
writeFile(REVIEWED, B .. "\n")
p, settings = plugin()
p:applyDesktopSaves()
eq(settings.data.pbanki_card_history, nil, "no cards: no history")
eq(settings.data.pbanki_reviewed.k2, true, "no cards: still marked")

-- History stays within its limit
local long = {}
for i = 1, 40 do long[i] = { front = "old" .. i, back = "", quote = "q" } end
writeFile(REVIEWED, A .. "\n")
p, settings = plugin({ pbanki_card_history = long })
p:applyDesktopSaves()
eq(#settings.data.pbanki_card_history, 40, "history limit")
eq(settings.data.pbanki_card_history[1].front, "old2", "oldest dropped")
eq(settings.data.pbanki_card_history[40].front, "Q", "newest last")

-- Settings not written: the lines stay to be applied next time
writeFile(REVIEWED, A .. "\n")
warnings = {}
p, settings = plugin(nil, true)
p:applyDesktopSaves()
eq(readFile(REVIEWED), A .. "\n", "flush failed: file kept")
eq(#warnings, 1, "flush failed: logged")

-- Temp file not written: the old file stays
write_fails = true
writeFile(REVIEWED, A .. "\n" .. OTHER .. "\n")
warnings = {}
p, settings = plugin()
p:applyDesktopSaves()
write_fails = false
eq(readFile(REVIEWED), A .. "\n" .. OTHER .. "\n", "rewrite failed: file kept")
assert(warnings[1]:find("cannot rewrite", 1, true), "rewrite failed: logged")

-- Empty file: deleted
writeFile(REVIEWED, "")
p, settings = plugin()
p:applyDesktopSaves()
eq(readFile(REVIEWED), nil, "empty file deleted")
eq(settings.flushes, 0, "empty file: no flush")

-- Failures are caught
writeFile(REVIEWED, A .. "\n")
warnings = {}
p = setmetatable({ ui = {} }, { __index = PbAnki })
p:applyDesktopSaves()
eq(readFile(REVIEWED), A .. "\n", "error: file kept")
assert(warnings[1]:find("cannot apply", 1, true), "error: logged")

os.remove(REVIEWED)
assert(os.execute("mkdir '" .. REVIEWED .. "'"))
warnings = {}
p, settings = plugin()
p:applyDesktopSaves()
assert(warnings[1]:find("cannot apply", 1, true), "folder in place of the file: logged")
eq(settings.flushes, 0, "folder in place of the file: no flush")

assert(os.execute("rm -rf '" .. dir .. "'"))

print("desktop_saves_spec: OK")
