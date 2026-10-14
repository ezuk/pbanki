-- Batch mode suggests cards for the next highlight in the background.
local Event = require("ui/event")
local UIManager = require("ui/uimanager")

local function editor()
    for i = #UIManager._window_stack, 1, -1 do
        local w = UIManager._window_stack[i].widget
        if w.setCards then return w end
    end
end
local function highlight(ui, x0, y0, x1, y1)
    ui.highlight.selected_text = ui.document:getTextFromPositions({ x = x0, y = y0 }, { x = x1, y = y1 })
    ui.highlight:saveHighlight(true)
    ui.highlight:clear()
end
local function cards(tag)
    local e = editor()
    local fronts = {}
    for _, c in ipairs(e.cards) do table.insert(fronts, c.front) end
    io.stderr:write("uxdriver ", tag, ": ", e.subtitle, " waiting=", tostring(e.waiting),
        " cards=", #e.cards, " | ", table.concat(fronts, " | "), "\n")
end

return {
    function(ui) ui:handleEvent(Event:new("GotoPage", 35)) end,
    function(ui)
        highlight(ui, 30, 268, 140, 355)
        highlight(ui, 60, 674, 505, 674)
        highlight(ui, 60, 717, 540, 755)
        ui:handleEvent(Event:new("GotoPage", 36))
    end,
    function(ui) highlight(ui, 40, 150, 700, 260) end,
    function(ui) ui.pbanki:onPbAnkiReviewHighlights(); return 30 end,
    function(ui, shot) cards("first"); shot("prefetch_1") end,
    -- The second highlight was fetched while the first was on screen
    function(ui) local e = editor(); e.on_skip(e); return 0.3 end,
    function(ui, shot) cards("second, right away"); shot("prefetch_2") end,
    -- Skip on twice at once, before the fourth is ready: the editor waits for it
    function(ui) local e = editor(); e.on_skip(e); e.on_skip(e); return 0.1 end,
    function(ui, shot) cards("fourth, right away"); shot("prefetch_4_waiting"); return 25 end,
    function(ui, shot) cards("fourth, later"); shot("prefetch_4") end,
}
