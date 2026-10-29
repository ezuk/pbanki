--[[--
Full-screen editor for the cards made from one highlight.

Shows the highlight, then the cards (tap one to edit it, ✕ to remove it),
in pages when they do not fit, then Suggest / Clear / + Card and the Save row.

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
local InputDialog = require("ui/widget/inputdialog")
local LineWidget = require("ui/widget/linewidget")
local ScrollTextWidget = require("ui/widget/scrolltextwidget")
local Size = require("ui/size")
local TextBoxWidget = require("ui/widget/textboxwidget")
local TextViewer = require("ui/widget/textviewer")
local TextWidget = require("ui/widget/textwidget")
local TitleBar = require("ui/widget/titlebar")
local UIManager = require("ui/uimanager")
local VerticalGroup = require("ui/widget/verticalgroup")
local VerticalSpan = require("ui/widget/verticalspan")
local cardgen = require("cardgen")
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
    local cloze = cardgen.isCloze(self.card.front)
    local back = self.card.back
    if back == "" then back = cloze and _("(cloze)") or _("(no answer)") end
    local texts = VerticalGroup:new{
        align = "left",
        TextBoxWidget:new{
            text = cloze and cardgen.showCloze(self.card.front) or self.card.front,
            face = Font:getFace("cfont", 20),
            bold = true,
            width = text_width,
            height = Screen:scaleBySize(56),
            height_adjust = true,
            height_overflow_show_ellipsis = true,
        },
        TextBoxWidget:new{
            text = back,
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
    -- AI suggestions the reader removed, as the AI wrote them
    rejected = nil,
    title = nil,
    subtitle = nil,
    -- Page of cards shown
    page = 1,
    -- Card to bring into view on the next build
    focus_card = nil,
    -- Whether the Suggest button is offered
    can_suggest = false,
    -- How many cards to ask the AI for, or nil to skip the AI
    request = nil,
    -- Suggestions are on their way
    waiting = false,
    -- The reader's instructions for the next suggestions, or nil
    guidance = nil,
    on_suggest = nil,
    on_save = nil,
    -- Batch mode: skip this highlight
    on_skip = nil,
    on_close = nil,
}

function CardEditor:init()
    self.cards = self.cards or {}
    self.rejected = self.rejected or {}
    self.width = Screen:getWidth()
    self.height = Screen:getHeight()
    self.dimen = Geom:new{ x = 0, y = 0, w = self.width, h = self.height }
    if Device:hasKeys() then
        self.key_events.Close = { { Device.input.group.Back } }
    end
    self.ges_events.Swipe = {
        GestureRange:new{ ges = "swipe", range = function() return self.dimen end },
    }
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

    local used = title_bar:getSize().h + quote_height + bottom:getSize().h
        + 2 * Size.span.vertical_large + Size.line.thick
    local room = self.height - used - Size.span.vertical_large

    local rows = VerticalGroup:new{ align = "left" }
    if #self.cards == 0 then
        table.insert(rows, VerticalSpan:new{ width = Size.span.vertical_large * 2 })
        table.insert(rows, CenterContainer:new{
            dimen = Geom:new{ w = inner_width, h = Screen:scaleBySize(40) },
            TextBoxWidget:new{
                text = self.waiting and _("Suggesting cards…")
                    or _("No cards yet. Tap + Card to write one."),
                face = Font:getFace("cfont", 18),
                width = inner_width,
                alignment = "center",
            },
        })
    end
    local card_rows = {}
    for i, card in ipairs(self.cards) do
        card_rows[i] = CardRow:new{
            card = card,
            width = inner_width,
            show_parent = self,
            on_edit = function() self:editCard(i) end,
            on_remove = function()
                self:reject(table.remove(self.cards, i))
                self:refresh()
            end,
        }
    end
    local pager_height = Screen:scaleBySize(48)
    local pages = self:paginate(card_rows, room, pager_height)
    self.page_count = #pages
    if self.focus_card then
        for p, page in ipairs(pages) do
            if self.focus_card >= page.first and self.focus_card <= page.last then self.page = p end
        end
        self.focus_card = nil
    end
    self.page = math.max(1, math.min(self.page, #pages))
    local shown = pages[self.page]
    for i, row in ipairs(card_rows) do
        if shown and i >= shown.first and i <= shown.last then
            table.insert(rows, VerticalSpan:new{ width = Size.span.vertical_large })
            table.insert(rows, row)
        else
            row:free()
        end
    end

    local pager = VerticalSpan:new{ width = 0 }
    if #pages > 1 then
        pager = self:pager(inner_width, pager_height, shown)
    end
    local filler = math.max(0, room - rows:getSize().h - pager:getSize().h)

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
            pager,
            bottom,
        },
    }
end

--- Splits card rows into pages that fit in `room`, leaving `pager_height`
-- for the page buttons when there is more than one page.
-- @return list of {first, last}, empty without cards
function CardEditor:paginate(card_rows, room, pager_height)
    local heights, total = {}, 0
    for i, row in ipairs(card_rows) do
        heights[i] = Size.span.vertical_large + row:getSize().h
        total = total + heights[i]
    end
    if #card_rows == 0 then return {} end
    if total <= room then return { { first = 1, last = #card_rows } } end
    room = room - pager_height
    local pages, page, used = {}, nil, 0
    for i, h in ipairs(heights) do
        if not page or used + h > room then
            page = { first = i, last = i }
            table.insert(pages, page)
            used = h
        else
            page.last = i
            used = used + h
        end
    end
    return pages
end

--- ‹ Cards 1–4 of 9 › for page `shown`.
function CardEditor:pager(width, height, shown)
    local button_width = Screen:scaleBySize(80)
    local function arrow(text, page, enabled)
        return Button:new{
            text = text,
            enabled = enabled,
            bordersize = 0,
            width = button_width,
            callback = function() self:goToPage(page) end,
            show_parent = self,
        }
    end
    return CenterContainer:new{
        dimen = Geom:new{ w = width, h = height },
        HorizontalGroup:new{
            align = "center",
            arrow("‹", self.page - 1, self.page > 1),
            CenterContainer:new{
                dimen = Geom:new{ w = width - 2 * button_width, h = height },
                TextWidget:new{
                    text = T(_("Cards %1–%2 of %3"), shown.first, shown.last, #self.cards),
                    face = Font:getFace("cfont", 18),
                },
            },
            arrow("›", self.page + 1, self.page < self.page_count),
        },
    }
end

function CardEditor:goToPage(page)
    if page < 1 or page > (self.page_count or 1) or page == self.page then return end
    self.page = page
    self:refresh()
end

function CardEditor:onSwipe(_arg, ges)
    if ges.direction == "west" then
        self:goToPage(self.page + 1)
        return true
    elseif ges.direction == "east" then
        self:goToPage(self.page - 1)
        return true
    end
end

local function saveText(count)
    return count > 1 and T(_("Save %1 cards"), count) or _("Save card")
end

function CardEditor:buttonRows()
    local count = #self.cards
    local save_text = saveText(count)
    local tools = {
        {
            text = count > 0 and _("Suggest more") or _("Suggest"),
            enabled = self.can_suggest,
            callback = function() self.on_suggest(self) end,
        },
        {
            text = _("Clear"),
            enabled = count > 0,
            callback = function()
                for _idx, card in ipairs(self.cards) do self:reject(card) end
                self.cards = {}
                self:refresh()
            end,
        },
        {
            text = _("+ Card"),
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
                callback = function() self:save() end,
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
                callback = function() self:save() end,
            },
        }
    end
    return { tools, actions }
end

--- Index of the first question card without an answer, or nil.
function CardEditor:firstUnanswered(skip)
    for i, card in ipairs(self.cards) do
        if i ~= skip and card.back == "" and not cardgen.isCloze(card.front) then return i end
    end
end

--- Saves the cards, after asking for the answer of each question that has none.
function CardEditor:save()
    local index = self:firstUnanswered()
    if index then
        self.focus_card = index
        self:refresh()
        self:editCard(index, true)
        return
    end
    self.on_save(self, self.cards)
end

--- Replaces the cards shown.
function CardEditor:setCards(cards)
    self.cards = cards
    self.page = 1
    self.waiting = false
    self:refresh()
end

--- Adds AI suggestions after the cards shown, and shows the first.
function CardEditor:addCards(cards)
    if #cards > 0 then self.focus_card = #self.cards + 1 end
    for _idx, card in ipairs(cards) do
        card.suggested = { front = card.front, back = card.back }
        table.insert(self.cards, card)
    end
    self.waiting = false
    self:refresh()
end

--- Shows that suggestions are on their way while there are no cards yet.
function CardEditor:setWaiting(waiting)
    self.waiting = waiting
    self:refresh()
end

--- Shows another highlight in the same editor (batch mode).
function CardEditor:setHighlight(highlight, subtitle, cards)
    self.highlight = highlight
    self.subtitle = subtitle
    self.cards = cards or {}
    self.rejected = {}
    self.guidance = nil
    self.page = 1
    self.waiting = false
    self:refresh()
end

--- Remembers a removed card for the next suggestions when the AI wrote it.
function CardEditor:reject(card)
    if card and card.suggested then table.insert(self.rejected, card.suggested) end
end

function CardEditor:refresh()
    self:free()
    self:build()
    UIManager:setDirty(self, "ui")
end

--- Edits card `index`, or adds a new card when `index` is nil, front then
-- back on two full screens. A question without an answer opens on the back.
-- With `save`, finishing saves the cards right away.
function CardEditor:editCard(index, save)
    local card = index and self.cards[index] or { front = "", back = "" }
    local front, back = card.front, card.back
    local function done()
        if index then
            self.cards[index] = { front = front, back = back, suggested = card.suggested }
        else
            table.insert(self.cards, { front = front, back = back })
            self.focus_card = #self.cards
        end
        self:refresh()
        if save then self:save() end
    end
    local askFront, askBack
    askFront = function()
        self:sideDialog{
            title = _("Card front"),
            input = front,
            hint = _("Question, or a sentence with a cloze"),
            cloze_button = true,
            left = { text = _("Cancel"), id = "close" },
            right = { text = _("Next") .. " ›" },
            on_left = function() end,
            on_right = function(text)
                front = text
                if front == "" then return false end
                askBack()
            end,
        }
    end
    askBack = function()
        self:sideDialog{
            title = _("Card back"),
            description = cardgen.isCloze(front) and cardgen.showCloze(front) or front,
            input = back,
            hint = cardgen.isCloze(front) and _("Extra shown with the answer (optional)") or _("Answer"),
            left = { text = "‹ " .. _("Front") },
            right = { text = not save and _("Done")
                or (self:firstUnanswered(index) and _("Next") .. " ›")
                or saveText(#self.cards + (index and 0 or 1)) },
            on_left = function(text)
                back = text
                askFront()
            end,
            on_right = function(text)
                back = text
                done()
            end,
        }
    end
    if card.back == "" and card.front ~= "" and not cardgen.isCloze(card.front) then
        askBack()
    else
        askFront()
    end
end

--- Shows one side of a card in a full-screen input with large text.
-- `on_right` may return false to keep the dialog open.
function CardEditor:sideDialog(opts)
    local dialog
    local function finish(handler)
        local text = util.trim(dialog:getInputText())
        if handler(text) == false then return end
        UIManager:close(dialog)
    end
    local row = {
        {
            text = opts.left.text,
            id = opts.left.id,
            callback = function() finish(opts.on_left) end,
        },
    }
    if opts.cloze_button then
        -- Opens a cloze deletion at the cursor, or closes the open one
        table.insert(row, {
            text = "{{…}} " .. _("Cloze"),
            callback = function()
                local input = dialog._input_widget
                local before = table.concat(input.charlist, "", 1, input.charpos - 1)
                input:addChars(cardgen.clozeMark(before, input:getText()))
            end,
        })
    end
    table.insert(row, {
        text = _("Highlight"),
        callback = function() self:showHighlight(dialog) end,
    })
    table.insert(row, {
        text = opts.right.text,
        id = "done",
        is_enter_default = true,
        callback = function() finish(opts.on_right) end,
    })
    dialog = InputDialog:new{
        title = opts.title,
        description = opts.description,
        input = opts.input,
        input_hint = opts.hint,
        input_face = Font:getFace("cfont", 28),
        fullscreen = true,
        condensed = true,
        allow_newline = true,
        buttons = { row },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
    return dialog
end

--- Shows the highlight full screen over `dialog`, scrollable when long.
-- The keyboard is hidden meanwhile; the dialog keeps its text and cursor.
function CardEditor:showHighlight(dialog)
    local keyboard = dialog:isKeyboardVisible()
    if keyboard then dialog:onCloseKeyboard() end
    local viewer
    viewer = TextViewer:new{
        title = _("Highlight"),
        text = self.highlight.text,
        width = Screen:getWidth(),
        height = Screen:getHeight(),
        show_menu = false,
        buttons_table = { {
            {
                text = _("Close"),
                callback = function() viewer:onClose() end,
            },
        } },
        close_callback = function()
            if keyboard then dialog:onShowKeyboard() end
        end,
    }
    UIManager:show(viewer)
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
