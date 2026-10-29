-- Highlight button on both card sides: the highlight over the editor, then back to the text and keyboard.
-- answers them before saving.
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
        if widget.text and widget.text:find(text, 1, true) and widget.callback then
            found = widget
            return
        end
        for _idx, child in ipairs(widget) do search(child) end
        if widget.button_table then search(widget.button_table) end
        if widget.buttons_layout then search(widget.buttons_layout) end
        if widget.layout then for _r, row in ipairs(widget.layout) do search(row) end end
    end
    search(dialog())
    io.stderr:write("uxdriver tap ", text, " ", tostring(found ~= nil), "\n")
    found.callback()
end
local function waitAI() return tonumber(os.getenv("UX_AI_WAIT")) or 20 end
local function typeText(text) dialog()._input_widget:addChars(text) end
local function done() dialog().button_table:getButtonById("done").callback() end
local function viewer()
    for i = #UIManager._window_stack, 1, -1 do
        local w = UIManager._window_stack[i].widget
        if w.scroll_widget then return w end
    end
end
local function report(label)
    local input = dialog()._input_widget
    io.stderr:write("uxdriver ", label, " text=[", input and input:getText() or "none",
        "] keyboard=", tostring(input and input:isKeyboardVisible()), "\n")
end

return {
    function(ui)
        -- The emulator has a real keyboard; show KOReader's on-screen one as on the device
        G_reader_settings:saveSetting("virtual_keyboard_enabled", true)
        ui:handleEvent(Event:new("GotoPage", 35))
    end,
    function(ui, shot)
        -- Most of the page, so the highlight is long
        select(ui, 30, 268, 720, 960)
        ui.pbanki:createFromSelection(ui.highlight, nil)
    end,
    function(ui, shot)
        tapButton("Write my own")
        -- Longer than one screen, to show scrolling
        local h = editor().highlight
        h.text = h.text .. "\n" .. h.text
    end,
    function(ui, shot) typeText("What does a rich single man supposedly want?") end,
    function(ui, shot) shot("49_front_highlight_button"); report("front before") end,
    function(ui, shot) tapButton("Highlight") end,
    function(ui, shot) shot("50_highlight_shown") end,
    function(ui, shot) viewer().scroll_widget:scrollDown() end,
    function(ui, shot) shot("51_highlight_scrolled") end,
    function(ui, shot) tapButton("Close") end,
    function(ui, shot) shot("52_front_after"); report("front after") end,
    function(ui, shot) done() end,
    function(ui, shot) typeText("A wi") end,
    function(ui, shot) shot("53_back_highlight_button"); report("back before") end,
    function(ui, shot) tapButton("Highlight") end,
    function(ui, shot) tapButton("Close") end,
    function(ui, shot) typeText("fe.") end,
    function(ui, shot) shot("54_back_after"); report("back after") end,
}
