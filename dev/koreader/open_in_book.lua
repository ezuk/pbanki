-- pbAnki's Open in book: a jump request, then KOReader comes to the front.
local json = require("json")

local function request(position, age)
    local f = io.open("/app/pbanki/open-in-book.json", "w")
    f:write(json.encode{ file = "/tmp/book.epub", position = position, time = os.time() - (age or 0) })
    f:close()
end
local function where(ui, tag)
    io.stderr:write("uxdriver ", tag, ": page ", ui.document:getCurrentPage(),
        " request left=", tostring(io.open("/app/pbanki/open-in-book.json") ~= nil), "\n")
end

return {
    function(ui) where(ui, "start") end,
    -- A stale request is dropped without moving
    function(ui) request("/body/DocFragment[2]/body/p[19]/text().0", 600); ui.pbanki:onResume() end,
    function(ui) where(ui, "stale request") end,
    function(ui) request("/body/DocFragment[2]/body/p[19]/text().0"); ui.pbanki:onResume() end,
    function(ui, shot) where(ui, "jumped"); shot("open_in_book") end,
    function(ui) ui.link:onGoBackLink() end,
    function(ui, shot) where(ui, "after Back"); shot("open_in_book_back") end,
}
