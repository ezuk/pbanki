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

-- The prompt steers toward the reader's own cards
assert(cardgen.SYSTEM_PROMPT:find("note on the highlight", 1, true), "note steers the cards")
assert(cardgen.SYSTEM_PROMPT:find("book's own name", 1, true), "book's terms preferred")
assert(cardgen.SYSTEM_PROMPT:find("not be guessable", 1, true), "guessable answers ruled out")
assert(user:find("let it steer", 1, true), "note marked as steering")

-- Card history keeps the latest cards with their AI originals
local hl = { text = "Slow breathing boosts willpower.", note = "for flights" }
local entry = cardgen.historyEntry(hl, edited)
eq(entry.quote, hl.text, "history keeps the quote")
eq(entry.note, "for flights", "history keeps the note")
eq(entry.suggested.front, "Q2 long", "history keeps the AI original")
eq(cardgen.historyEntry({ text = "x", note = "" }, kept).note, nil, "empty note dropped")
eq(cardgen.historyEntry({ text = "x" }, { front = "Q", back = "A" }).suggested, nil, "own card has no original")
local history = cardgen.appendHistory({ { front = "old" } }, { { front = "a" }, { front = "b" } }, 2)
eq(#history, 2, "history capped")
eq(history[1].front, "a", "oldest dropped")
eq(#cardgen.appendHistory(nil, { { front = "a" } }, 5), 1, "empty history")

-- Edited cards are shown first, then the latest
local log = {
    { front = "E1", back = "x", suggested = { front = "e1", back = "x" }, quote = "q1" },
    { front = "K1", back = "x", suggested = { front = "K1", back = "x" }, quote = "q1" },
    { front = "K2", back = "x", quote = "q2" },
    { front = "K3", back = "x", quote = "q2" },
}
local picked = cardgen.pickRecent(log, 2)
eq(#picked, 2, "picked up to limit")
eq(picked[1].front, "E1", "edited card picked though oldest")
eq(picked[2].front, "K3", "then the latest")
eq(#cardgen.pickRecent(log, 10), 4, "all when under limit")
eq(#cardgen.pickRecent({}, 3), 0, "empty history picks none")

local withRecent = cardgen.buildMessages({ text = "x" }, {
    count = 1,
    recent = { log[1], log[2], { front = "N", back = "y", quote = "q3", note = "my note" } },
})[2].content
assert(withRecent:find("saved recently from this book", 1, true), "recent cards introduced")
assert(withRecent:find("Highlight: «q1»\n- You suggested: e1 → x\n  The reader changed it to: E1 → x\n- K1 → x", 1, true),
    "recent cards grouped under their highlight")
assert(withRecent:find("Highlight: «q3»\nReader's note: my note\n- N → y", 1, true), "note shown with its highlight")
assert(withRecent:find("Write 1 card.", 1, true), "count still last with recent cards")
assert(not ranged:find("saved recently", 1, true), "no history, no mention")
local long = cardgen.recentSection({ { front = "Q", back = "A", quote = string.rep("word ", 100) } })
assert(long:find("word …»", 1, true), "long quote shortened at a word")

-- Cloze
local clozeMessages = cardgen.buildMessages({ text = "x" }, {
    count = 1,
    cloze = true,
    existing = { { front = "A {{c1::b}}.", back = "" } },
})
eq(clozeMessages[1].content, cardgen.CLOZE_PROMPT, "cloze request uses the cloze prompt")
assert(cardgen.CLOZE_PROMPT:find("Choosing what to ask", 1, true), "cloze prompt keeps the choosing rules")
assert(clozeMessages[2].content:find("- A {{c1::b}}.\n", 1, true), "cloze card without extra listed alone")
eq(cardgen.buildMessages({ text = "x" }, { count = 1 })[1].content, cardgen.SYSTEM_PROMPT, "plain request")
assert(cardgen.isCloze("The {{c1::Seine}}"), "cloze detected")
assert(not cardgen.isCloze("{{Front}}"), "field reference is not a cloze")
eq(cardgen.showCloze("A {{c1::b::hint}} and {{c2::c}}."), "A [b] and [c].", "cloze shown with brackets")
eq(cardgen.clozeMark("The ", "The x"), "{{c1::", "first mark opens c1")
eq(cardgen.clozeMark("The {{c1::Sei", "The {{c1::Sei"), "}}", "open deletion is closed")
eq(cardgen.clozeMark("The {{c1::Seine}} and ", "The {{c1::Seine}} and "), "{{c2::", "next deletion numbered")

-- Cloze cards quote the book
local book = "It is a truth universally acknowledged, that a single man in possession of a good fortune, "
    .. "must be in want of a wife."
assert(cardgen.quotesText("a single man in possession of a good {{c1::fortune}}", book), "verbatim part quoted")
assert(cardgen.quotesText("It is a {{c1::truth universally acknowledged::hint}}", book), "hint ignored")
assert(cardgen.quotesText("It\226\128\153s fine", "It's fine."), "curly apostrophe matches")
assert(not cardgen.quotesText("A rich single man must want a {{c1::wife}}.", book), "paraphrase rejected")
assert(not cardgen.quotesText("{{c1::wife}} must be in want", book), "reordered rejected")
local clozeReply = '{"cards":[{"front":"P"},{"front":"Q"},{"front":"R"}]}'
local clozeDecode = fakeDecoder{ [clozeReply] = { cards = {
    { front = "A single man must want a {{c1::wife}}." },
    { front = "must be in want of a {{c1::wife}}." },
    { front = "What is universally acknowledged?", back = "A truth" },
} } }
local quoted = cardgen.parseReply(clozeReply, { count = 3, cloze = true }, { text = book }, clozeDecode)
eq(#quoted, 1, "only the quoting cloze card kept")
eq(quoted[1].front, book:gsub("wife%.", "{{c1::wife}}."), "quoting card widened to the whole highlight")
eq(#cardgen.parseReply(clozeReply, { count = 3 }, { text = book }, clozeDecode), 3, "basic cards not filtered")
local noQuote = '{"cards":[{"front":"X"}]}'
local dropped, droppedErr = cardgen.parseReply(noQuote, { count = 1, cloze = true }, { text = book },
    fakeDecoder{ [noQuote] = { cards = { { front = "Men want {{c1::wives}}." } } } })
eq(dropped, nil, "no quoting cards")
assert(droppedErr, "error when no cloze card quotes the book")
assert(cardgen.CLOZE_PROMPT:find("word for word", 1, true), "cloze prompt asks for exact quotes")
assert(cardgen.CLOZE_PROMPT:find("even when it is a long paragraph", 1, true), "cloze keeps long highlights whole")

-- Cloze cards cover the whole highlight
local para = "First sentence here. The key idea is focus. Last sentence there."
eq(cardgen.expandCloze("The key idea is {{c1::focus}}.", para),
    "First sentence here. The key idea is {{c1::focus}}. Last sentence there.", "widened to the paragraph")
eq(cardgen.expandCloze("{{c1::First}} sentence here. The key idea is focus. Last sentence there.", para),
    "{{c1::First}} sentence here. The key idea is focus. Last sentence there.", "whole highlight unchanged")
eq(cardgen.expandCloze("A {{c1::sentence}} around a short highlight.", "short highlight"),
    "A {{c1::sentence}} around a short highlight.", "longer than the highlight unchanged")
eq(cardgen.expandCloze("The key idea is {{c1::focus::a hint}}.", para),
    "First sentence here. The key idea is {{c1::focus::a hint}}. Last sentence there.", "hints kept")

-- Definition cards
local defReply = '{"term":"x"}'
local pair = cardgen.parseReply(defReply, { definition = true }, { text = "x" },
    fakeDecoder{ [defReply] = { term = " willpower ", definition = "The ability to do what matters most." } })
eq(#pair, 2, "definition gives two cards")
eq(pair[1].front, "willpower", "term on the front")
eq(pair[1].back, "The ability to do what matters most.", "definition on the back")
eq(pair[2].front, "The ability to do what matters most.", "reverse card shows the definition")
eq(pair[2].back, "willpower", "reverse card asks for the term")
local badDef = '{"term":""}'
eq(cardgen.parseDefinition(badDef, fakeDecoder{ [badDef] = { term = "" } }), nil, "missing definition")
local defMessages = cardgen.buildMessages({ text = "willpower" }, {
    definition = true,
    cloze = true,
    existing = { { front = "willpower", back = "Self-control" } },
})
eq(defMessages[1].content, cardgen.DEFINITION_PROMPT, "definition prompt wins over cloze")
assert(defMessages[2].content:find("different definition", 1, true), "asks for a different definition")
assert(not defMessages[2].content:find("Write 1 card", 1, true), "no card count for definitions")

-- Guidance
local guided = cardgen.buildMessages({ text = "x" }, { count = 2, guidance = "Ask about the numbers" })[2].content
assert(guided:find("Ask about the numbers", 1, true), "guidance included")
assert(guided:find("Write 2 cards", 1, true), "count still asked with guidance")
assert(not cardgen.buildMessages({ text = "x" }, { count = 1 })[2].content:find("reader asked", 1, true),
    "no guidance section without guidance")

print("cardgen_spec: OK")
