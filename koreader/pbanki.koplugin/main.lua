--[[--
Makes flashcards for pbAnki from KOReader highlights.

"Create flashcard" in the highlight menu saves the highlight and opens the
card editor. "Flashcards from highlights" walks through the book's highlights
that have not been reviewed yet. Reviewed highlights are remembered in the
book's settings file.

With the AI Assistant plugin installed, the editor is filled with suggested
cards. Cards are appended as JSON lines to pbAnki's inbox, which pbAnki
imports into its "KOReader" deck.

@module koplugin.pbanki
--]]--

local CardEditor = require("cardeditor")
local Dispatcher = require("dispatcher")
local InfoMessage = require("ui/widget/infomessage")
local NetworkMgr = require("ui/network/manager")
local Notification = require("ui/widget/notification")
local SpinWidget = require("ui/widget/spinwidget")
local Trapper = require("ui/trapper")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local cardgen = require("cardgen")
local json = require("json")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local util = require("util")
local _ = require("gettext")
local T = require("ffi/util").template

local PBANKI_DIR = os.getenv("PBANKI_DIR") or "/mnt/ext1/applications/pbanki"
local INBOX_FILE = PBANKI_DIR .. "/inbox.jsonl"

-- Words of book text on each side of the highlight sent to the AI
local CONTEXT_WORDS = 150
local MAX_CARDS = 4

local PbAnki = WidgetContainer:extend{
    name = "pbanki",
    is_doc_only = true,
}

function PbAnki:init()
    self.settings = G_reader_settings:readSetting("pbanki", {
        use_ai = true,
        -- Batch mode: the AI writes 1 to this many cards per highlight
        max_cards_batch = 3,
    })
    self:onDispatcherRegisterActions()
    self.ui.menu:registerToMainMenu(self)
    if self.ui.highlight then
        self.ui.highlight:addToHighlightDialog("12_pbanki_create_flashcard", function(highlight, index)
            return {
                text = _("Create flashcard"),
                callback = function()
                    self:createFromSelection(highlight, index)
                end,
            }
        end)
    end
end

function PbAnki:onDispatcherRegisterActions()
    Dispatcher:registerAction("pbanki_review_highlights", {
        category = "none",
        event = "PbAnkiReviewHighlights",
        title = _("Flashcards from highlights"),
        reader = true,
    })
end

function PbAnki:addToMainMenu(menu_items)
    menu_items.pbanki = {
        text = _("pbAnki flashcards"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text_func = function()
                    local count = #self:pendingHighlights()
                    return count > 0 and T(_("Flashcards from highlights (%1 new)"), count)
                        or _("Flashcards from highlights")
                end,
                callback = function() self:onPbAnkiReviewHighlights() end,
                separator = true,
            },
            {
                text = _("Suggest cards with AI"),
                checked_func = function() return self.settings.use_ai end,
                enabled_func = function() return self:assistant() ~= nil end,
                callback = function()
                    self.settings.use_ai = not self.settings.use_ai
                    self:saveSettings()
                end,
                help_text = _("Fill the card editor with AI suggestions from the AI Assistant plugin. Turn off when you have no network, e.g. in flight mode."),
            },
            {
                text_func = function()
                    return T(_("Most cards per highlight in batch: %1"), self.settings.max_cards_batch)
                end,
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    UIManager:show(SpinWidget:new{
                        title_text = _("Most cards per highlight"),
                        info_text = _("In Flashcards from highlights, the AI writes one card for a focused highlight and more, up to this many, for a rich one."),
                        value = self.settings.max_cards_batch,
                        value_min = 1,
                        value_max = MAX_CARDS,
                        default_value = 3,
                        callback = function(spin)
                            self.settings.max_cards_batch = spin.value
                            self:saveSettings()
                            touchmenu_instance:updateItems()
                        end,
                    })
                end,
            },
            {
                text_func = function()
                    local assistant = self:assistant()
                    if not assistant then
                        return _("AI model: AI Assistant not installed")
                    end
                    return T(_("AI model: %1"), assistant:flashcardsModelLabel())
                end,
                enabled_func = function() return self:assistant() ~= nil end,
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    self:assistant():showFlashcardsSettings(function()
                        touchmenu_instance:updateItems()
                    end)
                end,
                separator = true,
            },
            {
                text = _("Review this book's highlights again"),
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    self.ui.doc_settings:delSetting("pbanki_reviewed")
                    touchmenu_instance:updateItems()
                    UIManager:show(Notification:new{ text = _("All highlights in this book are new again.") })
                end,
            },
        },
    }
