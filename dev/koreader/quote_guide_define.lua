-- Cloze cards that quote the book, guidance for Suggest more, and definition
-- cards for a short selection.
local Event = require("ui/event")
local UIManager = require("ui/uimanager")

local function top()
    local stack = UIManager._window_stack
    return stack[#stack].widget
end
local function closeTop() UIManager:close(top()) end
local function editor()
    for i = #UIManager._window_stack, 1, -1 do
        local w = UIManager._window_stack[i].widget
        if w.setCards then return w end
    end
end
local function select(ui, x0, y0, x1, y1)
    local sel = ui.document:getTextFromPositions({ x = x0, y = y0 }, { x = x1, y = y1 })
    ui.highlight.selected_text = sel
    ui.highlight.hold_pos = { x = x0, y = y0, page = ui.document:getCurrentPage() }
    io.stderr:write("uxdriver selected: ", tostring(sel and sel.text), "\n")
    return sel
end
local function pick(count)
    local spin = top()
    spin.value = count
    UIManager:close(spin)
    spin.callback(spin)
end
local function cards(tag)
    local e = editor()
    local lines = {}
    for i, c in ipairs(e.cards) do table.insert(lines, i .. ". " .. c.front .. " → " .. c.back) end
    io.stderr:write("uxdriver ", tag, ": ", table.concat(lines, " | "), "\n")
end
local function tapButton(text)
    local found
    local function search(widget)
        if type(widget) ~= "table" or found then return end
        if widget.text and widget.text:find(text, 1, true) and widget.callback then
            found = widget
            return
        end
        for _idx, child in ipairs(widget) do search(child) end
        if widget.button_table then search(widget.button_table) end
        if widget.buttons_layout then search(widget.buttons_layout) end
        if widget.layout then for _r, row in ipairs(widget.layout) do search(row) end end
    end
    search(top())
    io.stderr:write("uxdriver tap ", text, " ", tostring(found ~= nil), "\n")
    found.callback()
end
local function dismissInfo()
    for i = #UIManager._window_stack, 1, -1 do
        local w = UIManager._window_stack[i].widget
        if w.modal and w.text and not w.setCards then UIManager:close(w) end
    end
end
local function waitAI() return tonumber(os.getenv("UX_AI_WAIT")) or 25 end

return {
    function(ui) ui:handleEvent(Event:new("GotoPage", 35)) end,
    -- Cloze cards quoted from the highlight
    function(ui)
        ui.pbanki.settings.cloze = true
        select(ui, 30, 268, 140, 355)
        ui.pbanki:createFromSelection(ui.highlight, nil)
    end,
    function(ui) pick(2); return waitAI() end,
    function(ui, shot) cards("cloze"); shot("26_cloze_quoted") end,
    -- Suggest more with guidance
    function(ui) local e = editor(); e.on_suggest(e) end,
    function(ui, shot) shot("27_guidance_empty") end,
    function(ui) top()._input_widget:addChars("Hide just the last word") end,
    function(ui, shot) shot("28_guidance_typed") end,
    function(ui) top().button_table:getButtonById("done").callback() end,
    function(ui, shot) shot("29_guidance_count") end,
    function(ui) pick(1); return waitAI() end,
    function(ui, shot) cards("guided"); shot("30_guided_added") end,
    function(ui) dismissInfo(); local e = editor(); e:onClose() end,
    -- Definition cards for a few words
    function(ui)
        ui.pbanki.settings.cloze = false
        dismissInfo()
        select(ui, 60, 674, 170, 674)
        ui.highlight:onShowHighlightMenu()
    end,
    function(ui, shot) shot("31_definition_menu") end,
    function(ui) dismissInfo(); tapButton("definition"); return waitAI() end,
    function(ui, shot) cards("definition"); shot("32_definition_cards") end,
    function(ui) local e = editor(); e.on_suggest(e) end,
    function(ui) top()._input_widget:addChars("Simpler words") end,
    function(ui) top().button_table:getButtonById("done").callback(); return waitAI() end,
    function(ui, shot) cards("definition more"); shot("33_definition_more") end,
    function(ui)
        local e = editor()
        e.on_save(e, e.cards)
        return 2.5
    end,
    function(ui)
        local f = io.open(os.getenv("PBANKI_DIR") .. "/inbox.jsonl")
        io.stderr:write("uxdriver inbox ", f and f:read("*a") or "none", "\n")
        if f then f:close() end
        closeTop()
    end,
}
