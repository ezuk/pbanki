-- Runs the plugin's cardgen.lua and prompts.lua, and the Assistant plugin's slicer, on JSON
-- cases read from stdin. Each case is {"fn": name, "args": [...]}; results go to stdout as
-- a JSON list of {"value": ...} or {"error": message}.
-- Run from the repo root with the test folder and plugin folder on package.path.
local json = require("tinyjson")
local cardgen = require("cardgen")
local prompts = require("prompts")

local unpack = table.unpack or unpack

-- Stands for JSON null in cases and in replies, not a table, like cjson.null
local NULL = function() end

local function decode(text)
    return json.decode(text, NULL)
end

local function upvalue(fn, wanted)
    local i = 1
    while true do
        local name, value = debug.getupvalue(fn, i)
        if not name then error("no upvalue " .. wanted) end
        if name == wanted then return value end
        i = i + 1
    end
end

local function pair(first, second)
    return { first = first, second = second }
end

local function slicer()
    return require("assistant_companion_slicer")
end

local function readFile(path)
    local f = assert(io.open(path, "rb"))
    local text = f:read("*a")
    f:close()
    return text
end

local FUNCTIONS = {
    requestKind = cardgen.requestKind,
    systemPrompt = cardgen.systemPrompt,
    isCloze = cardgen.isCloze,
    showCloze = cardgen.showCloze,
    clozeMark = cardgen.clozeMark,
    wasEdited = cardgen.wasEdited,
    questionsOnly = cardgen.questionsOnly,
    buildMessages = cardgen.buildMessages,
    historyEntry = cardgen.historyEntry,
    appendHistory = cardgen.appendHistory,
    pickRecent = cardgen.pickRecent,
    recentSection = cardgen.recentSection,
    countInstruction = cardgen.countInstruction,
    cardLimit = cardgen.cardLimit,
    extractJson = cardgen.extractJson,
    quotesText = cardgen.quotesText,
    expandCloze = cardgen.expandCloze,
    words = function(text) return upvalue(cardgen.quotesText, "words")(text) end,
    shorten = function(text, limit) return upvalue(cardgen.recentSection, "shorten")(text, limit) end,
    parseCards = function(text, count) return pair(cardgen.parseCards(text, count, decode)) end,
    parseDefinition = function(text) return pair(cardgen.parseDefinition(text, decode)) end,
    parseReply = function(text, request, h) return pair(cardgen.parseReply(text, request, h, decode)) end,
    normalize = prompts.normalize,
    promptNames = prompts.names,
    promptChoose = prompts.choose,
    promptRead = prompts.read,
    slice = function(text, percent, keep)
        local out, info = slicer().slice(text, percent, keep and { keep_current_synopsis = true } or nil)
        return { text = out, info = info }
    end,
    sliceFile = function(path, percent, keep)
        local out, info = slicer().slice(readFile(path), percent, keep and { keep_current_synopsis = true } or nil)
        return { text = out, info = info }
    end,
    positionNote = function(info) return slicer().positionNote(info) end,
}

local cases = json.decode(io.read("*a"), NULL)
local results = {}
for index, case in ipairs(cases) do
    local args, count = {}, #case.args
    for i = 1, count do
        if case.args[i] ~= NULL then args[i] = case.args[i] end
    end
    local ok, value = pcall(FUNCTIONS[case.fn], unpack(args, 1, count))
    if ok then
        results[index] = { value = value, ok = true }
    else
        results[index] = { error = tostring(value), ok = false }
    end
end
io.write(json.encode(results))
