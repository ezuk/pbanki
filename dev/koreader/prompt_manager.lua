-- AI prompt folders: Standard written by the plugin, a reader's folder with
-- one file, picked for this book.
local UIManager = require("ui/uimanager")
local lfs = require("libs/libkoreader-lfs")

local DIR = os.getenv("PBANKI_DIR") .. "/prompts"

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
local function system(ui, request)
    local p = ui.pbanki
    return p:messagesFor({ text = "x" }, p:fullRequest(request))[1].content:sub(1, 50):gsub("\n", " ")
end

return {
    function(ui)
        local names = {}
        for name in lfs.dir(DIR .. "/Standard") do
            if name:sub(1, 1) ~= "." then table.insert(names, name) end
        end
        table.sort(names)
        io.stderr:write("uxdriver standard=", table.concat(names, ","),
            " readme=", tostring(lfs.attributes(DIR .. "/README.md", "mode")), "\n")
        lfs.mkdir(DIR .. "/Concise")
        local f = io.open(DIR .. "/Concise/definitions.md", "w")
        f:write("# Short definitions\nDefine the term in under 10 words.\n- Use plain words.\n")
        f:close()
    end,
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
    function(ui) open(ui, "AI prompt") end,
    function(ui, shot) shot("34_prompts") end,
    function(ui) open(ui, "This book uses") end,
    function(ui, shot) shot("35_book_choices") end,
    function(ui) open(ui, "Concise") end,
    function(ui, shot) shot("36_book_concise") end,
    function(ui) menu(ui):backToUpperMenu() end,
    function(ui, shot)
        shot("37_prompts_after")
        io.stderr:write("uxdriver active=", ui.pbanki:activePromptName(),
            " | definition=", system(ui, { definition = true }),
            " | questions=", system(ui, { count = 1 }), "\n")
    end,
    function(ui) open(ui, "About prompt folders") end,
    function(ui, shot) shot("38_about_folders") end,
    function(ui)
        UIManager:close(top())
        ui.menu:onCloseReaderMenu()
        ui.texteditor:checkEditFile(DIR .. "/Standard/cloze.md")
    end,
    function(ui, shot) shot("39_text_editor") end,
}
