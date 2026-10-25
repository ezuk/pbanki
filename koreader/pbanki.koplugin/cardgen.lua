--[[--
Builds the AI request for flashcard suggestions and parses the reply.

No KOReader dependencies, so it runs under plain Lua in spec/.

@module koplugin.pbanki.cardgen
--]]--

local prompts = require("prompts")

local M = {}

--- Built-in instructions for each kind of card, without the reply format.
M.PROMPTS = prompts.read(prompts.BUILTIN_DIR .. "/" .. prompts.STANDARD)

--- The reply format for each kind of card, added after its instructions.
M.REPLY_FORMATS = {
    questions = [[Reply with JSON only, no other text, in this shape:
{"cards": [{"front": "question", "back": "answer"}]}]],
    cloze = [[Reply with JSON only, no other text, in this shape:
{"cards": [{"front": "Sentence quoted from the text with {{c1::hidden words}}.", "back": ""}]}]],
    definition = [[Reply with JSON only, no other text, in this shape:
{"term": "term", "definition": "definition"}]],
}

--- The kind of card a request asks for: "questions", "cloze" or "definition".
function M.requestKind(request)
    if request.definition then return "definition" end
    if request.cloze then return "cloze" end
    return "questions"
end

--- System prompt for `kind`: the instructions from `prompt` (a table keyed
-- by kind, see prompts.lua), or the built-in ones, then the reply format.
function M.systemPrompt(prompt, kind)
    local text = prompt and prompt[kind]
    if type(text) ~= "string" or text:match("^%s*$") then
        text = M.PROMPTS[kind]
    end
    return text .. "\n\n" .. M.REPLY_FORMATS[kind]
end

M.SYSTEM_PROMPT = M.systemPrompt(nil, "questions")
M.CLOZE_PROMPT = M.systemPrompt(nil, "cloze")
M.DEFINITION_PROMPT = M.systemPrompt(nil, "definition")

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

--- `body` inside <tag> … </tag>, after an optional `intro` line.
local function tagged(tag, body, intro)
    local block = "<" .. tag .. ">\n" .. body .. "\n</" .. tag .. ">"
    if intro then return intro .. "\n" .. block end
    return block
end

--- Messages for Assistant's querier: the Book Companion first, then the
-- instructions, then the highlight and the task.
-- @tparam table h highlight: text, before, after, note, book, author, chapter, percent
-- @tparam table request `{count = n}` for exactly n cards, or `{max = n}` to let the AI choose 1 to n;
-- optional `existing`, a list of {front, back, suggested} the new cards must not repeat,
-- where `suggested` is the AI's original {front, back} of a card;
-- optional `rejected`, a list of {front, back} suggestions the reader removed;
-- optional `recent`, entries from the book's card history (see M.historyEntry);
-- optional `cloze`, true for cloze cards;
-- optional `definition`, true for a term and definition pair (see M.parseDefinition);
-- optional `guidance`, the reader's own instructions for these cards
-- optional `prompt`, instructions by kind of card (see M.systemPrompt)
-- @string companion optional Book Companion text
-- @string position_note optional Book Companion note about the reading position
function M.buildMessages(h, request, companion, position_note)
    local messages = {}
    if companion and companion ~= "" then
        table.insert(messages, {
            role = "system",
            content = "Reference notes about this book (background only, never quiz on them directly):\n\n"
                .. tagged("book_notes", companion),
            cache = true,
        })
    end
    table.insert(messages, { role = "system", content = M.systemPrompt(request.prompt, M.requestKind(request)) })

    local parts = {}
    local about = {
        line("Book", h.book),
        line("Author", h.author),
        line("Chapter", h.chapter),
        h.percent and string.format("Position in book: %d%%", math.floor(h.percent + 0.5)) or nil,
    }
    if #about > 0 then
        table.insert(parts, tagged("book", table.concat(about, "\n")))
    end
    local before, after = h.before or "", h.after or ""
    if before ~= "" or after ~= "" then
        table.insert(parts, tagged("passage", before .. " «" .. h.text .. "» " .. after,
            "Passage around the highlight (the highlight is between « and »):"))
    end
    table.insert(parts, tagged("highlight", h.text, "Highlighted text:"))
    if h.note and h.note ~= "" then
        table.insert(parts, tagged("reader_note", h.note, "The reader's note on this highlight. It says what "
            .. "they care about here, so let it steer which ideas you pick and how you ask:"))
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
        if request.definition then
            table.insert(parts, tagged("existing_cards", table.concat(lines, "\n"),
                "The reader already has these cards for this term. Write a different definition:"))
        else
            table.insert(parts, tagged("existing_cards", table.concat(lines, "\n"),
                "The reader already has these cards. Write new ones that ask something else, "
                .. "in the same style and level of detail:"))
        end
        if #edits > 0 then
            table.insert(parts, tagged("edited_cards", table.concat(edits, "\n"),
                "The reader rewrote some of your earlier suggestions. "
                .. "Notice what they changed and write new cards the way they would:"))
        end
    end
    if request.rejected and #request.rejected > 0 then
        local lines = {}
        for _idx, card in ipairs(request.rejected) do
            table.insert(lines, "- " .. cardLine(card))
        end
        table.insert(parts, tagged("removed_cards", table.concat(lines, "\n"),
            "The reader removed these suggested cards. Do not write them again or close "
            .. "variants of them. Notice what they have in common (for example too easy, too long, "
            .. "or about a side detail) and avoid that, while still basing new cards on the highlight:"))
    end
    if request.guidance and request.guidance ~= "" then
        table.insert(parts, tagged("reader_request", request.guidance, "The reader asked for these cards:")
            .. "\nFollow it, within the rules above.")
    end
    if not request.definition then
        table.insert(parts, M.countInstruction(request))
    end
    table.insert(parts, "Reply with the JSON only, in the shape given in the instructions.")

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
    return tagged("recent_cards", table.concat(texts, "\n\n"),
        "Cards this reader saved recently from this book. They show what the reader chooses to ask "
        .. "about and how they word questions and answers, including how they rewrote your suggestions. "
        .. "Write the new cards the same way, but do not repeat these:")
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
    if request.cloze then
        return string.format("Write %d cards, each hiding different words.", request.count)
    end
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

