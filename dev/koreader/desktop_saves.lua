-- Reports what the pbAnki plugin did when the book opened, then saves as closing the book does.
-- Run through dev/koreader/check_desktop_saves.sh.
local SIDECAR = "/mnt/ext1/English/book.sdr/metadata.epub.lua"
local REVIEWED_FILE = os.getenv("PBANKI_DIR") .. "/reviewed.jsonl"

local function log(...) io.stderr:write("uxdriver ", table.concat({ ... }, " "), "\n") end
local function count(t)
    local n = 0
    for _key in pairs(t or {}) do n = n + 1 end
    return n
end
local function fileExists(path)
    local f = io.open(path, "rb")
    if f then f:close() end
    return f ~= nil
end

return {
    function(ui)
        -- Read from disk before anything saves the book's settings
        local disk = dofile(SIDECAR)
        log("opened", "on_disk_reviewed=" .. count(disk.pbanki_reviewed),
            "on_disk_history=" .. count(disk.pbanki_card_history),
            "annotations=" .. #ui.annotation.annotations,
            "pending=" .. #ui.pbanki:pendingHighlights())
        if not fileExists(REVIEWED_FILE) then
            local start = os.clock()
            for _i = 1, 1000 do ui.pbanki:applyDesktopSaves() end
            log("no_file", string.format("microseconds_per_call=%.1f", (os.clock() - start) * 1000))
        end
    end,
    function(ui)
        ui:saveSettings()
        log("saved")
    end,
}
