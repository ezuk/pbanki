--[[--
The reader's AI prompts: the built-in Standard prompt and saved copies.

A prompt has an id, a name and instructions for each kind of card. Saved
prompts keep only the instructions the reader changed; the others come from
Standard.

No KOReader dependencies, so it runs under plain Lua in spec/.

@module koplugin.pbanki.prompts
--]]--

local cardgen = require("cardgen")

local M = {}

M.STANDARD_ID = "standard"

M.KINDS = {
    { key = "questions", label = "Question cards" },
    { key = "cloze", label = "Cloze cards" },
    { key = "definition", label = "Definition cards" },
}

M.STANDARD = {
    id = M.STANDARD_ID,
    name = "Standard",
    builtin = true,
}

--- Standard first, then the saved prompts.
function M.all(saved)
    local list = { M.STANDARD }
    for _idx, p in ipairs(saved or {}) do table.insert(list, p) end
    return list
end

--- The prompt with `id`, or nil.
function M.find(saved, id)
    if id == nil then return nil end
    for _idx, p in ipairs(M.all(saved)) do
        if p.id == id then return p end
    end
end

--- The prompt a book uses: its own choice, else the default, else Standard.
function M.resolve(saved, book_id, default_id)
    return M.find(saved, book_id) or M.find(saved, default_id) or M.STANDARD
end

--- Instructions of `prompt` for `kind`, falling back to Standard's.
function M.text(prompt, kind)
    local text = prompt[kind]
    if type(text) == "string" and not text:match("^%s*$") then return text end
    return cardgen.PROMPTS[kind]
end

--- Whether `prompt` has its own instructions for `kind`.
function M.isEdited(prompt, kind)
    return M.text(prompt, kind) ~= cardgen.PROMPTS[kind]
end

--- Stores `text` as `prompt`'s instructions for `kind`; Standard's text or
-- blank text clears them.
function M.setText(prompt, kind, text)
    if text == nil or text:match("^%s*$") or text == cardgen.PROMPTS[kind] then
        prompt[kind] = nil
    else
        prompt[kind] = text
    end
end

local function nameTaken(saved, name)
    for _idx, p in ipairs(M.all(saved)) do
        if p.name == name then return true end
    end
    return false
end

--- A free name for a copy of `name`: "Standard copy", "Standard copy 2", …
function M.copyName(saved, name)
    local base = name .. " copy"
    local candidate, n = base, 1
    while nameTaken(saved, candidate) do
        n = n + 1
        candidate = base .. " " .. n
    end
    return candidate
end

local function newId(saved)
    local highest = 0
    for _idx, p in ipairs(saved) do
        local n = tonumber(tostring(p.id):match("^p(%d+)$"))
        if n and n > highest then highest = n end
    end
    return "p" .. (highest + 1)
end

--- Adds a copy of `source` named `name` to `saved` and returns it.
function M.duplicate(saved, source, name)
    local copy = { id = newId(saved), name = name }
    for _idx, kind in ipairs(M.KINDS) do
        if source[kind.key] then copy[kind.key] = source[kind.key] end
    end
    table.insert(saved, copy)
    return copy
end

--- Removes the saved prompt with `id`.
function M.remove(saved, id)
    for i, p in ipairs(saved) do
        if p.id == id then
            table.remove(saved, i)
            return
        end
    end
end

return M
