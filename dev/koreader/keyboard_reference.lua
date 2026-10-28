-- Captures KOReader's on-screen keyboard as a reference for pbAnki's own.
-- Usage: dev/koreader/render.sh keyboard_reference.lua
local InputDialog = require("ui/widget/inputdialog")
local UIManager = require("ui/uimanager")

local dialog
local function keyboard() return dialog._input_widget.keyboard end
local function key(label)
    for _, row in ipairs(keyboard().layout) do
        for _, k in ipairs(row) do
            if k.label == label then return k end
        end
    end
end

return {
    function(ui)
        -- The desktop emulator has a real keyboard, so KOReader hides its own
        G_reader_settings:makeTrue("virtual_keyboard_enabled")
        dialog = InputDialog:new{
            title = "Front",
            input = "What is the capital of France? It sits on the Seine and has been the capital since the tenth century.",
            allow_newline = true,
            text_height = 300,
            buttons = { { { text = "Cancel" }, { text = "Save" } } },
        }
        UIManager:show(dialog)
        dialog:onShowKeyboard()
    end,
    function(ui, shot) shot("kb_01_lower") end,
    function(ui) keyboard():setLayer("Shift") end,
    function(ui, shot) shot("kb_02_shift") end,
    function(ui) keyboard():setLayer("Shift"); keyboard():setLayer("Sym") end,
    function(ui, shot) shot("kb_03_sym") end,
    function(ui) keyboard():setLayer("Shift") end,
    function(ui, shot) shot("kb_04_sym_shift") end,
    function(ui) keyboard():setLayer("Shift"); keyboard():setLayer("Sym") end,
    function(ui) key("e"):onHoldSelect() end,
    function(ui, shot) shot("kb_05_hold_e") end,
}
