-- Makes real highlights across chapters of the real book, for the desktop app's test fixture.
-- Run through dev/koreader/check_desktop_saves.sh --make-fixture.
local Event = require("ui/event")
local UIManager = require("ui/uimanager")

local function log(...) io.stderr:write("uxdriver ", table.concat({ ... }, " "), "\n") end
local function select(ui, x0, y0, x1, y1)
    local sel = ui.document:getTextFromPositions({ x = x0, y = y0 }, { x = x1, y = y1 })
    ui.highlight.selected_text = sel
    ui.highlight.hold_pos = { x = x0, y = y0, page = ui.document:getCurrentPage() }
    return sel
end
-- A highlight as the Highlight button makes it
local function highlight(ui, x0, y0, x1, y1)
    select(ui, x0, y0, x1, y1)
    ui.highlight:saveHighlight(true)
    ui.highlight:clear()
end
local function goTo(page)
    return function(ui) ui:handleEvent(Event:new("GotoPage", page)) end
end
-- The topmost window that is not the on-screen keyboard
local function dialog()
    for i = #UIManager._window_stack, 1, -1 do
        local w = UIManager._window_stack[i].widget
        if not w.inputbox then return w end
    end
end
local function tapButton(text)
    local found
    local function search(widget)
        if type(widget) ~= "table" or found then return end
        if widget.text == text and widget.callback then
            found = widget
            return
        end
        for _idx, child in ipairs(widget) do search(child) end
        if widget.button_table then search(widget.button_table) end
        if widget.layout then for _r, row in ipairs(widget.layout) do search(row) end end
    end
    search(dialog())
    found.callback()
end

return {
    -- Chapter I: the drop-cap opening, and a repeated name of two words
    goTo(35),
    function(ui) highlight(ui, 30, 244, 730, 336) end,
    function(ui) highlight(ui, 30, 796, 150, 796) end,
    -- Chapter I: with a note, through the Add note dialog
    goTo(36),
    function(ui)
        select(ui, 30, 612, 730, 704)
        ui.highlight:addNote()
    end,
    function(ui) dialog():setInputText("Money first, then the man.") end,
    function(ui) tapButton("Save") end,
    -- Chapter II: two paragraphs, then a short one
    goTo(41),
    function(ui) highlight(ui, 30, 106, 730, 290) end,
    function(ui) highlight(ui, 30, 520, 730, 520) end,
    -- Chapter IV
    goTo(55),
    function(ui) highlight(ui, 30, 704, 730, 796) end,
    -- Chapter VII
    goTo(78),
    function(ui) highlight(ui, 30, 934, 730, 980) end,
    -- Chapter XI: a page bookmark, which is not a highlight
    goTo(121),
    function(ui) ui:handleEvent(Event:new("ToggleBookmark")) end,
    -- Stop reading further on, then save as closing the book does
    goTo(150),
    function(ui) ui:saveSettings() end,
    function(ui)
        for _idx, a in ipairs(ui.annotation.annotations) do
            log("annotation", tostring(a.datetime), "drawer=" .. tostring(a.drawer), "note=" .. tostring(a.note),
                "chapter=" .. tostring(a.chapter), "page=" .. tostring(a.pageno), "pos0=" .. tostring(a.pos0))
            log("  text=" .. tostring(a.text))
        end
    end,
}
