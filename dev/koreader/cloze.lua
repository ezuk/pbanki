-- Cloze cards: the Cloze checkbox when asking for suggestions, and the Cloze
-- button when writing a card.
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
    return sel
end
local function tapButton(text)
    local dialog = top()
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
    search(dialog)
    io.stderr:write("uxdriver tap ", text, " ", tostring(found ~= nil), "\n")
    found.callback()
end
local function waitAI() return tonumber(os.getenv("UX_AI_WAIT")) or 20 end
local function typeText(text) top()._input_widget:addChars(text) end

return {
    function(ui) ui:handleEvent(Event:new("GotoPage", 35)) end,
    function(ui, shot)
        select(ui, 30, 268, 140, 355)
        ui.pbanki:createFromSelection(ui.highlight, nil)
    end,
    function(ui, shot) shot("19_cloze_off") end,
    function(ui, shot) tapButton("Cloze") end,
    function(ui, shot) shot("20_cloze_on") end,
    function(ui, shot)
        local spin = top()
        spin.value = 2
        UIManager:close(spin)
        spin.callback(spin)
        return waitAI()
    end,
    function(ui, shot) shot("21_cloze_suggested") end,
    -- Writing one by hand with the Cloze button
    function(ui, shot) editor():editCard(nil) end,
    function(ui, shot)
        typeText("A single man of large fortune must be in want of a ")
        tapButton("Cloze")
        typeText("wife")
    end,
    function(ui, shot) shot("22_cloze_button_open") end,
    function(ui, shot)
        tapButton("Cloze")
        typeText(".")
    end,
    function(ui, shot) shot("23_cloze_button_closed") end,
    function(ui, shot) top().button_table:getButtonById("done").callback() end,
    function(ui, shot) shot("24_cloze_back") end,
    function(ui, shot) top().button_table:getButtonById("done").callback() end,
    function(ui, shot) shot("25_cloze_cards"); return 1 end,
    function(ui, shot)
        local e = editor()
        e.on_save(e, e.cards)
        return 2.5
    end,
    function(ui, shot)
        local f = io.open(os.getenv("PBANKI_DIR") .. "/inbox.jsonl")
        io.stderr:write("uxdriver inbox ", f and f:read("*a") or "none", "\n")
        if f then f:close() end
        closeTop()
    end,
}
