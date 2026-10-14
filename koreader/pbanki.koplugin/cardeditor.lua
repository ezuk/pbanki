--[[--
Full-screen editor for the cards made from one highlight.

Shows the highlight, then the cards (tap one to edit it, ✕ to remove it),
then Suggest / Clear / + Card and the Save row.

@module koplugin.pbanki.cardeditor
--]]--

local Blitbuffer = require("ffi/blitbuffer")
local Button = require("ui/widget/button")
local ButtonTable = require("ui/widget/buttontable")
local CenterContainer = require("ui/widget/container/centercontainer")
local Device = require("device")
local Font = require("ui/font")
local FrameContainer = require("ui/widget/container/framecontainer")
local Geom = require("ui/geometry")
local GestureRange = require("ui/gesturerange")
local HorizontalGroup = require("ui/widget/horizontalgroup")
local InputContainer = require("ui/widget/container/inputcontainer")
local LineWidget = require("ui/widget/linewidget")
local MultiInputDialog = require("ui/widget/multiinputdialog")
local ScrollTextWidget = require("ui/widget/scrolltextwidget")
local Size = require("ui/size")
local TextBoxWidget = require("ui/widget/textboxwidget")
local TitleBar = require("ui/widget/titlebar")
local UIManager = require("ui/uimanager")
local VerticalGroup = require("ui/widget/verticalgroup")
local VerticalSpan = require("ui/widget/verticalspan")
local util = require("util")
local _ = require("gettext")
local T = require("ffi/util").template
local Screen = Device.screen

-- One card: front and back, tap to edit
local CardRow = InputContainer:extend{
    card = nil,
    width = nil,
    on_edit = nil,
    on_remove = nil,
    show_parent = nil,
}

function CardRow:init()
    local remove = Button:new{
        text = "✕",
        bordersize = 0,
        width = Screen:scaleBySize(44),
        callback = self.on_remove,
        show_parent = self.show_parent,
    }
    local padding = Size.padding.default
    local text_width = self.width - remove:getSize().w - 2 * padding - 2 * Size.border.thin
    local texts = VerticalGroup:new{
        align = "left",
        TextBoxWidget:new{
            text = self.card.front,
            face = Font:getFace("cfont", 20),
            bold = true,
            width = text_width,
            height = Screen:scaleBySize(56),
            height_adjust = true,
            height_overflow_show_ellipsis = true,
        },
        TextBoxWidget:new{
            text = self.card.back ~= "" and self.card.back or _("(no answer)"),
            face = Font:getFace("cfont", 18),
            fgcolor = self.card.back ~= "" and Blitbuffer.COLOR_BLACK or Blitbuffer.COLOR_DARK_GRAY,
            width = text_width,
            height = Screen:scaleBySize(52),
            height_adjust = true,
            height_overflow_show_ellipsis = true,
        },
    }
    self[1] = FrameContainer:new{
        bordersize = Size.border.thin,
        radius = Size.radius.default,
        padding = padding,
        HorizontalGroup:new{
            align = "center",
            texts,
            remove,
        },
    }
    self.dimen = Geom:new{ x = 0, y = 0, w = self[1]:getSize().w, h = self[1]:getSize().h }
    self.ges_events.TapEdit = {
        GestureRange:new{ ges = "tap", range = function() return self.dimen end },
    }
end

function CardRow:onTapEdit()
    self.on_edit()
    return true
end

local CardEditor = InputContainer:extend{
    covers_fullscreen = true,
    -- {text, book, chapter, ...}
    highlight = nil,
    cards = nil,
    title = nil,
    subtitle = nil,
    max_cards = 4,
    -- Whether the Suggest button is offered
    can_suggest = false,
    on_suggest = nil,
    on_save = nil,
    -- Batch mode: skip this highlight
    on_skip = nil,
    on_close = nil,
}

function CardEditor:init()
    self.cards = self.cards or {}
    self.width = Screen:getWidth()
    self.height = Screen:getHeight()
    self.dimen = Geom:new{ x = 0, y = 0, w = self.width, h = self.height }
    if Device:hasKeys() then
        self.key_events.Close = { { Device.input.group.Back } }
    end
    self:build()
end

