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
local ConfirmBox = require("ui/widget/confirmbox")
local Dispatcher = require("dispatcher")
local InputDialog = require("ui/widget/inputdialog")
local InfoMessage = require("ui/widget/infomessage")
local NetworkMgr = require("ui/network/manager")
local Notification = require("ui/widget/notification")
local SpinWidget = require("ui/widget/spinwidget")
local TextViewer = require("ui/widget/textviewer")
local Trapper = require("ui/trapper")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local cardgen = require("cardgen")
local json = require("json")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local prompts = require("prompts")
local util = require("util")
local _ = require("gettext")
local T = require("ffi/util").template

local PBANKI_DIR = os.getenv("PBANKI_DIR") or "/mnt/ext1/applications/pbanki"
local INBOX_FILE = PBANKI_DIR .. "/inbox.jsonl"

-- Words of book text on each side of the highlight sent to the AI
local CONTEXT_WORDS = 150
local MAX_CARDS = 10
-- Saved cards remembered per book, and how many of them the AI sees
local HISTORY_KEPT = 40
local HISTORY_SHOWN = 8
-- Longest selection offered as a term for definition cards
local DEFINITION_MAX_WORDS = 3

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
    -- The AI writes cloze cards instead of questions
    self.settings.cloze = self.settings.cloze or false
    -- Saved AI prompts and the id of the one books use unless they pick another
    self.settings.prompts = self.settings.prompts or {}
    self.settings.default_prompt = self.settings.default_prompt or prompts.STANDARD_ID
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
        self.ui.highlight:addToHighlightDialog("13_pbanki_definition", function(highlight, index)
            return {
                text = _("Suggest definition flashcards"),
                show_in_highlight_dialog_func = function()
                    local selected = highlight.selected_text and highlight.selected_text.text or ""
                    local _words, count = selected:gsub("%S+", "")
                    return count >= 1 and count <= DEFINITION_MAX_WORDS and self:aiEnabled()
                end,
                callback = function()
                    self:createDefinitionFromSelection(highlight, index)
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
                text = _("Suggest cloze cards"),
                checked_func = function() return self.settings.cloze end,
                enabled_func = function() return self:assistant() ~= nil end,
                callback = function()
                    self.settings.cloze = not self.settings.cloze
                    self:saveSettings()
                end,
                help_text = _("The AI writes sentences with the answer hidden, like \"Self-control needs the [pause-and-plan] response\", instead of questions. Also set by the Cloze button when asking for suggestions."),
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
            },
            {
                text_func = function()
                    return T(_("AI prompt: %1"), self:activePrompt().name)
                end,
                enabled_func = function() return self:assistant() ~= nil end,
                sub_item_table_func = function() return self:promptsMenu() end,
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

-- AI prompts

--- The prompt this book uses.
function PbAnki:activePrompt()
    return prompts.resolve(self.settings.prompts, self.ui.doc_settings:readSetting("pbanki_prompt"),
        self.settings.default_prompt)
end

--- Saves prompt changes to disk right away, so typed edits survive a crash.
function PbAnki:savePrompts()
    self:saveSettings()
    G_reader_settings:flush()
end

--- Asks for a prompt name, then calls `fn(name)`.
function PbAnki:askPromptName(title, name, ok_text, fn)
    local dialog
    dialog = InputDialog:new{
        title = title,
        input = name,
        allow_newline = false,
        buttons = { {
            {
                text = _("Cancel"),
                id = "close",
                callback = function() UIManager:close(dialog) end,
            },
            {
                text = ok_text,
                id = "done",
                is_enter_default = true,
                callback = function()
                    local text = util.trim(dialog:getInputText())
                    if text == "" then return end
                    UIManager:close(dialog)
                    fn(text)
                end,
            },
        } },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

--- Shows `prompt`'s instructions for `kind`, in an editor unless it is Standard.
function PbAnki:editPromptText(prompt, kind, touchmenu_instance)
    local title = T(_("%1: %2"), prompt.name, _(kind.label))
    if prompt.builtin then
        UIManager:show(TextViewer:new{
            title = title,
            text = prompts.text(prompt, kind.key),
        })
        return
    end
    local dialog
    dialog = InputDialog:new{
        title = title,
        description = _("The reply format is added after this automatically."),
        input = prompts.text(prompt, kind.key),
        fullscreen = true,
        condensed = true,
        allow_newline = true,
        cursor_at_end = false,
        add_nav_bar = true,
        reset_button_text = _("Standard"),
        reset_callback = function()
            return prompts.text(prompts.STANDARD, kind.key), _("Standard text restored. Save to keep it.")
        end,
        save_callback = function(content)
            prompts.setText(prompt, kind.key, content)
            self:savePrompts()
            touchmenu_instance:updateItems()
            return true, _("Prompt saved.")
        end,
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

--- A menu table that rebuilds itself when a submenu closes.
function PbAnki:refreshingMenu(build)
    local items = build()
    items.needs_refresh = true
    items.refresh_func = function() return self:refreshingMenu(build) end
    return items
end

--- Menu for choosing and managing AI prompts.
function PbAnki:promptsMenu()
    return self:refreshingMenu(function()
        local saved = self.settings.prompts
        local items = {
            {
                text_func = function()
                    local own = prompts.find(saved, self.ui.doc_settings:readSetting("pbanki_prompt"))
                    return own and T(_("This book uses: %1"), own.name)
                        or T(_("This book uses: %1 (default)"), self:activePrompt().name)
                end,
                sub_item_table_func = function() return self:bookPromptMenu() end,
                help_text = _("The prompt the AI follows for cards from this book. Books use the default unless you pick another."),
            },
            {
                text_func = function()
                    return T(_("Default for all books: %1"), prompts.resolve(saved, nil, self.settings.default_prompt).name)
                end,
                sub_item_table_func = function() return self:defaultPromptMenu() end,
                separator = true,
            },
        }
        for _idx, p in ipairs(prompts.all(saved)) do
            table.insert(items, {
                text_func = function()
                    return p.builtin and T(_("%1 (built-in)"), p.name) or p.name
                end,
                sub_item_table_func = function() return self:promptMenu(p) end,
            })
        end
        return items
    end)
end

function PbAnki:bookPromptMenu()
    local function bookChoice()
        return prompts.find(self.settings.prompts, self.ui.doc_settings:readSetting("pbanki_prompt"))
    end
    local items = {
        {
            text_func = function()
                return T(_("Default (%1)"), prompts.resolve(self.settings.prompts, nil, self.settings.default_prompt).name)
            end,
            radio = true,
            checked_func = function() return bookChoice() == nil end,
            callback = function() self.ui.doc_settings:delSetting("pbanki_prompt") end,
            separator = true,
        },
    }
    for _idx, p in ipairs(prompts.all(self.settings.prompts)) do
        table.insert(items, {
            text = p.name,
            radio = true,
            checked_func = function() return bookChoice() == p end,
            callback = function() self.ui.doc_settings:saveSetting("pbanki_prompt", p.id) end,
        })
    end
    return items
end

function PbAnki:defaultPromptMenu()
    local items = {}
    for _idx, p in ipairs(prompts.all(self.settings.prompts)) do
        table.insert(items, {
            text = p.name,
            radio = true,
            checked_func = function()
                return prompts.resolve(self.settings.prompts, nil, self.settings.default_prompt) == p
            end,
            callback = function()
                self.settings.default_prompt = p.id
                self:savePrompts()
            end,
        })
    end
    return items
end

--- Menu for one prompt: its instructions, rename, duplicate and delete.
function PbAnki:promptMenu(prompt)
    local saved = self.settings.prompts
    local items = {}
    for _idx, kind in ipairs(prompts.KINDS) do
        table.insert(items, {
            text_func = function()
                if prompt.builtin then return _(kind.label) end
                return prompts.isEdited(prompt, kind.key) and T(_("%1: edited"), _(kind.label))
                    or T(_("%1: same as Standard"), _(kind.label))
            end,
            keep_menu_open = true,
            callback = function(touchmenu_instance)
                self:editPromptText(prompt, kind, touchmenu_instance)
            end,
        })
    end
    items[#items].separator = true
    table.insert(items, {
        text = _("Rename"),
        enabled_func = function() return not prompt.builtin end,
        keep_menu_open = true,
        callback = function(touchmenu_instance)
            self:askPromptName(_("Rename prompt"), prompt.name, _("Rename"), function(name)
                prompt.name = name
                self:savePrompts()
                touchmenu_instance:updateItems()
            end)
        end,
    })
    table.insert(items, {
        text = _("Duplicate"),
        keep_menu_open = true,
        callback = function(touchmenu_instance)
            self:askPromptName(_("Duplicate prompt"), prompts.copyName(saved, prompt.name), _("Duplicate"),
                function(name)
                    prompts.duplicate(saved, prompt, name)
                    self:savePrompts()
                    touchmenu_instance:backToUpperMenu()
                    UIManager:show(Notification:new{ text = T(_("Added %1."), name) })
                end)
        end,
    })
    table.insert(items, {
        text = _("Delete"),
        enabled_func = function() return not prompt.builtin end,
        keep_menu_open = true,
        callback = function(touchmenu_instance)
            UIManager:show(ConfirmBox:new{
                text = T(_("Delete the prompt %1? Books that use it go back to the default."), prompt.name),
                ok_text = _("Delete"),
                ok_callback = function()
                    prompts.remove(saved, prompt.id)
                    if self.settings.default_prompt == prompt.id then
                        self.settings.default_prompt = prompts.STANDARD_ID
                    end
                    self:savePrompts()
                    touchmenu_instance:backToUpperMenu()
                end,
            })
        end,
    })
    return items
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

-- Card history: the book's latest saved cards, which steer new suggestions

function PbAnki:cardHistory()
    return self.ui.doc_settings:readSetting("pbanki_card_history") or {}
end

function PbAnki:addToCardHistory(h, cards)
    local entries = {}
    for _idx, card in ipairs(cards) do
        table.insert(entries, cardgen.historyEntry(h, card))
    end
    self.ui.doc_settings:saveSetting("pbanki_card_history",
        cardgen.appendHistory(self:cardHistory(), entries, HISTORY_KEPT))
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

--- Asks how many cards to suggest and whether they are cloze cards, then
-- calls `fn(count)`. `on_write_own` adds a button to skip the AI.
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
        -- A checkbox: the dialog closes on tap, so it opens again with the new state
        option_text = (self.settings.cloze and "✓ " or "▢ ") .. _("Cloze"),
        option_callback = function(spin)
            self.settings.cloze = not self.settings.cloze
            self:saveSettings()
            self:askCardCount(spin.value, fn, on_write_own)
        end,
        callback = function(spin) fn(spin.value) end,
    })
end

--- Asks for optional guidance for the AI, kept on `editor`, then calls `fn`.
function PbAnki:askGuidance(editor, fn)
    local dialog
    dialog = InputDialog:new{
        title = #editor.cards > 0 and _("Suggest more") or _("Suggest cards"),
        description = _("Optional: tell the AI what to focus on or how to write the cards."),
        input = editor.guidance or "",
        input_hint = _("e.g. ask about the numbers"),
        allow_newline = false,
        buttons = { {
            {
                text = _("Cancel"),
                id = "close",
                callback = function() UIManager:close(dialog) end,
            },
            {
                text = _("Suggest"),
                id = "done",
                is_enter_default = true,
                callback = function()
                    local text = util.trim(dialog:getInputText())
                    editor.guidance = text ~= "" and text or nil
                    UIManager:close(dialog)
                    fn()
                end,
            },
        } },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

--- `request` with the book's prompt, card history and the cloze setting added.
function PbAnki:fullRequest(request)
    local full = {}
    for key, value in pairs(request) do full[key] = value end
    full.prompt = self:activePrompt()
    if not request.definition then
        full.recent = cardgen.pickRecent(self:cardHistory(), HISTORY_SHOWN)
        full.cloze = self.settings.cloze
    end
    return full
end

--- Messages asking the AI for cards for highlight `h`, per a full request.
function PbAnki:messagesFor(h, request)
    local companion, position_note = self:assistant():companionContext(h.file, h.percent or 0)
    return cardgen.buildMessages(h, request, companion, position_note)
end

--- Asks the AI for cards and adds them to `editor`, per `editor.request`
-- (see cardgen.buildMessages). Calls `after` once the editor has its cards,
-- or the AI could not suggest any.
function PbAnki:suggest(editor, after)
    local assistant = self:assistant()
    if not assistant then return end
    local highlight = editor.highlight
    local request = self:fullRequest{
        count = editor.request.count,
        max = editor.request.max,
        definition = editor.request.definition,
        guidance = editor.guidance,
        existing = editor.cards,
        rejected = editor.rejected,
    }
    local function run()
        Trapper:wrap(function()
            local answer, err = assistant:queryFlashcards(self:messagesFor(highlight, request),
                _("Suggesting cards…"))
            -- The editor may have moved on or closed while waiting
            if editor.highlight ~= highlight or not UIManager:isWidgetShown(editor) then return end
            if err then
                if not (assistant.querier and assistant.querier.user_interrupted) then
                    UIManager:show(InfoMessage:new{ text = T(_("No suggestions: %1"), tostring(err)) })
                end
            else
                local cards, parse_err = cardgen.parseReply(answer, request, highlight, json.decode)
                if cards then
                    editor:addCards(cards)
                else
                    logger.warn("pbanki: unreadable AI reply", answer)
                    UIManager:show(InfoMessage:new{ text = parse_err })
                end
            end
            if after then after() end
        end)
    end
    if NetworkMgr:isOnline() then
        run()
    else
        NetworkMgr:turnOnWifiAndWaitForConnection(run)
    end
end

--- Asks the AI for cards for highlight `h` with nothing on screen. Calls
-- `done(cards)`, with nil cards when that failed. Returns a cancel function.
function PbAnki:suggestInBackground(h, request, done)
    request = self:fullRequest(request)
    return self:assistant():queryFlashcardsInBackground(self:messagesFor(h, request), function(answer, err)
        if not answer then
            logger.info("pbanki: background suggestion failed", err)
            done(nil)
            return
        end
        local cards = cardgen.parseReply(answer, request, h, json.decode)
        if not cards then logger.warn("pbanki: unreadable AI reply", answer) end
        done(cards)
    end)
end

--- Suggests cards for a highlight just shown in `editor`, or opens a blank
-- card when AI is off or not wanted. Calls `after` once suggestions are in.
function PbAnki:fill(editor, after)
    UIManager:nextTick(function()
        if editor.request and self:aiEnabled() then
            self:suggest(editor, after)
        else
            editor:editCard(nil, true)
        end
    end)
end

function PbAnki:newEditor(highlight, opts)
    local editor = CardEditor:new{
        title = opts.title,
        subtitle = opts.subtitle,
        highlight = highlight,
        can_suggest = self:assistant() ~= nil,
        request = opts.request,
        on_suggest = function(e)
            self:askGuidance(e, function()
                if not opts.ask_count then
                    self:suggest(e)
                    return
                end
                self:askCardCount(e.request and e.request.count or 1, function(count)
                    e.request = { count = count }
                    self:suggest(e)
                end)
            end)
        end,
        on_save = opts.on_save,
        on_skip = opts.on_skip,
        on_close = opts.on_close,
    }
    UIManager:show(editor)
    self:fill(editor, opts.after_fill)
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

--- Opens the editor with a term and definition pair for a short selection.
function PbAnki:createDefinitionFromSelection(highlight, index)
    local before, after = highlight:getSelectedWordContext(CONTEXT_WORDS)
    if not index then
        index = highlight:saveHighlight(true)
    end
    highlight:onClose()
    local item = index and self.ui.annotation.annotations[index]
    if not item then return end

    self:newEditor(self:describe(item, before or "", after or ""), {
        title = _("Definition flashcards"),
        subtitle = self.ui.doc_props and self.ui.doc_props.display_title,
        request = { definition = true },
        on_save = function(editor, cards)
            if self:saveCards(editor.highlight, cards, false, true) then
                self:markReviewed(item)
                UIManager:close(editor)
            end
        end,
    })
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
    local request = { max = self.settings.max_cards_batch }
    local function subtitle()
        return T(_("Highlight %1 of %2"), position, #pending)
    end

    -- Suggestions for the next highlight, fetched in the background while
    -- the reader works on the current one
    local prefetch
    -- Background suggestions the shown highlight is still waiting for
    local awaited
    local function cancelPrefetch()
        if prefetch and not prefetch.done then prefetch.cancel() end
        if awaited and not awaited.done then awaited.cancel() end
        prefetch, awaited = nil, nil
    end
    local function startPrefetch()
        local next_position = position + 1
        local assistant = self:assistant()
        if next_position > #pending or not self:aiEnabled() or not assistant.queryFlashcardsInBackground
                or not NetworkMgr:isOnline() then
            return
        end
        local entry = { position = next_position, highlight = self:describe(pending[next_position]) }
        prefetch = entry
        entry.cancel = self:suggestInBackground(entry.highlight, request, function(cards)
            entry.done, entry.cards = true, cards
            if entry.on_done then entry.on_done() end
        end)
    end

    local function advance(editor)
        if awaited and not awaited.done then awaited.cancel() end
        awaited = nil
        self:markReviewed(pending[position])
        position = position + 1
        if position > #pending then
            cancelPrefetch()
            UIManager:close(editor)
            UIManager:show(InfoMessage:new{
                text = T(_("All highlights reviewed. %1 cards saved."), saved),
                timeout = 3,
            })
            return
        end
        local entry = prefetch
        if not (entry and entry.position == position) then
            cancelPrefetch()
            editor:setHighlight(self:describe(pending[position]), subtitle())
            self:fill(editor, startPrefetch)
            return
        end
        prefetch = nil
        editor:setHighlight(entry.highlight, subtitle())
        local function apply()
            if editor.highlight ~= entry.highlight or not UIManager:isWidgetShown(editor) then return end
            if not entry.cards then
                -- The background request failed: ask again in the foreground
                editor:setWaiting(false)
                self:fill(editor, startPrefetch)
                return
            end
            -- After any card written while waiting
            editor:addCards(entry.cards)
            startPrefetch()
        end
        if entry.done then
            apply()
        else
            editor:setWaiting(true)
            awaited = entry
            entry.on_done = apply
        end
    end

    self:newEditor(self:describe(pending[position]), {
        title = _("Flashcards from highlights"),
        subtitle = subtitle(),
        request = request,
        after_fill = startPrefetch,
        on_save = function(editor, cards)
            if self:saveCards(editor.highlight, cards, true) then
                saved = saved + #cards
                advance(editor)
            end
        end,
        on_skip = advance,
        on_close = cancelPrefetch,
    })
    return true
end

-- Inbox

--- Appends cards to pbAnki's inbox and confirms. `no_history` keeps them
-- out of the card history that steers suggestions.
function PbAnki:saveCards(h, cards, quiet, no_history)
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
    if not no_history then self:addToCardHistory(h, cards) end
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
