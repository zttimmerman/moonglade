-- addonstub.lua: the in-game (FrameXML) side of the stub runtime, for the Moonglade add-on's unit tests
-- (openspec/changes/test-pyramid design 5; part 2: "Lua tests for the add-on's menu hooks and banner").
--
-- Built on wowstub's world (same frames, clock, call log, "unstubbed API" rule). Only what Moonglade.lua calls is
-- added. Stock behaviour stood in for, from the 3.3.5a client's FrameXML:
-- - UIParent (UIParent.xml) and the game menu GameMenuFrame with its Logout/Quit buttons (GameMenuFrame.xml: the menu
--   is a DIALOG-strata frame; its buttons' text comes from LOGOUT / EXIT_GAME);
-- - CreateFrame(type, name, parent) (a global named frame when `name` is given), Frame:HookScript (runs the hook after
--   the frame's own script, as the client does), CreateTexture / CreateFontString (regions with the setters used);
-- - StaticPopupDialogs["CAMP"] (StaticPopup.lua: text = CAMP_TIMER) and the GlobalStrings.lua wording globals;
-- - UnitName("player"): the probe character's name (opts.player); SendAddonMessage and Screenshot are recorders.
local S = require("wowstub")
local A = {}

-- FrameXML GlobalStrings.lua defines these keys (Moonglade.lua replaces them). Their stock wording isn't asserted by
-- any test, so the stub holds placeholders instead of text typed from memory.
local GLOBAL_STRINGS = {}
for _, k in ipairs({ "LOGOUT", "CAMP_TIMER", "CAMP_NOW", "ERR_LOGOUT_FAILED", "PLAYER_LOGOUT_FAILED",
	"PLAYER_LOGOUT_FAILED_ERROR", "IDLE_MESSAGE", "ERR_COMSAT_DISCONNECT", "SERVER_MESSAGE_PREFIX" }) do
	GLOBAL_STRINGS[k] = "<stock " .. k .. ">"
end

function A.new(opts)
	opts = opts or {}
	local w = S.new(opts)
	local env = w.env
	for k, v in pairs(GLOBAL_STRINGS) do env[k] = v end
	local ui = w.frame{ name = "UIParent" }
	ui.strata = "MEDIUM"
	local menu = w.frame{ name = "GameMenuFrame", parent = ui, hidden = true }
	menu.strata = "DIALOG"
	menu.height = 250
	w.frame{ type = "Button", name = "GameMenuButtonMacros", parent = menu, height = 21 }
	w.frame{ type = "Button", name = "GameMenuButtonRatings", parent = menu, hidden = true, height = 21 }
	w.frame{ type = "Button", name = "GameMenuButtonLogout", parent = menu, text = env.LOGOUT, height = 21 }
	w.frame{ type = "Button", name = "GameMenuButtonQuit", parent = menu, text = "<stock EXIT_GAME>", height = 21 }
	-- UIErrorsFrame (UIParent.xml): AddMessage(text, r, g, b, a) shows the red/yellow error text; recorded
	local errs = w.frame{ name = "UIErrorsFrame", parent = ui }
	errs.messages = {}
	env.UISpecialFrames = {}               -- UIParent.lua: frames Escape closes
	env.CANCEL = "<stock CANCEL>"          -- GlobalStrings.lua (placeholder)
	-- StaticPopup.lua: StaticPopup_Show(which, text_arg1, text_arg2, data) / StaticPopup_Hide(which): recorded; a
	-- test accepts a shown dialog with w.accept(which) (its OnAccept(self, data), as the Accept button does)
	w.popups = {}
	function env.StaticPopup_Show(which, a1, a2, data)
		w.record("StaticPopup_Show", which, a1, a2, data); w.popups[which] = { text = a1, data = data }
	end
	function env.StaticPopup_Hide(which) w.record("StaticPopup_Hide", which); w.popups[which] = nil end
	function w.accept(which)
		local p = w.popups[which]
		env.StaticPopupDialogs[which].OnAccept(nil, p and p.data)
	end
	function env.HideUIPanel(f) f:Hide() end                      -- UIParent.lua (no panel layout in the stub)
	-- key bindings (the client's binding set): w.bindings[key] = action
	w.bindings = opts.bindings or {}
	function env.GetCurrentBindingSet() return opts.binding_set or 1 end
	function env.GetBindingAction(key) return w.bindings[key] or "" end
	function env.GetBindingKey(action)
		local keys = {}
		for k, a in pairs(w.bindings) do if a == action then table.insert(keys, k) end end
		table.sort(keys)
		return keys[1]
	end
	function env.SetBinding(key, action) w.record("SetBinding", key, action); w.bindings[key] = action end
	for _, n in ipairs({ "SaveBindings", "Logout", "Quit", "StopCinematic" }) do
		env[n] = function(...) w.record(n, ...) end
	end
	env.StaticPopupDialogs = { CAMP = { text = env.CAMP_TIMER } }
	env.format = string.format
	function env.UnitName(unit) if unit == "player" then return opts.player or "Hsprobe" end end
	function env.InCinematic() return nil end
	function env.CreateFrame(ftype, name, parent, template)
		local f = w.frame{ type = ftype, name = name, parent = parent }
		f.template = template
		return f
	end
	w.ui, w.menu = ui, menu
	return w
end

-- the in-game frame methods Moonglade.lua uses, on wowstub's frame metatable
local M = S.methods
function M:HookScript(h, fn)
	local old = self.scripts[h]
	self.scripts[h] = old and function(...) old(...); fn(...) end or fn
end
function M:SetFrameStrata(s) self.strata = s end
function M:AddMessage(msg, r, g, b, a) self.messages = self.messages or {}; table.insert(self.messages, { msg, r, g, b, a }) end
function M:SetToplevel(v) self.toplevel = v end
function M:EnableMouse(v) self.mouse = v end
function M:SetHighlightTexture(t, mode) self.highlightTexture = t end
function M:SetJustifyH(j) self.justifyH = j end
function M:SetAutoFocus(v) self.autoFocus = v end
function M:SetMaxLetters(n) self.maxLetters = n end
function M:ClearFocus() self.focus = false end
function M:UnregisterAllEvents() self.events = {} end
function M:GetFrameStrata() return self.strata or "MEDIUM" end
function M:SetBackdrop(b) self.backdrop = b end
function M:CreateTexture(name, layer)
	return self.w.frame{ type = "Texture", name = name, parent = self }
end
function M:CreateFontString(name, layer, template)
	local f = self.w.frame{ type = "FontString", name = name, parent = self }
	f.template = template
	return f
end

return A
