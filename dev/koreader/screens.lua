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
local function typeAndNext(text)
    local dialog = top()
    dialog:setInputText(text)
    dialog.button_table:getButtonById("done").callback()
end
local function waitAI() return tonumber(os.getenv("UX_AI_WAIT")) or 20 end

return {
    function(ui) ui:handleEvent(Event:new("GotoPage", 35)) end,
    -- Mode 1: highlight menu
    function(ui, shot)
        select(ui, 30, 268, 140, 355)
        ui.highlight:onShowHighlightMenu()
    end,
    function(ui, shot) shot("01_highlight_menu") end,
    function(ui, shot)
        closeTop()
        ui.pbanki:createFromSelection(ui.highlight, nil)
    end,
    function(ui, shot) shot("02_card_count") end,
    function(ui, shot)
        local spin = top()
        spin.value = 2
        UIManager:close(spin)
        spin.callback(spin)
        return 1.5
    end,
    function(ui, shot) shot("03_suggesting"); return waitAI() end,
    function(ui, shot) shot("04_suggested") end,
    function(ui, shot) editor():editCard(1) end,
    function(ui, shot) shot("05_edit_card") end,
    function(ui, shot) closeTop() end,
    function(ui, shot) local e = editor(); e.on_save(e, e.cards) end,
    function(ui, shot) shot("06_saved"); return 2.5 end,
    function(ui, shot) shot("07_page_highlighted") end,
    -- Plain highlights made while reading
    function(ui, shot)
        select(ui, 60, 674, 505, 674); ui.highlight:saveHighlight(true); ui.highlight:clear()
        select(ui, 60, 717, 540, 755); ui.highlight:saveHighlight(true); ui.highlight:clear()
        ui:handleEvent(Event:new("GotoPage", 36))
    end,
    function(ui, shot)
        select(ui, 40, 150, 700, 260); ui.highlight:saveHighlight(true); ui.highlight:clear()
    end,
    function(ui, shot) shot("08_more_highlights") end,
    -- Menu
    function(ui, shot)
        ui.menu:onShowMenu()
        local tm = ui.menu.menu_container[1]
        io.stderr:write("uxdriver menu ", tostring(tm and tm.tab_item_table and #tm.tab_item_table), "\n")
        for i, tab in ipairs(tm.tab_item_table) do
            for _, item in ipairs(tab) do
                if item.text == "pbAnki flashcards" then
                    tm:switchMenuTab(i)
                    tm:onMenuSelect(item)
                end
            end
        end
    end,
    function(ui, shot) shot("09_menu") end,
    function(ui, shot) ui.menu:onCloseReaderMenu() end,
    -- Mode 2: batch
    function(ui, shot) ui.pbanki:onPbAnkiReviewHighlights(); return waitAI() end,
    function(ui, shot) shot("10_batch_first") end,
    function(ui, shot) local e = editor(); e.on_skip(e); return waitAI() end,
    function(ui, shot) shot("11_batch_second") end,
    function(ui, shot) local e = editor(); e.cards = {}; e:refresh() end,
    function(ui, shot) shot("12_cleared") end,
    function(ui, shot) local e = editor(); e:editCard(nil) end,
    function(ui, shot) closeTop(); local e = editor(); e:setCards({ { front = "What did Mrs. Long tell Mrs. Bennet?", back = "Netherfield Park is let" } }) end,
    function(ui, shot) local e = editor(); e.on_save(e, e.cards); return waitAI() end,
    function(ui, shot) shot("13_batch_third") end,
    function(ui, shot) local e = editor(); e.on_save(e, e.cards) end,
    function(ui, shot) shot("14_batch_done"); return 3.5 end,
    function(ui, shot) ui.pbanki:onPbAnkiReviewHighlights() end,
    function(ui, shot) shot("15_batch_again"); return 3.5 end,
    -- AI off
    function(ui, shot)
        ui.pbanki.settings.use_ai = false
        ui:handleEvent(Event:new("GotoPage", 37))
    end,
    function(ui, shot)
        select(ui, 40, 150, 700, 200)
        ui.pbanki:createFromSelection(ui.highlight, nil)
    end,
    function(ui, shot) shot("16_ai_off") end,
    function(ui, shot) typeAndNext("Who is the speaker addressing?") end,
    function(ui, shot) shot("17_ai_off_back") end,
    function(ui, shot) typeAndNext("Mrs. Bennet") end,
    function(ui, shot) shot("18_ai_off_saved"); return 2.5 end,
}
