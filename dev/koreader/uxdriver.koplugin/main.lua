local UIManager = require("ui/uimanager")
local Screen = require("device").screen
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local logger = require("logger")

local Driver = WidgetContainer:extend{ name = "uxdriver", is_doc_only = true }

function Driver:onReaderReady()
    local script = os.getenv("UX_SCRIPT")
    if not script then return end
    local steps = dofile(script)
    local i = 0
    local function shot(name)
        UIManager:forceRePaint()
        Screen:shot("/shots/" .. name .. ".png")
        logger.info("uxdriver: shot", name)
    end
    local function nextStep()
        i = i + 1
        local step = steps[i]
        if not step then
            UIManager:quit()
            return
        end
        local ok, err = pcall(step, self.ui, shot)
        local delay = ok and type(err) == "number" and err or nil
        if not ok then
            logger.err("uxdriver: step", i, "failed:", err)
            io.stderr:write("uxdriver step " .. i .. " failed: " .. tostring(err) .. "\n")
            UIManager:quit()
            return
        end
        UIManager:scheduleIn(delay or tonumber(os.getenv("UX_DELAY")) or 1, nextStep)
    end
    UIManager:scheduleIn(2, nextStep)
end

return Driver