function CardEditor:build()
    local h_padding = Size.padding.large
    local inner_width = self.width - 2 * h_padding

    local title_bar = TitleBar:new{
        width = self.width,
        fullscreen = true,
        with_bottom_line = true,
        title = self.title,
        subtitle = self.subtitle,
        close_callback = function() self:onClose() end,
        show_parent = self,
    }

    local bottom = ButtonTable:new{
        width = inner_width,
        buttons = self:buttonRows(),
        zero_sep = true,
        show_parent = self,
    }

    -- The highlight at its natural height, scrollable past a fifth of the screen
    local quote_face = Font:getFace("cfont", 18)
    local max_quote_height = math.floor(self.height * 0.2)
    local quote = TextBoxWidget:new{
        text = self.highlight.text,
        face = quote_face,
        width = inner_width,
    }
    if quote:getSize().h > max_quote_height then
        quote:free()
        quote = ScrollTextWidget:new{
            text = self.highlight.text,
            face = quote_face,
            width = inner_width,
            height = max_quote_height,
            dialog = self,
        }
    end
    local quote_height = quote:getSize().h

    local rows = VerticalGroup:new{ align = "left" }
    if #self.cards == 0 then
        table.insert(rows, VerticalSpan:new{ width = Size.span.vertical_large * 2 })
        table.insert(rows, CenterContainer:new{
            dimen = Geom:new{ w = inner_width, h = Screen:scaleBySize(40) },
            TextBoxWidget:new{
                text = _("No cards yet. Tap + Card to write one."),
                face = Font:getFace("cfont", 18),
                width = inner_width,
                alignment = "center",
            },
        })
    end
    for i, card in ipairs(self.cards) do
        table.insert(rows, VerticalSpan:new{ width = Size.span.vertical_large })
        table.insert(rows, CardRow:new{
            card = card,
            width = inner_width,
            show_parent = self,
            on_edit = function() self:editCard(i) end,
            on_remove = function()
                table.remove(self.cards, i)
                self:refresh()
            end,
        })
    end

    local used = title_bar:getSize().h + quote_height + bottom:getSize().h
        + 2 * Size.span.vertical_large + Size.line.thick
    local filler = math.max(0, self.height - used - Size.span.vertical_large - rows:getSize().h)

    self[1] = FrameContainer:new{
        width = self.width,
        height = self.height,
        background = Blitbuffer.COLOR_WHITE,
        bordersize = 0,
        padding = 0,
        VerticalGroup:new{
            align = "center",
            title_bar,
            VerticalSpan:new{ width = Size.span.vertical_large },
            quote,
            VerticalSpan:new{ width = Size.span.vertical_large },
            LineWidget:new{
                dimen = Geom:new{ w = inner_width, h = Size.line.thick },
                background = Blitbuffer.COLOR_DARK_GRAY,
            },
            rows,
            VerticalSpan:new{ width = filler },
            bottom,
        },
    }
end

function CardEditor:buttonRows()
    local count = #self.cards
    local save_text = count > 1 and T(_("Save %1 cards"), count) or _("Save card")
    local tools = {
        {
            text = _("Suggest"),
            enabled = self.can_suggest,
            callback = function() self.on_suggest(self) end,
        },
        {
            text = _("Clear"),
            enabled = count > 0,
            callback = function()
                self.cards = {}
                self:refresh()
            end,
        },
        {
            text = _("+ Card"),
            enabled = count < self.max_cards,
            callback = function() self:editCard(nil) end,
        },
    }
    local actions
    if self.on_skip then
        actions = {
            {
                text = _("Skip"),
                callback = function() self.on_skip(self) end,
            },
            {
                text = count > 0 and save_text .. " ›" or _("Save"),
                enabled = count > 0,
                callback = function() self.on_save(self, self.cards) end,
            },
        }
    else
        actions = {
            {
                text = _("Cancel"),
                callback = function() self:onClose() end,
            },
            {
                text = count > 0 and save_text or _("Save"),
                enabled = count > 0,
                callback = function() self.on_save(self, self.cards) end,
            },
        }
    end
    return { tools, actions }
end

--- Replaces the cards shown, e.g. with AI suggestions.
function CardEditor:setCards(cards)
    self.cards = cards
    self:refresh()
end

--- Shows another highlight in the same editor (batch mode).
function CardEditor:setHighlight(highlight, subtitle, cards)
    self.highlight = highlight
    self.subtitle = subtitle
    self.cards = cards or {}
    self:refresh()
end

function CardEditor:refresh()
    self:free()
    self:build()
    UIManager:setDirty(self, "ui")
end

--- Edits card `index`, or adds a new card when `index` is nil.
function CardEditor:editCard(index)
    local card = index and self.cards[index] or { front = "", back = "" }
    local dialog
    dialog = MultiInputDialog:new{
        title = index and _("Edit card") or _("New card"),
        fields = {
            {
                description = _("Front"),
                text = card.front,
                hint = _("Question"),
                allow_newline = true,
            },
            {
                description = _("Back"),
                text = card.back,
                hint = _("Answer"),
                allow_newline = true,
            },
        },
        buttons = {
            {
                {
                    text = _("Cancel"),
                    id = "close",
                    callback = function() UIManager:close(dialog) end,
                },
                {
                    text = _("OK"),
                    is_enter_default = true,
                    callback = function()
                        local fields = dialog:getFields()
                        local front, back = util.trim(fields[1]), util.trim(fields[2])
                        UIManager:close(dialog)
                        if front == "" then return end
                        if index then
                            self.cards[index] = { front = front, back = back }
                        else
                            table.insert(self.cards, { front = front, back = back })
                        end
                        self:refresh()
                    end,
                },
            },
        },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function CardEditor:onClose()
    UIManager:close(self)
    if self.on_close then self.on_close() end
    return true
end

function CardEditor:onCloseWidget()
    UIManager:setDirty(nil, "ui")
end

return CardEditor
