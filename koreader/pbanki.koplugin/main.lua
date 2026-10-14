--[[--
Adds "Make Anki card" to the highlight menu.

Cards are appended as JSON lines to pbAnki's inbox, which pbAnki imports
into its "KOReader" deck.

@module koplugin.pbanki
--]]--

local InfoMessage = require("ui/widget/infomessage")
local MultiInputDialog = require("ui/widget/multiinputdialog")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local json = require("json")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local util = require("util")
local _ = require("gettext")

local PBANKI_DIR = "/mnt/ext1/applications/pbanki"
local INBOX_FILE = PBANKI_DIR .. "/inbox.jsonl"

local PbAnki = WidgetContainer:extend{
    name = "pbanki",
    is_doc_only = true,
}

function PbAnki:init()
    if self.ui.highlight then
        self.ui.highlight:addToHighlightDialog("12_make_anki_card", function(highlight)
            return {
                text = _("Make Anki card"),
                callback = function()
                    self:makeCard(highlight)
                end,
            }
        end)
    end
end

--- Book, author, chapter, page and position of the current selection.
function PbAnki:getSource(highlight)
    local props = self.ui.doc_props or {}
    local pos0 = highlight.selected_text.pos0
    local page
    if self.ui.rolling then
        page = self.ui.document:getPageFromXPointer(pos0)
    else
        page = pos0.page
    end

    local chapter
    if self.ui.toc then
        chapter = self.ui.toc:getTocTitleByPage(self.ui.rolling and pos0 or page)
    end

    return {
        book = props.display_title,
        author = props.authors and props.authors:gsub("\n", ", ") or nil,
        chapter = chapter ~= "" and chapter or nil,
        page = page,
        file = self.ui.document.file,
        -- An xpointer in reflowable books, the page number in fixed-layout ones
        position = self.ui.rolling and pos0 or tostring(page),
    }
end

function PbAnki:makeCard(highlight)
    local text = util.cleanupSelectedText(highlight.selected_text.text)
    local source = self:getSource(highlight)
    highlight:onClose()

    local dialog
    dialog = MultiInputDialog:new{
        title = _("New Anki card"),
        fields = {
            {
                description = _("Front"),
                text = text,
                allow_newline = true,
            },
            {
                description = _("Back"),
                hint = _("Answer"),
                allow_newline = true,
            },
        },
        buttons = {
            {
                {
                    text = _("Cancel"),
                    id = "close",
                    callback = function()
                        UIManager:close(dialog)
                    end,
                },
                {
                    text = _("Save"),
                    is_enter_default = true,
                    callback = function()
                        local fields = dialog:getFields()
                        if util.trim(fields[1]) == "" then
                            UIManager:show(InfoMessage:new{ text = _("The front can't be empty.") })
                            return
                        end
                        local ok, err = self:saveCard{
                            front = fields[1],
                            back = fields[2],
                            quote = text,
                            book = source.book,
                            author = source.author,
                            chapter = source.chapter,
                            page = source.page,
                            file = source.file,
                            position = source.position,
                        }
                        UIManager:close(dialog)
                        if ok then
                            UIManager:show(InfoMessage:new{ text = _("Card saved for pbAnki."), timeout = 2 })
                        else
                            UIManager:show(InfoMessage:new{ text = _("Could not save card: ") .. tostring(err) })
                        end
                    end,
                },
            },
        },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function PbAnki:saveCard(card)
    if lfs.attributes(PBANKI_DIR, "mode") ~= "directory" then
        return false, _("pbAnki is not installed.")
    end
    local encoded_ok, line = pcall(json.encode, card)
    if not encoded_ok then
        return false, line
    end
    local file, err = io.open(INBOX_FILE, "a")
    if not file then
        logger.warn("pbanki: cannot open inbox", err)
        return false, err
    end
    file:write(line, "\n")
    file:close()
    return true
end

return PbAnki
