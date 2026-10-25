-- AI prompts: duplicate Standard, edit the copy, and pick it for this book.
local UIManager = require("ui/uimanager")

local function top()
    local stack = UIManager._window_stack
    return stack[#stack].widget
end
local function menu(ui) return ui.menu.menu_container[1] end
local function label(item)
    return item.text or (item.text_func and item.text_func()) or ""
end
local function open(ui, prefix)
    local tm = menu(ui)
    for _, item in ipairs(tm.item_table) do
        if label(item):sub(1, #prefix) == prefix then
            tm:onMenuSelect(item)
            return
        end
    end
    error("no menu item " .. prefix)
end
local function log(ui)
    local p = ui.pbanki
    local system = p:messagesFor({ text = "x" }, p:fullRequest{ count = 1 })[1].content
    io.stderr:write("uxdriver active=", p:activePrompt().name, " system=", system:sub(1, 60), "\n")
end

return {
    function(ui)
        ui.menu:onShowMenu()
        local tm = menu(ui)
        for i, tab in ipairs(tm.tab_item_table) do
            for _, item in ipairs(tab) do
                if item.text == "pbAnki flashcards" then
                    tm:switchMenuTab(i)
                    tm:onMenuSelect(item)
                end
            end
        end
    end,
    function(ui, shot) shot("34_menu_prompt") end,
    function(ui) open(ui, "AI prompt") end,
    function(ui, shot) shot("35_prompts") end,
    function(ui) open(ui, "Standard") end,
    function(ui, shot) shot("36_standard") end,
    function(ui) open(ui, "Question cards") end,
    function(ui, shot) shot("37_standard_view") end,
    function(ui) UIManager:close(top()) end,
    function(ui) open(ui, "Duplicate") end,
    function(ui, shot) shot("38_duplicate_name") end,
    function(ui)
        local dialog = top()
        dialog:setInputText("Concise")
        dialog.button_table:getButtonById("done").callback()
    end,
    function(ui, shot) shot("39_prompts_with_copy") end,
    function(ui) open(ui, "Concise") end,
    function(ui) open(ui, "Question cards") end,
    function(ui, shot) shot("40_edit_prompt") end,
    function(ui)
        local dialog = top()
        dialog:setInputText("You write one short Anki question per idea a reader highlighted.\n\n"
            .. "- Front: under 8 words.\n- Back: 1 to 3 words.")
        -- As typing would
        dialog._buttons_edit_callback(true)
        dialog.button_table:getButtonById("save").callback()
    end,
    function(ui, shot) shot("41_edit_saved") end,
    function(ui) top().button_table:getButtonById("close").callback() end,
    function(ui, shot) shot("42_concise_edited") end,
    function(ui) menu(ui):backToUpperMenu() end,
    function(ui) open(ui, "This book uses") end,
    function(ui) open(ui, "Concise") end,
    function(ui, shot) shot("43_book_choice") end,
    function(ui) menu(ui):backToUpperMenu() end,
    function(ui, shot) shot("44_prompts_after"); log(ui) end,
    function(ui)
        local saved = G_reader_settings:readSetting("pbanki").prompts
        io.stderr:write("uxdriver saved=", #saved, " name=", saved[1].name, " questions=",
            tostring(saved[1].questions):sub(1, 30), " cloze=", tostring(saved[1].cloze), "\n")
        ui.menu:onCloseReaderMenu()
    end,
}