end

function PbAnki:saveSettings()
    G_reader_settings:saveSetting("pbanki", self.settings)
end

--- AI Assistant plugin with the flashcards API, or nil.
function PbAnki:assistant()
    local assistant = self.ui.assistant
    if assistant and assistant.queryFlashcards then
        return assistant
    end
end

function PbAnki:aiEnabled()
    return self.settings.use_ai and self:assistant() ~= nil
end

-- Reviewed highlights

local function highlightKey(item)
    local pos = type(item.pos0) == "table" and tostring(item.pos0.page) or tostring(item.pos0)
    return tostring(item.datetime) .. "|" .. pos
end

function PbAnki:reviewed()
    return self.ui.doc_settings:readSetting("pbanki_reviewed") or {}
end

function PbAnki:markReviewed(item)
    local reviewed = self:reviewed()
    reviewed[highlightKey(item)] = true
    self.ui.doc_settings:saveSetting("pbanki_reviewed", reviewed)
end

--- Highlights in book order that have not been reviewed.
function PbAnki:pendingHighlights()
    local reviewed = self:reviewed()
    local pending = {}
    for _idx, item in ipairs(self.ui.annotation.annotations or {}) do
        if item.drawer and item.text and item.text ~= "" and not reviewed[highlightKey(item)] then
            table.insert(pending, item)
        end
    end
    return pending
end

-- Highlight details

--- Card source fields and AI context for a saved highlight.
function PbAnki:describe(item, before, after)
    local props = self.ui.doc_props or {}
    local doc = self.ui.document
    local page, percent
    if self.ui.rolling then
        page = doc:getPageFromXPointer(item.pos0)
    else
        page = item.pos0.page
    end
    local total = doc:getPageCount()
    if page and total and total > 0 then
        percent = 100 * page / total
    end
    if before == nil and self.ui.rolling then
        local ok, prev, next = pcall(doc.getSelectedWordContext, doc,
            item.text, CONTEXT_WORDS, item.pos0, item.pos1, false)
        if ok then
            before, after = prev, next
        end
    end
    local chapter = item.chapter
    if (not chapter or chapter == "") and self.ui.toc then
        chapter = self.ui.toc:getTocTitleByPage(self.ui.rolling and item.pos0 or page)
    end
    return {
        item = item,
        text = util.cleanupSelectedText(item.text),
        note = item.note,
        before = before and util.cleanupSelectedText(before) or nil,
        after = after and util.cleanupSelectedText(after) or nil,
        percent = percent,
        book = props.display_title,
        author = props.authors and props.authors:gsub("\n", ", ") or nil,
        chapter = chapter ~= "" and chapter or nil,
        page = page,
        file = doc.file,
        -- An xpointer in reflowable books, the page number in fixed-layout ones
        position = self.ui.rolling and item.pos0 or tostring(page),
    }
end

-- AI suggestions

--- Asks how many cards to suggest, then calls `fn(count)`.
-- `on_write_own` adds a button to skip the AI.
function PbAnki:askCardCount(value, fn, on_write_own)
    UIManager:show(SpinWidget:new{
        title_text = _("Cards to suggest"),
        value = value,
        value_min = 1,
        value_max = MAX_CARDS,
        precision = "%d",
        ok_text = _("Suggest"),
        ok_always_enabled = true,
        cancel_text = _("Cancel"),
        extra_text = on_write_own and _("Write my own") or nil,
        extra_callback = on_write_own,
        callback = function(spin) fn(spin.value) end,
    })
end

--- Asks the AI for cards and shows them in `editor`, per `editor.request`
-- (see cardgen.buildMessages).
function PbAnki:suggest(editor)
    local assistant = self:assistant()
    if not assistant then return end
    local highlight = editor.highlight
    local request = editor.request
    local function run()
        Trapper:wrap(function()
            local companion, position_note = assistant:companionContext(highlight.file, highlight.percent or 0)
            local messages = cardgen.buildMessages(highlight, request, companion, position_note)
            local answer, err = assistant:queryFlashcards(messages, _("Suggesting cards…"))
            -- The editor may have moved on or closed while waiting
            if editor.highlight ~= highlight or not UIManager:isWidgetShown(editor) then return end
            if err then
                if not (assistant.querier and assistant.querier.user_interrupted) then
                    UIManager:show(InfoMessage:new{ text = T(_("No suggestions: %1"), tostring(err)) })
                end
                return
            end
            local cards, parse_err = cardgen.parseCards(answer, cardgen.cardLimit(request), json.decode)
            if not cards then
                logger.warn("pbanki: unreadable AI reply", answer)
                UIManager:show(InfoMessage:new{ text = parse_err })
                return
            end
            editor:setCards(cards)
        end)
    end
    if NetworkMgr:isOnline() then
        run()
    else
        NetworkMgr:turnOnWifiAndWaitForConnection(run)
    end
