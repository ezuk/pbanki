--[[--
Builds the AI request for flashcard suggestions and parses the reply.

No KOReader dependencies, so it runs under plain Lua in spec/.

@module koplugin.pbanki.cardgen
--]]--

local M = {}

M.SYSTEM_PROMPT = [[You write Anki flashcards from a passage a reader highlighted in a book.

Rules:
- Base every card tightly on the highlighted text. Use the surrounding passage and book notes only to understand it.
- Each card asks something different: another fact, or the same fact from the other direction ("What is the capital of France?" / "Paris is the capital of which country?"). Never repeat a question in other words.
- Front: one short, specific question, ideally under 12 words.
- Back: the shortest complete answer, ideally 1 to 5 words, never more than one sentence.
- Ask about the idea itself, as plain fact. Never attribute it to the author, the book or a character ("According to …", "In this book, …", "What does the author say …"). Quote: "Self-knowledge is the foundation of self-control." Wrong: "According to Kelly McGonigal, what is the foundation of self-control?" Right: "What is the foundation of self-control?"
- The question must still make sense months later without the book open: name the specific thing it is about when the highlight does.
- Do not copy the whole highlight onto a card.
- Write in the language of the highlighted text.
- Plain text only, no Markdown.

Reply with JSON only, no other text, in this shape:
{"cards": [{"front": "question", "back": "answer"}]}]]

local function line(label, value)
    if value == nil or value == "" then return nil end
    return label .. ": " .. tostring(value)
end

--- Messages for Assistant's querier.
-- @tparam table h highlight: text, before, after, note, book, author, chapter, percent
-- @tparam table request `{count = n}` for exactly n cards, or `{max = n}` to let the AI choose 1 to n;
-- optional `existing`, a list of {front, back} the new cards must not repeat
-- @string companion optional Book Companion text
-- @string position_note optional Book Companion note about the reading position
function M.buildMessages(h, request, companion, position_note)
    local messages = {
        { role = "system", content = M.SYSTEM_PROMPT },
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
        table.insert(parts, "The reader's note on this highlight:\n\n" .. h.note)
    end
    if position_note and position_note ~= "" then
        table.insert(parts, position_note)
    end
    if request.existing and #request.existing > 0 then
        local lines = {}
        for _idx, card in ipairs(request.existing) do
            table.insert(lines, "- " .. card.front .. " → " .. card.back)
        end
        table.insert(parts, "The reader already has these cards. Write new ones that ask something else:\n\n"
            .. table.concat(lines, "\n"))
    end
    table.insert(parts, M.countInstruction(request))

    table.insert(messages, { role = "user", content = table.concat(parts, "\n\n") })
    return messages
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
