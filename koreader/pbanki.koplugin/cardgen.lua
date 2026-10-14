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
- The question must make sense months later without the book open: name the person, place or idea it is about.
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
-- @int count cards to ask for
-- @string companion optional Book Companion text
-- @string position_note optional Book Companion note about the reading position
function M.buildMessages(h, count, companion, position_note)
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
    table.insert(parts, count == 1 and "Write 1 card."
        or string.format("Write %d cards, each asking something different.", count))

    table.insert(messages, { role = "user", content = table.concat(parts, "\n\n") })
    return messages
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
