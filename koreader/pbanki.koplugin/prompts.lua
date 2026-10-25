--[[--
AI prompts kept as Markdown files, one folder per prompt.

    prompts/
        Standard/           the built-in prompt, rewritten by the plugin
            questions.md
            cloze.md
            definitions.md
        Concise/            the reader's own prompt
            questions.md    kinds without a file use Standard's

No KOReader dependencies, so it runs under plain Lua in spec/.

@module koplugin.pbanki.prompts
--]]--

local cardgen = require("cardgen")

local M = {}

M.STANDARD = "Standard"

-- The first file name is the one written for Standard
M.KINDS = {
    { key = "questions", label = "questions", files = { "questions.md", "question.md" } },
    { key = "cloze", label = "cloze", files = { "cloze.md" } },
    { key = "definition", label = "definitions", files = { "definitions.md", "definition.md" } },
}

M.README = [[# pbAnki prompts

Each folder here is a prompt the AI can follow when it writes flashcards. Pick one in KOReader under Tools → pbAnki flashcards → AI prompt, for all books or just the book you are reading.

A folder can hold up to three Markdown files:

- `questions.md` for question and answer cards
- `cloze.md` for cloze cards
- `definitions.md` for definition cards

A missing file means that kind of card uses the Standard prompt.

To make your own prompt, copy the Standard folder, rename the copy and edit its files. The plugin rewrites Standard whenever a book opens, so edits made there are lost.

The plugin adds the reply format itself, so the files only need the instructions.
]]

local function trim(s)
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

--- Markdown `text` tidied for the AI: Unix line ends, no trailing spaces,
-- single blank lines between paragraphs, and headings set apart.
function M.normalize(text)
    text = text:gsub("^\239\187\191", ""):gsub("\r\n?", "\n"):gsub("[ \t]+\n", "\n"):gsub("[ \t]+$", "")
    text = ("\n" .. text .. "\n"):gsub("\n(#+[ \t][^\n]*)", "\n\n%1\n\n")
    return trim((text:gsub("\n\n\n+", "\n\n")))
end

local function readFile(path)
    local file = io.open(path, "r")
    if not file then return nil end
    local text = file:read("*a")
    file:close()
    return text
end

--- Instructions by kind from prompt folder `dir`, without kinds that have
-- no file or an empty one.
function M.read(dir)
    local prompt = {}
    for _idx, kind in ipairs(M.KINDS) do
        for _idx2, name in ipairs(kind.files) do
            local text = readFile(dir .. "/" .. name)
            if text then
                text = M.normalize(text)
                if text ~= "" then
                    prompt[kind.key] = text
                    break
                end
            end
        end
    end
    return prompt
end

--- Labels of the kinds `prompt` has its own instructions for.
function M.ownKinds(prompt)
    local labels = {}
    for _idx, kind in ipairs(M.KINDS) do
        if prompt[kind.key] then table.insert(labels, kind.label) end
    end
    return labels
end

--- Standard first, then the other folder names in alphabetical order.
function M.names(folders)
    local others = {}
    for _idx, name in ipairs(folders) do
        if name ~= M.STANDARD then table.insert(others, name) end
    end
    table.sort(others, function(a, b) return a:lower() < b:lower() end)
    table.insert(others, 1, M.STANDARD)
    return others
end

--- The prompt a book uses: its own choice, else the default, else Standard,
-- skipping folders that no longer exist.
function M.choose(folders, book_choice, default)
    local function exists(wanted)
        for _idx, name in ipairs(folders) do
            if name == wanted then return true end
        end
        return false
    end
    if exists(book_choice) then return book_choice end
    if exists(default) then return default end
    return M.STANDARD
end

--- Files for the Standard folder, as {name, text}.
function M.standardFiles()
    local files = {}
    for _idx, kind in ipairs(M.KINDS) do
        table.insert(files, { name = kind.files[1], text = cardgen.PROMPTS[kind.key] .. "\n" })
    end
    return files
end

return M
