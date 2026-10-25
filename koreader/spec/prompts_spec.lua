-- Run from the repo root: lua koreader/spec/prompts_spec.lua
package.path = "koreader/pbanki.koplugin/?.lua;" .. package.path
local cardgen = require("cardgen")
local prompts = require("prompts")

local function eq(got, want, msg)
    assert(got == want, msg .. ": expected [" .. tostring(want) .. "] got [" .. tostring(got) .. "]")
end

-- Standard is always there and uses the built-in instructions
local saved = {}
eq(#prompts.all(saved), 1, "only Standard at first")
eq(prompts.resolve(saved, nil, nil), prompts.STANDARD, "Standard by default")
eq(prompts.text(prompts.STANDARD, "cloze"), cardgen.PROMPTS.cloze, "Standard cloze text")
eq(prompts.isEdited(prompts.STANDARD, "questions"), false, "Standard not edited")

-- Duplicating keeps only changed instructions
eq(prompts.copyName(saved, "Standard"), "Standard copy", "first copy name")
local mine = prompts.duplicate(saved, prompts.STANDARD, "Mine")
eq(mine.id, "p1", "first id")
eq(mine.questions, nil, "copy of Standard stores no text")
eq(prompts.find(saved, "p1"), mine, "found by id")

prompts.setText(mine, "questions", "Ask short questions.")
eq(prompts.text(mine, "questions"), "Ask short questions.", "edited text")
eq(prompts.isEdited(mine, "questions"), true, "marked edited")
eq(prompts.text(mine, "definition"), cardgen.PROMPTS.definition, "other kinds follow Standard")
prompts.setText(mine, "cloze", cardgen.PROMPTS.cloze)
eq(mine.cloze, nil, "Standard text is not stored")
prompts.setText(mine, "cloze", "   ")
eq(mine.cloze, nil, "blank text is not stored")

local copy = prompts.duplicate(saved, mine, prompts.copyName(saved, "Mine"))
eq(copy.name, "Mine copy", "copy of a saved prompt")
eq(copy.id, "p2", "next id")
eq(copy.questions, "Ask short questions.", "copy keeps edits")
eq(prompts.copyName(saved, "Mine"), "Mine copy 2", "numbered copy name")

-- A book's choice wins over the default; missing ids fall back
eq(prompts.resolve(saved, "p2", "p1"), copy, "book's own prompt")
eq(prompts.resolve(saved, nil, "p1"), mine, "default prompt")
prompts.remove(saved, "p2")
eq(#prompts.all(saved), 2, "removed")
eq(prompts.resolve(saved, "p2", "p1"), mine, "deleted book prompt falls back to default")
eq(prompts.resolve(saved, "p2", "p9"), prompts.STANDARD, "then to Standard")
eq(prompts.duplicate(saved, mine, "Again").id, "p2", "ids reused after the highest is removed")

-- The prompt reaches the AI, with the reply format kept
local msgs = cardgen.buildMessages({ text = "x" }, { count = 1, prompt = mine })
eq(msgs[1].content, "Ask short questions.\n\n" .. cardgen.REPLY_FORMATS.questions, "custom questions prompt")
msgs = cardgen.buildMessages({ text = "x" }, { count = 1, cloze = true, prompt = mine })
eq(msgs[1].content, cardgen.CLOZE_PROMPT, "unedited kind uses Standard")
msgs = cardgen.buildMessages({ text = "x" }, { count = 1, prompt = prompts.STANDARD })
eq(msgs[1].content, cardgen.SYSTEM_PROMPT, "Standard prompt unchanged")

print("prompts_spec: OK")
