-- Small JSON decoder and encoder for the parity tests, which run under plain lua.
local M = {}

local ESCAPES = { ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }

local function utf8Char(code)
    if code < 0x80 then return string.char(code) end
    if code < 0x800 then
        return string.char(0xC0 + math.floor(code / 0x40), 0x80 + code % 0x40)
    end
    if code < 0x10000 then
        return string.char(0xE0 + math.floor(code / 0x1000), 0x80 + math.floor(code / 0x40) % 0x40, 0x80 + code % 0x40)
    end
    return string.char(0xF0 + math.floor(code / 0x40000), 0x80 + math.floor(code / 0x1000) % 0x40,
        0x80 + math.floor(code / 0x40) % 0x40, 0x80 + code % 0x40)
end

--- Decodes `text`, erroring on bad input. JSON null becomes `null_value`.
function M.decode(text, null_value)
    local pos = 1
    local value

    local function skip()
        pos = text:find("[^ \t\r\n]", pos) or #text + 1
    end

    local function fail()
        error("bad json at " .. pos)
    end

    local function str()
        pos = pos + 1
        local out = {}
        while true do
            local c = text:sub(pos, pos)
            if c == "" then fail() end
            if c == '"' then
                pos = pos + 1
                return table.concat(out)
            elseif c == "\\" then
                local e = text:sub(pos + 1, pos + 1)
                if e == "u" then
                    local code = tonumber(text:sub(pos + 2, pos + 5), 16)
                    if not code then fail() end
                    pos = pos + 6
                    if code >= 0xD800 and code < 0xDC00 and text:sub(pos, pos + 1) == "\\u" then
                        local low = tonumber(text:sub(pos + 2, pos + 5), 16)
                        code = 0x10000 + (code - 0xD800) * 0x400 + (low - 0xDC00)
                        pos = pos + 6
                    end
                    table.insert(out, utf8Char(code))
                else
                    if not ESCAPES[e] then fail() end
                    table.insert(out, ESCAPES[e])
                    pos = pos + 2
                end
            else
                table.insert(out, c)
                pos = pos + 1
            end
        end
    end

    function value()
        skip()
        local c = text:sub(pos, pos)
        if c == "{" then
            local obj = {}
            pos = pos + 1
            skip()
            if text:sub(pos, pos) == "}" then pos = pos + 1; return obj end
            while true do
                skip()
                if text:sub(pos, pos) ~= '"' then fail() end
                local key = str()
                skip()
                if text:sub(pos, pos) ~= ":" then fail() end
                pos = pos + 1
                obj[key] = value()
                skip()
                local sep = text:sub(pos, pos)
                pos = pos + 1
                if sep == "}" then return obj end
                if sep ~= "," then fail() end
            end
        elseif c == "[" then
            local list = {}
            pos = pos + 1
            skip()
            if text:sub(pos, pos) == "]" then pos = pos + 1; return list end
            while true do
                list[#list + 1] = value()
                skip()
                local sep = text:sub(pos, pos)
                pos = pos + 1
                if sep == "]" then return list end
                if sep ~= "," then fail() end
            end
        elseif c == '"' then
            return str()
        elseif text:sub(pos, pos + 3) == "true" then
            pos = pos + 4
            return true
        elseif text:sub(pos, pos + 4) == "false" then
            pos = pos + 5
            return false
        elseif text:sub(pos, pos + 3) == "null" then
            pos = pos + 4
            return null_value
        end
        local num = text:match("^-?%d+%.?%d*[eE]?[+-]?%d*", pos)
        if not num or not tonumber(num) then fail() end
        pos = pos + #num
        return tonumber(num)
    end

    local result = value()
    skip()
    if pos <= #text then fail() end
    return result
end

local function encodeString(s)
    return '"' .. s:gsub('[%c"\\]', function(c)
        if c == '"' then return '\\"' end
        if c == "\\" then return "\\\\" end
        return string.format("\\u%04x", c:byte())
    end) .. '"'
end

--- JSON text for `value`. An empty table encodes as a list.
function M.encode(value)
    local kind = type(value)
    if kind == "nil" then return "null" end
    if kind == "string" then return encodeString(value) end
    if kind == "boolean" then return tostring(value) end
    if kind == "number" then
        if value == math.floor(value) then return string.format("%d", value) end
        return string.format("%.17g", value)
    end
    if kind ~= "table" then return "null" end
    local parts = {}
    if value[1] ~= nil or next(value) == nil then
        for _idx, item in ipairs(value) do table.insert(parts, M.encode(item)) end
        return "[" .. table.concat(parts, ",") .. "]"
    end
    for key, item in pairs(value) do
        table.insert(parts, encodeString(tostring(key)) .. ":" .. M.encode(item))
    end
    return "{" .. table.concat(parts, ",") .. "}"
end

return M