--- Lowercase words of `text`, ignoring punctuation, joined by single spaces.
local function words(text)
    text = text:gsub("\226\128[\152\153]", "'")
        :gsub("\226\128[\147\148\156\157\166]", " ")
        :gsub("\194\160", " ")
        :gsub("\194\173", "")
    local list = {}
    for found in text:gmatch("[%w\128-\255']+") do
        local w = found:gsub("^'+", ""):gsub("'+$", "")
        if w ~= "" then table.insert(list, w:lower()) end
    end
    return table.concat(list, " ")
end

--- Whether cloze `front`, without its marks, quotes `source` word for word.
function M.quotesText(front, source)
    local plain = front:gsub("{{c%d+::(.-)}}", function(inner)
        return inner:match("^(.-)::") or inner
    end)
    local quote = words(plain)
    return quote ~= "" and (" " .. words(source) .. " "):find(" " .. quote .. " ", 1, true) ~= nil
end

--- Cloze `front` widened to the whole highlight `text` when it quotes only
-- part of it, word for word.
function M.expandCloze(front, text)
    local plain = front:gsub("{{c%d+::(.-)}}", function(inner)
        return inner:match("^(.-)::") or inner
    end)
    local first, last = text:find(plain, 1, true)
    if not first or #plain == #text then return front end
    return text:sub(1, first - 1) .. front .. text:sub(last + 1)
end

--- The term and definition in a reply as two cards: term → definition and
-- definition → term.
-- @return cards, or nil and an error message
function M.parseDefinition(text, decode)
    local json = M.extractJson(text)
    local ok, data = pcall(decode, json or "")
    if not json or not ok or type(data) ~= "table" then
        return nil, "The AI reply could not be read."
    end
    local term = type(data.term) == "string" and trim(data.term) or ""
    local definition = type(data.definition) == "string" and trim(data.definition) or ""
    if term == "" or definition == "" then
        return nil, "The AI reply had no definition."
    end
    return {
        { front = term, back = definition },
        { front = definition, back = term },
    }
end

--- Cards from a reply to `request` (see M.buildMessages) for highlight `h`.
-- Cloze cards that do not quote the book are dropped; the rest cover the
-- whole highlight.
-- @return cards, or nil and an error message
function M.parseReply(text, request, h, decode)
    if request.definition then
        return M.parseDefinition(text, decode)
    end
    local limit = M.cardLimit(request)
    if not request.cloze then
        return M.parseCards(text, limit, decode)
    end
    local cards, err = M.parseCards(text, nil, decode)
    if not cards then return nil, err end
    local source = table.concat({ h.before or "", h.text or "", h.after or "" }, " ")
    local kept = {}
    for _idx, card in ipairs(cards) do
        if M.isCloze(card.front) and M.quotesText(card.front, source) then
            card.front = M.expandCloze(card.front, h.text or "")
            table.insert(kept, card)
        end
        if limit and #kept >= limit then break end
    end
    if #kept == 0 then
        return nil, "The AI's cloze cards did not quote the book. Try Suggest again."
    end
    return kept
end

return M
