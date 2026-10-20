--[[--
Builds the AI request for flashcard suggestions and parses the reply.

No KOReader dependencies, so it runs under plain Lua in spec/.

@module koplugin.pbanki.cardgen
--]]--

local M = {}

local CHOOSING = [[You write Anki flashcards from a passage a reader highlighted in a book.

Choosing what to ask:
- Base every card on the highlighted text. Use the surrounding passage and book notes to understand it and to name things.
- If the reader left a note on the highlight, it says what they care about or how they will use it. Let it decide which ideas you pick and how you frame the cards, even over the highlight's main point.
- Pick what is worth remembering: the main claim, a surprising finding, or a practice the reader can use. Skip side examples and restatements.
- When the passage gives advice, ask when to use it: "How to breathe when encountering temptation?" → "Slowly, 4 to 6 breaths per minute."
- Use the book's own name for an idea, even when the name appears only in the surrounding passage or book notes: "What does self-control require instead of fight-or-flight?" → "The pause-and-plan response."
- Keep the strength of the claim. If the text says "best", "most", "only" or "foundation", say it in the question: "What is the best way to improve self-control?"
- When the text lists parallel items, write one card that asks for the list (up to 4 items) instead of one card per item. Wrong: four cards like "How does sleep deprivation affect attention?" Right: "What does chronic mild sleep deprivation make harder?" → "Controlling emotions, focusing attention, finding energy for big challenges."
- Each card asks something different: another idea, or the same idea from the other direction. Never repeat a question in other words.]]

local WRITING_BASIC = [[Writing the card:
- Front: one short, specific question, ideally under 12 words.
- Back: the shortest complete answer, ideally 1 to 6 words. It may add a few words of takeaway after a full stop: "The enemy within, not the cake. Turn attention inward."
- The answer must not be guessable from the question, and must not repeat its words. Wrong: "How does sleep deprivation affect emotional control?" → "It makes emotions harder to control."
- Ask about the idea itself, as plain fact. Never attribute it to the author, the book or a character ("According to the author …", "In this book, …"). Quote: "Self-knowledge is the foundation of self-control." Wrong: "According to Kelly McGonigal, what is the foundation of self-control?" Right: "What is the foundation of self-control?" Naming a researcher or study the book cites is fine when it helps recall.
- The question must still make sense months later without the book open: name the specific thing it is about.
- Do not copy the whole highlight onto a card.
- Write in the language of the highlighted text.
- Plain text only, no Markdown.

Reply with JSON only, no other text, in this shape:
{"cards": [{"front": "question", "back": "answer"}]}]]

local WRITING_CLOZE = [[Writing the card:
- Write cloze cards: the front is a statement with the part to recall hidden as {{c1::hidden words}}. The examples above are questions and answers; state the same fact as a sentence and hide the answer: "Self-control requires the {{c1::pause-and-plan response}}, not fight-or-flight."
- Front: one short, self-contained sentence, ideally under 20 words.
- Hide what the question would ask for: the key term, finding or practice, ideally 1 to 6 words. Never hide filler words, and never hide most of the sentence.
- Use only c1, so each card is one flashcard. For a list, hide every item with c1: "Sleep deprivation makes it harder to {{c1::control emotions}}, {{c1::focus attention}} and {{c1::find energy}}."
- The hidden words must not be guessable from the rest of the sentence, and the rest must still give enough of a cue to recall them.
- Back: empty, or a few words of extra takeaway shown with the answer.
- State the idea itself, as plain fact. Never attribute it to the author, the book or a character ("According to the author …", "In this book, …"). Naming a researcher or study the book cites is fine when it helps recall.
- The sentence must still make sense months later without the book open: name the specific thing it is about.
- Do not copy the whole highlight onto a card.
- Write in the language of the highlighted text.
- Plain text only, no Markdown.

Reply with JSON only, no other text, in this shape:
{"cards": [{"front": "Sentence with {{c1::hidden words}}.", "back": ""}]}]]

M.SYSTEM_PROMPT = CHOOSING .. "\n\n" .. WRITING_BASIC
M.CLOZE_PROMPT = CHOOSING .. "\n\n" .. WRITING_CLOZE

local function line(label, value)
    if value == nil or value == "" then return nil end
    return label .. ": " .. tostring(value)
end

local function cardLine(card)
    if M.isCloze(card.front) and card.back == "" then return card.front end
    return card.front .. " → " .. card.back
end

local CLOZE_PATTERN = "{{c(%d+)::"

--- Whether `text` has a cloze deletion such as {{c1::answer}}.
function M.isCloze(text)
    return text ~= nil and text:find(CLOZE_PATTERN) ~= nil
end

