-- Run from the repo root: lua koreader/spec/prompts_spec.lua
package.path = "koreader/pbanki.koplugin/?.lua;" .. package.path
local cardgen = require("cardgen")
local prompts = require("prompts")

local function eq(got, want, msg)
    assert(got == want, msg .. ": expected [" .. tostring(want) .. "] got [" .. tostring(got) .. "]")
end

local function write(path, text)
    local f = assert(io.open(path, "w"))
    f:write(text)
    f:close()
end

-- Markdown is tidied: line ends, trailing spaces, blank lines, headings
eq(prompts.normalize("\239\187\191One  \r\nline\r\n\r\n\r\n\r\nTwo\n"), "One\nline\n\nTwo", "BOM, CRLF, blank lines")
eq(prompts.normalize("Intro\n## Rules\n- a\n- b"), "Intro\n\n## Rules\n\n- a\n- b", "headings set apart")
eq(prompts.normalize("# Title\n\nText"), "# Title\n\nText", "tidy text unchanged")
eq(prompts.normalize("Use #tags\n"), "Use #tags", "hash inside a line is not a heading")

-- A folder with one file uses Standard for the other kinds
local dir = os.tmpname()
os.remove(dir)
assert(os.execute("mkdir -p '" .. dir .. "'"))
write(dir .. "/definitions.md", "Define it simply.\r\n")
local prompt = prompts.read(dir)
eq(prompt.definition, "Define it simply.", "definitions read")
eq(prompt.questions, nil, "no questions file")
eq(table.concat(prompts.ownKinds(prompt), ","), "definitions", "own kinds")
eq(cardgen.buildMessages({ text = "x" }, { definition = true, prompt = prompt })[1].content,
    "Define it simply.\n\n" .. cardgen.REPLY_FORMATS.definition, "definition from the file")
eq(cardgen.buildMessages({ text = "x" }, { count = 1, prompt = prompt })[1].content,
    cardgen.SYSTEM_PROMPT, "questions fall back to Standard")
write(dir .. "/question.md", "Ask briefly.")
write(dir .. "/cloze.md", "  \n")
prompt = prompts.read(dir)
eq(prompt.questions, "Ask briefly.", "singular file name accepted")
eq(prompt.cloze, nil, "empty file falls back")
os.execute("rm -rf '" .. dir .. "'")

-- Choosing a folder
local folders = { "zeta", "Standard", "Concise" }
eq(table.concat(prompts.names(folders), ","), "Standard,Concise,zeta", "Standard first, then sorted")
eq(table.concat(prompts.names({}), ","), "Standard", "Standard even without folders")
eq(prompts.choose(folders, "zeta", "Concise"), "zeta", "book's choice")
eq(prompts.choose(folders, nil, "Concise"), "Concise", "default")
eq(prompts.choose(folders, "gone", "Concise"), "Concise", "missing book choice falls back")
eq(prompts.choose(folders, "gone", "also gone"), "Standard", "then Standard")

-- Built-in files ship in the plugin folder and are the built-in instructions
local files = prompts.builtinFiles()
eq(#files, 4, "README and three Standard files")
eq(files[1].name, "README.md", "README first")
eq(files[4].name, "Standard/definitions.md", "definitions file name")
eq(prompts.normalize(files[2].text), cardgen.PROMPTS.questions, "questions from the file")
eq(prompts.normalize(files[3].text), cardgen.PROMPTS.cloze, "cloze from the file")
assert(cardgen.PROMPTS.definition:find("dictionary headword", 1, true), "definition from the file")

print("prompts_spec: OK")
