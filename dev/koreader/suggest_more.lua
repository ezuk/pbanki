-- Suggest more adds cards to the list, which pages when it gets long.
local Event = require("ui/event")
local UIManager = require("ui/uimanager")

local function top()
    local stack = UIManager._window_stack
    return stack[#stack].widget
end
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
local function pick(count)
    local spin = top()
    spin.value = count
    UIManager:close(spin)
    spin.callback(spin)
end
local function cards(tag)
    local e = editor()
    local fronts = {}
    for i, c in ipairs(e.cards) do table.insert(fronts, i .. ". " .. c.front) end
    io.stderr:write("uxdriver ", tag, ": cards=", #e.cards, " page=", e.page, "/", tostring(e.page_count),
        " | ", table.concat(fronts, " | "), "\n")
end
local function waitAI() return tonumber(os.getenv("UX_AI_WAIT")) or 25 end

return {
    function(ui) ui:handleEvent(Event:new("GotoPage", 35)) end,
    function(ui)
        select(ui, 30, 268, 140, 355)
        ui.pbanki:createFromSelection(ui.highlight, nil)
    end,
    function(ui, shot)
        local spin = top()
        io.stderr:write("uxdriver spin max=", tostring(spin.value_max), "\n")
        UIManager:close(spin)
        ui.pbanki:askCardCount(10, function() end)
    end,
    function(ui, shot) shot("more_01_count_10"); UIManager:close(top()) end,
    function(ui)
        select(ui, 30, 268, 140, 355)
        ui.pbanki:createFromSelection(ui.highlight, nil)
    end,
    function(ui) pick(3); return waitAI() end,
    function(ui, shot) cards("first"); shot("more_02_first_three") end,
    function(ui) local e = editor(); e.on_suggest(e) end,
    function(ui, shot) shot("more_03_ask_more") end,
    function(ui) pick(5); return waitAI() end,
    function(ui, shot) cards("after more"); shot("more_04_added") end,
    function(ui) editor():goToPage(2) end,
    function(ui, shot) cards("page 2"); shot("more_05_page_two") end,
}