--- `text` with each cloze deletion shown as [answer].
function M.showCloze(text)
    return (text:gsub("{{c%d+::(.-)}}", function(inner)
        return "[" .. (inner:match("^(.-)::") or inner) .. "]"
    end))
end

--- What the cloze button inserts at the cursor, given the text before it:
-- "}}" to close an open deletion, else the next opening such as "{{c2::".
-- @string before text before the cursor
-- @string all the whole text
function M.clozeMark(before, all)
    local last_open = 0
    for pos in before:gmatch("(){{c%d+::") do last_open = pos end
    if last_open > 0 and not before:find("}}", last_open, true) then
        return "}}"
    end
    local highest = 0
    for n in all:gmatch(CLOZE_PATTERN) do highest = math.max(highest, tonumber(n)) end
    return "{{c" .. (highest + 1) .. "::"
end

--- Whether the reader changed an AI card, which keeps its original text in `suggested`.
function M.wasEdited(card)
    local s = card.suggested
    return s ~= nil and (s.front ~= card.front or s.back ~= card.back)
end

--- Messages for Assistant's querier.
-- @tparam table h highlight: text, before, after, note, book, author, chapter, percent
-- @tparam table request `{count = n}` for exactly n cards, or `{max = n}` to let the AI choose 1 to n;
-- optional `existing`, a list of {front, back, suggested} the new cards must not repeat,
-- where `suggested` is the AI's original {front, back} of a card;
-- optional `rejected`, a list of {front, back} suggestions the reader removed;
-- optional `recent`, entries from the book's card history (see M.historyEntry);
-- optional `cloze`, true for cloze cards
-- @string companion optional Book Companion text
-- @string position_note optional Book Companion note about the reading position
function M.buildMessages(h, request, companion, position_note)
    local messages = {
        { role = "system", content = request.cloze and M.CLOZE_PROMPT or M.SYSTEM_PROMPT },
    }
    if companion and companion ~= "" then
        table.insert(messages, {
            role = "system",
            content = "Reference notes about this book (background only, never quiz on them directly):\n\n" .. companion,
            cache = true,
        })
    end

    local parts = {}
    local about = {
        line("Book", h.book),
        line("Author", h.author),
        line("Chapter", h.chapter),
        h.percent and string.format("Position in book: %d%%", math.floor(h.percent + 0.5)) or nil,
    }
    if #about > 0 then
        table.insert(parts, table.concat(about, "\n"))
    end
    local before, after = h.before or "", h.after or ""
    if before ~= "" or after ~= "" then
        table.insert(parts, "Passage around the highlight (the highlight is between « and »):\n\n"
            .. before .. " «" .. h.text .. "» " .. after)
    end
    table.insert(parts, "Highlighted text:\n\n" .. h.text)
    if h.note and h.note ~= "" then
        table.insert(parts, "The reader's note on this highlight. It says what they care about here, "
            .. "so let it steer which ideas you pick and how you ask:\n\n" .. h.note)
    end
    if position_note and position_note ~= "" then
        table.insert(parts, position_note)
    end
    if request.recent and #request.recent > 0 then
        table.insert(parts, M.recentSection(request.recent))
    end
    if request.existing and #request.existing > 0 then
        local lines, edits = {}, {}
        for _idx, card in ipairs(request.existing) do
            table.insert(lines, "- " .. cardLine(card))
            if M.wasEdited(card) then
                table.insert(edits, "- You suggested: " .. cardLine(card.suggested)
                    .. "\n  The reader changed it to: " .. cardLine(card))
            end
        end
        table.insert(parts, "The reader already has these cards. Write new ones that ask something else, "
            .. "in the same style and level of detail:\n\n" .. table.concat(lines, "\n"))
        if #edits > 0 then
            table.insert(parts, "The reader rewrote some of your earlier suggestions. "
                .. "Notice what they changed and write new cards the way they would:\n\n"
                .. table.concat(edits, "\n"))
        end
    end
    if request.rejected and #request.rejected > 0 then
        local lines = {}
        for _idx, card in ipairs(request.rejected) do
            table.insert(lines, "- " .. cardLine(card))
        end
        table.insert(parts, "The reader removed these suggested cards. Do not write them again or close "
            .. "variants of them. Notice what they have in common (for example too easy, too long, "
            .. "or about a side detail) and avoid that, while still basing new cards on the highlight:\n\n"
            .. table.concat(lines, "\n"))
    end
    table.insert(parts, M.countInstruction(request))

    table.insert(messages, { role = "user", content = table.concat(parts, "\n\n") })
    return messages
end

-- Longest quote shown with a past card
local HISTORY_QUOTE_CHARS = 300

local function shorten(text, limit)
    if #text <= limit then return text end
    local cut = text:sub(1, limit):gsub("%s+%S*$", "")
    return cut .. " …"
end