end

--- Suggests cards for a highlight just shown in `editor`, or opens a blank
-- card when AI is off or not wanted.
function PbAnki:fill(editor)
    UIManager:nextTick(function()
        if editor.request and self:aiEnabled() then
            self:suggest(editor)
        else
            editor:editCard(nil)
        end
    end)
end

function PbAnki:newEditor(highlight, opts)
    local editor = CardEditor:new{
        title = opts.title,
        subtitle = opts.subtitle,
        highlight = highlight,
        max_cards = MAX_CARDS,
        can_suggest = self:assistant() ~= nil,
        request = opts.request,
        on_suggest = function(e)
            if not opts.ask_count then
                self:suggest(e)
                return
            end
            self:askCardCount(e.request and e.request.count or 1, function(count)
                e.request = { count = count }
                self:suggest(e)
            end)
        end,
        on_save = opts.on_save,
        on_skip = opts.on_skip,
        on_close = opts.on_close,
    }
    UIManager:show(editor)
    self:fill(editor)
    return editor
end

-- Mid-book: from the highlight menu

function PbAnki:createFromSelection(highlight, index)
    local before, after = highlight:getSelectedWordContext(CONTEXT_WORDS)
    if not index then
        -- New selection: save it as a normal highlight first
        index = highlight:saveHighlight(true)
    end
    highlight:onClose()
    local item = index and self.ui.annotation.annotations[index]
    if not item then return end

    local h = self:describe(item, before or "", after or "")
    local function open(request)
        self:newEditor(h, {
            title = _("Create flashcard"),
            subtitle = self.ui.doc_props and self.ui.doc_props.display_title,
            request = request,
            ask_count = true,
            on_save = function(editor, cards)
                if self:saveCards(editor.highlight, cards) then
                    self:markReviewed(item)
                    UIManager:close(editor)
                end
            end,
        })
    end
    if self:aiEnabled() then
        self:askCardCount(1, function(count) open({ count = count }) end, function() open(nil) end)
    else
        open(nil)
    end
end

-- Batch: through the book's pending highlights

function PbAnki:onPbAnkiReviewHighlights()
    local pending = self:pendingHighlights()
    if #pending == 0 then
        UIManager:show(InfoMessage:new{ text = _("No new highlights in this book."), timeout = 3 })
        return true
    end

    local position = 1
    local saved = 0
    local function subtitle()
        return T(_("Highlight %1 of %2"), position, #pending)
    end
    local function advance(editor)
        self:markReviewed(pending[position])
        position = position + 1
        if position > #pending then
            UIManager:close(editor)
            UIManager:show(InfoMessage:new{
                text = T(_("All highlights reviewed. %1 cards saved."), saved),
                timeout = 3,
            })
            return
        end
        editor:setHighlight(self:describe(pending[position]), subtitle())
        self:fill(editor)
    end

    self:newEditor(self:describe(pending[position]), {
        title = _("Flashcards from highlights"),
        subtitle = subtitle(),
        request = { max = self.settings.max_cards_batch },
        on_save = function(editor, cards)
            if self:saveCards(editor.highlight, cards, true) then
                saved = saved + #cards
                advance(editor)
            end
        end,
        on_skip = advance,
    })
    return true
end

-- Inbox

--- Appends cards to pbAnki's inbox and confirms.
function PbAnki:saveCards(h, cards, quiet)
    for _idx, card in ipairs(cards) do
        local ok, err = self:appendToInbox{
            front = card.front,
            back = card.back,
            quote = h.text,
            book = h.book,
            author = h.author,
            chapter = h.chapter,
            page = h.page,
            file = h.file,
            position = h.position,
        }
        if not ok then
            UIManager:show(InfoMessage:new{ text = _("Could not save card: ") .. tostring(err) })
            return false
        end
    end
    local text = #cards == 1 and _("Card saved for pbAnki.") or T(_("%1 cards saved for pbAnki."), #cards)
    if quiet then
        UIManager:show(Notification:new{ text = text })
    else
        UIManager:show(InfoMessage:new{ text = text, timeout = 2 })
    end
    return true
end

function PbAnki:appendToInbox(card)
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
