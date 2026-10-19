-- Run from the repo root: lua koreader/spec/cardgen_spec.lua
package.path = "koreader/pbanki.koplugin/?.lua;" .. package.path
local cardgen = require("cardgen")

local function eq(got, want, msg)
    assert(got == want, msg .. ": expected [" .. tostring(want) .. "] got [" .. tostring(got) .. "]")
end

-- Stands in for a JSON decoder: known strings map to tables
local function fakeDecoder(map)
    return function(s)
        local v = map[s]
        if v == nil then error("bad json") end
        return v
    end
end

local two = '{"cards":[{"front":"Q1","back":"A1"},{"front":"Q2","back":"A2"}]}'
local decode = fakeDecoder{
    [two] = { cards = { { front = " Q1 ", back = "A1\n" }, { front = "Q2", back = "A2" } } },
    ['[{"front":"Q","back":"A"}]'] = { { front = "Q", back = "A" } },
    ['{"cards":[{"front":"","back":"x"}]}'] = { cards = { { front = "", back = "x" } } },
}

-- extractJson
eq(cardgen.extractJson(two), two, "plain object")
eq(cardgen.extractJson("```json\n" .. two .. "\n```"), two, "fenced")
eq(cardgen.extractJson("Here you go:\n" .. two .. "\nEnjoy!"), two, "surrounding prose")
eq(cardgen.extractJson("no json here"), nil, "no json")
eq(cardgen.extractJson(nil), nil, "nil reply")

-- parseCards
local cards = cardgen.parseCards(two, 5, decode)
eq(#cards, 2, "two cards")
eq(cards[1].front, "Q1", "front trimmed")
eq(cards[1].back, "A1", "back trimmed")
eq(#cardgen.parseCards(two, 1, decode), 1, "capped at count")
eq(#cardgen.parseCards('[{"front":"Q","back":"A"}]', 3, decode), 1, "bare array")
local none, err = cardgen.parseCards('{"cards":[{"front":"","back":"x"}]}', 3, decode)
eq(none, nil, "empty fronts dropped")
assert(err, "error for no cards")
none, err = cardgen.parseCards("{oops}", 3, decode)
eq(none, nil, "undecodable reply")
assert(err, "error for undecodable reply")

-- buildMessages
local h = { text = "Paris is the capital of France.", before = "It is known that", after = "It is large.",
    book = "Geo", author = "A. Writer", chapter = "One", percent = 12.4, note = "check" }
local msgs = cardgen.buildMessages(h, { count = 2 }, nil, nil)
eq(#msgs, 2, "system and user without companion")
eq(msgs[1].role, "system", "system first")
local user = msgs[2].content
assert(user:find("Book: Geo", 1, true), "book named")
assert(user:find("Position in book: 12%", 1, true), "position rounded")
assert(user:find("«Paris is the capital of France.»", 1, true), "highlight marked in passage")
assert(user:find("reader's note", 1, true), "note included")
assert(user:find("Write 2 cards", 1, true), "count asked")
msgs = cardgen.buildMessages({ text = "x" }, { count = 1 }, "COMPANION", "You are mid-chapter.")
eq(#msgs, 3, "companion adds a system block")
eq(msgs[2].cache, true, "companion block cached")
assert(msgs[3].content:find("Write 1 card.", 1, true), "singular count")
assert(msgs[3].content:find("mid-chapter", 1, true), "position note in user message")
assert(not msgs[3].content:find("Passage around", 1, true), "no passage without context")

-- The AI chooses the count in batch mode
local ranged = cardgen.buildMessages({ text = "x" }, { max = 3 }, nil, nil)[2].content
assert(ranged:find("from 1 to 3", 1, true), "range offered")
assert(ranged:find("gets 1 card", 1, true), "concise highlights get one card")
eq(cardgen.countInstruction({ max = 1 }), "Write 1 card.", "range of one")
eq(cardgen.cardLimit({ max = 3 }), 3, "limit from range")
eq(cardgen.cardLimit({ count = 2 }), 2, "limit from count")

-- Suggesting more avoids the cards already there
local more = cardgen.buildMessages({ text = "x" }, { count = 2, existing = { { front = "Q1", back = "A1" } } })[2].content
assert(more:find("already has these cards", 1, true), "existing cards mentioned")
assert(more:find("- Q1 → A1", 1, true), "existing card listed")
assert(more:find("Write 2 cards", 1, true), "count still asked")
assert(not ranged:find("already has", 1, true), "no existing cards, no mention")
assert(more:find("same style", 1, true), "kept cards set the style")
assert(not more:find("rewrote", 1, true), "no edits, no mention")
assert(not more:find("removed these", 1, true), "no rejections, no mention")

-- Edited and removed suggestions teach the next ones
local kept = { front = "Q1", back = "A1", suggested = { front = "Q1", back = "A1" } }
local edited = { front = "Q2 short", back = "A2", suggested = { front = "Q2 long", back = "A2" } }
assert(not cardgen.wasEdited(kept), "unchanged suggestion")
assert(cardgen.wasEdited(edited), "changed suggestion")
assert(not cardgen.wasEdited({ front = "Q", back = "A" }), "own card")
local learn = cardgen.buildMessages({ text = "x" }, {
    count = 1,
    existing = { kept, edited },
    rejected = { { front = "Q3", back = "A3" } },
})[2].content
assert(learn:find("- You suggested: Q2 long → A2\n  The reader changed it to: Q2 short → A2", 1, true),
    "edit shown as before and after")
assert(not learn:find("You suggested: Q1", 1, true), "unchanged card not shown as edit")
assert(learn:find("removed these suggested cards", 1, true), "rejections mentioned")
assert(learn:find("- Q3 → A3", 1, true), "rejected card listed")
assert(learn:find("Write 1 card.", 1, true), "count still last")

print("cardgen_spec: OK")