--- A saved card for the book's card history: the card, its AI original
-- when the AI wrote it, and the highlight it came from.
function M.historyEntry(h, card)
    return {
        front = card.front,
        back = card.back,
        suggested = card.suggested and { front = card.suggested.front, back = card.suggested.back } or nil,
        quote = h.text,
        note = h.note ~= "" and h.note or nil,
    }
end

--- Card history with `entries` added at the end, keeping the last `limit`.
function M.appendHistory(history, entries, limit)
    local all = {}
    for _idx, e in ipairs(history or {}) do table.insert(all, e) end
    for _idx, e in ipairs(entries) do table.insert(all, e) end
    local first = math.max(1, #all - limit + 1)
    local kept = {}
    for i = first, #all do table.insert(kept, all[i]) end
    return kept
end

--- Up to `limit` history entries to show the AI: the latest edited AI cards
-- first, then the latest others, in history order.
function M.pickRecent(history, limit)
    local chosen = {}
    local count = 0
    local function take(want_edited)
        for i = #history, 1, -1 do
            if count >= limit then return end
            if not chosen[i] and M.wasEdited(history[i]) == want_edited then
                chosen[i] = true
                count = count + 1
            end
        end
    end
    take(true)
    take(false)
    local picked = {}
    for i = 1, #history do
        if chosen[i] then table.insert(picked, history[i]) end
    end
    return picked
end

--- Past cards from this book, grouped by highlight, as a style guide for the AI.
function M.recentSection(recent)
    local blocks = {}
    local block, last_quote
    for _idx, e in ipairs(recent) do
        if e.quote ~= last_quote then
            last_quote = e.quote
            local lines = { "Highlight: «" .. shorten(e.quote or "", HISTORY_QUOTE_CHARS) .. "»" }
            if e.note then table.insert(lines, "Reader's note: " .. e.note) end
            block = lines
            table.insert(blocks, block)
        end
        if M.wasEdited(e) then
            table.insert(block, "- You suggested: " .. cardLine(e.suggested)
                .. "\n  The reader changed it to: " .. cardLine(e))
        else
            table.insert(block, "- " .. cardLine(e))
        end
    end
    local texts = {}
    for _idx, b in ipairs(blocks) do table.insert(texts, table.concat(b, "\n")) end
    return "Cards this reader saved recently from this book. They show what the reader chooses to ask "
        .. "about and how they word questions and answers, including how they rewrote your suggestions. "
        .. "Write the new cards the same way, but do not repeat these:\n\n" .. table.concat(texts, "\n\n")
end

--- How many cards to write, as an instruction for the AI.
function M.countInstruction(request)
    if request.max then
        if request.max <= 1 then return "Write 1 card." end
        return string.format("Decide how many cards this highlight deserves, from 1 to %d. "
            .. "A short, focused highlight with one idea gets 1 card. "
            .. "Write more only when it holds several distinct facts or ideas worth remembering, one card each.",
            request.max)
    end
    if request.count == 1 then return "Write 1 card." end
    return string.format("Write %d cards, each asking something different.", request.count)
end

--- Most cards to accept from the reply.
function M.cardLimit(request)
    return request.max or request.count
end

local function trim(s)
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

--- The JSON part of a reply, without code fences or surrounding prose.
function M.extractJson(text)
    if type(text) ~= "string" then return nil end
    local fenced = text:match("```[%w]*%s*(.-)%s*```")
    if fenced then text = fenced end
    local first = text:find("[%[{]")
    if not first then return nil end
    local closer = text:sub(first, first) == "{" and "}" or "]"
    local last
    for i = #text, first, -1 do
        if text:sub(i, i) == closer then
            last = i
            break
        end
    end
    if not last then return nil end
    return text:sub(first, last)
end

--- Cards from a reply: a list of {front, back}, at most `count`.
-- @func decode JSON decoder (string -> table), errors on bad input
-- @return cards, or nil and an error message
function M.parseCards(text, count, decode)
    local json = M.extractJson(text)
    if not json then
        return nil, "The AI reply had no cards."
    end
    local ok, data = pcall(decode, json)
    if not ok or type(data) ~= "table" then
        return nil, "The AI reply could not be read."
    end
    local list = data.cards or data
    if type(list) ~= "table" then
        return nil, "The AI reply had no cards."
    end
    local cards = {}
    for _idx, item in ipairs(list) do
        if type(item) == "table" and type(item.front) == "string" then
            local front = trim(item.front)
            local back = type(item.back) == "string" and trim(item.back) or ""
            if front ~= "" then
                table.insert(cards, { front = front, back = back })
            end
        end
        if count and #cards >= count then break end
    end
    if #cards == 0 then
        return nil, "The AI reply had no cards."
    end
    return cards
end

return M
