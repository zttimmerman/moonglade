-- wowstub.lua: the stub client runtime for the Lua unit layer (openspec/changes/test-pyramid design 5).
--
-- Only the client API our glue and add-on code calls is stubbed. Anything else is absent, so calling it fails with
-- "attempt to call global 'X' (a nil value)", which run.lua reports as "unstubbed API: X": the stub can't silently
-- pretend. Behaviour notes cite the installed 3.3.5a client's own GlueXML (Interface/GlueXML/*.lua and *.xml, read
-- from the client's base archives; docs/research/login-screen-spike.md) for the stock functions we stand in for.
-- C API functions (DefaultServerLogin, PlaySound, ...) are recorders: they store their calls in S.calls and change
-- nothing else, except where a note says what state they keep.
--
-- S.new(opts) returns a fresh world: an environment table (env) to load the real .lua files into, the frame
-- registry, a test-driven clock and the call log. opts.saved = { accountName = "", accountList = "" } seeds the
-- client's saved account settings (WTF/Config.wtf accountName / accountList).

local S = {}

local FRAME_METHODS = {}
S.methods = FRAME_METHODS      -- addonstub.lua adds the in-game frame methods here
-- methods are capitalised (the client's widget API); lowercase keys are the stub's own fields. A capitalised name
-- with no stub raises "unstubbed API: Frame:<name>".
local Frame = { __index = function(_, k)
	local m = FRAME_METHODS[k]
	if m == nil and type(k) == "string" and k:match("^%u") then error("unstubbed API: Frame:" .. k, 2) end
	return m
end }

function S.new(opts)
	opts = opts or {}
	local w = { frames = {}, order = {}, calls = {}, clock = 0, events = {},
	            saved = { accountName = (opts.saved or {}).accountName or "",
	                      accountList = (opts.saved or {}).accountList or "" } }
	local env = {}
	w.env = env

	local function record(name, ...)
		table.insert(w.calls, { name = name, args = { ... }, t = w.clock })
	end
	w.record = record
	function w.called(name)
		local out = {}
		for _, c in ipairs(w.calls) do if c.name == name then table.insert(out, c) end end
		return out
	end

	-- frames ------------------------------------------------------------------------------------------------
	function w.frame(spec)
		local f = setmetatable({ w = w, type = spec.type or "Frame", name = spec.name, id = spec.id or 0,
		                         shown = not spec.hidden, scripts = {}, events = {}, text = spec.text,
		                         width = spec.width or 0, height = spec.height or 0, enabled = true,
		                         inherits = spec.inherits or {} }, Frame)
		if spec.parent then
			f.parent = type(spec.parent) == "table" and spec.parent or env[spec.parent]
			if f.parent == nil then error("stub: parent " .. tostring(spec.parent) .. " of " .. tostring(spec.name) .. " doesn't exist", 2) end
			f.parent.children = f.parent.children or {}
			table.insert(f.parent.children, f)
		end
		if spec.name then env[spec.name] = f; w.frames[spec.name] = f end
		table.insert(w.order, f)
		for h, fn in pairs(spec.scripts or {}) do f.scripts[h] = fn end
		return f
	end
	-- the XML prelude's frames: created, then OnLoad (the client runs OnLoad as each frame is created)
	function w.xmlframe(spec)
		local f = w.frame(spec)
		if f.scripts.OnLoad then f.scripts.OnLoad(f) end
		return f
	end

	-- clock and events ----------------------------------------------------------------------------------------
	-- advance(dt): the client calls each visible frame's OnUpdate(self, elapsed) once per frame
	function w.advance(dt, steps)
		for _ = 1, (steps or 1) do
			w.clock = w.clock + dt
			for _, f in ipairs(w.order) do
				if f.scripts.OnUpdate and f:IsVisible() then f.scripts.OnUpdate(f, dt) end
			end
		end
	end
	function w.fire(event, ...)
		for _, f in ipairs(w.order) do
			if f.events[event] and f.scripts.OnEvent then f.scripts.OnEvent(f, event, ...) end
		end
	end

	-- the stock login screen (Interface/GlueXML/AccountLogin.xml): the model frame AccountLogin, its UI child and
	-- logo, and the stock controls our menu hides; plus the glue dialogs our key handler looks at
	local root = w.frame{ type = "Frame", name = "GlueParent" }
	local al = w.frame{ type = "Model", name = "AccountLogin", parent = root }
	w.frame{ type = "Frame", name = "AccountLoginUI", parent = al }
	w.frame{ type = "Texture", name = "AccountLoginLogo", parent = env.AccountLoginUI }
	for _, n in ipairs({ "AccountLoginAccountEdit", "AccountLoginPasswordEdit", "AccountLoginTokenEdit",
		"AccountLoginLoginButton", "AccountLoginSaveAccountName", "AccountLoginSaveAccountNameText",
		"AccountLoginDropDown", "AccountLoginManageAccountButton", "AccountLoginCommunityButton",
		"AccountLoginShowLauncher", "AccountLoginTOSButton", "AccountLoginUpgradeAccountButton",
		"AccountLoginRealmName", "OptionsButton", "AccountLoginCinematicsButton", "AccountLoginCreditsButton",
		"AccountLoginExitButton" }) do
		w.frame{ type = "Button", name = n, parent = env.AccountLoginUI }
	end
	-- the character screen (Interface/GlueXML/CharacterSelect.xml), hidden while the login screen shows
	w.frame{ type = "Frame", name = "CharacterSelect", parent = root, hidden = true }
	for _, n in ipairs({ "GlueDialog", "TOSFrame", "ConnectionHelpFrame", "OptionsSelectFrame" }) do
		w.frame{ type = "Frame", name = n, parent = root, hidden = true }
	end

	-- globals ---------------------------------------------------------------------------------------------------
	env._G = env
	for _, k in ipairs({ "ipairs", "pairs", "next", "type", "tostring", "tonumber", "select", "unpack", "error",
		"pcall", "assert", "setmetatable", "getmetatable", "rawget", "rawset", "math", "string", "table" }) do
		env[k] = _G[k]
	end
	-- the client's string/table aliases (FrameXML/GlueXML use them; they are the Lua library functions)
	env.strupper, env.strlower, env.strsub, env.strlen = string.upper, string.lower, string.sub, string.len
	env.strfind, env.strmatch, env.format, env.strrep = string.find, string.match, string.format, string.rep
	env.tinsert, env.tremove, env.gsub = table.insert, table.remove, string.gsub
	function env.wipe(t) for k in pairs(t) do t[k] = nil end return t end
	function env.strsplit(sep, s, n)
		local out, pos = {}, 1
		while true do
			local a, b = string.find(s, sep, pos, true)
			if not a or (n and #out == n - 1) then table.insert(out, string.sub(s, pos)); break end
			table.insert(out, string.sub(s, pos, a - 1)); pos = b + 1
		end
		return unpack(out)
	end
	function env.GetTime() return w.clock end
	-- GlueXML GlueParent.lua SetGlueScreen(name) sets CURRENT_GLUE_SCREEN; the world starts on the login screen
	env.CURRENT_GLUE_SCREEN = opts.screen or "login"
	-- saved account settings (WTF/Config.wtf accountName / accountList): kept in w.saved
	function env.GetSavedAccountName() return w.saved.accountName end
	function env.SetSavedAccountName(s) record("SetSavedAccountName", s); w.saved.accountName = s end
	function env.GetSavedAccountList() return w.saved.accountList end
	function env.SetSavedAccountList(s) record("SetSavedAccountList", s); w.saved.accountList = s end
	-- recorders (C API)
	for _, n in ipairs({ "PlaySound", "PlayGlueMusic", "StopGlueMusic", "PlayGlueAmbience", "Screenshot",
		"EnterWorld", "SendAddonMessage", "AccountLogin_Exit", "AccountLogin_Cinematics", "AccountLogin_Credits",
		"StatusDialogClick" }) do
		env[n] = function(...) record(n, ...) end
	end
	-- DefaultServerLogin(account, password): records; the client also wipes the password string it's given in
	-- place (measured, see HSFrontMenu_Login's note), which Lua can't do to an immutable string: not modelled.
	function env.DefaultServerLogin(...) record("DefaultServerLogin", ...) end
	function env.GlueDialog_Show(which, text, data) record("GlueDialog_Show", which, text, data) end
	function env.GlueDialog_OnEvent(self, event, ...) record("GlueDialog_OnEvent", event, ...) end
	function env.GetBuildInfo() return "Release", "Release", "3.3.5", "12340", "Jun 24 2010" end
	function env.GetNumCharacters() return #(w.characters or {}) end
	function env.GetCharacterInfo(i) local c = (w.characters or {})[i]; if c then return c.name end end
	-- Stock AccountLogin_OnShow (GlueXML AccountLogin.lua, AccountLogin_OnShow): sets the model's sequence 0,
	-- PlayGlueMusic(CurrentGlueMusic), fills the account edit box from GetSavedAccountName(). The rest (EULA/TOS,
	-- realm name, token box, account drop-down, trial button) touches controls our menu hides.
	function env.AccountLogin_OnShow(self)
		self:SetSequence(0)
		env.PlayGlueMusic(env.CurrentGlueMusic)
		env.AccountLoginAccountEdit.text = env.GetSavedAccountName()
	end
	-- Stock AccountLogin_OnKeyDown (GlueXML AccountLogin.lua): ESCAPE shows the quit dialog, ENTER logs in,
	-- PRINTSCREEN takes a screenshot. Recorded only: our wrapper decides which keys reach it.
	function env.AccountLogin_OnKeyDown(key) record("Stock_AccountLogin_OnKeyDown", key) end
	return w
end

-- frame methods ------------------------------------------------------------------------------------------------
local M = FRAME_METHODS
function M:Show() if not self.shown then self.shown = true; if self.scripts.OnShow and self:IsVisible() then self.scripts.OnShow(self) end end end
function M:Hide() if self.shown then self.shown = false; if self.scripts.OnHide then self.scripts.OnHide(self) end end end
function M:IsShown() return self.shown end
function M:IsVisible()
	local f = self
	while f do if not f.shown then return false end f = f.parent end
	return true
end
function M:SetScript(h, fn) self.scripts[h] = fn end
function M:GetScript(h) return self.scripts[h] end
function M:RegisterEvent(e) self.events[e] = true end
function M:UnregisterEvent(e) self.events[e] = nil end
function M:GetName() return self.name end
function M:GetParent() return self.parent end
function M:GetID() return self.id end
function M:SetID(i) self.id = i end
function M:SetWidth(v) self.width = v end
function M:GetWidth() return self.width end
function M:SetHeight(v) self.height = v end
function M:GetHeight() return self.height end
function M:SetSize(x, y) self.width, self.height = x, y end
function M:SetPoint(...) self.points = self.points or {}; table.insert(self.points, { ... }) end
function M:ClearAllPoints() self.points = {} end
function M:SetAllPoints(r) self.allPoints = r or true end
function M:GetTop() return self.top or 0 end
function M:GetBottom() return self.bottom or 0 end
function M:SetText(t) self.text = t end
function M:GetText() return self.text end
function M:SetTextColor(r, g, b, a) self.textColor = { r, g, b, a } end
function M:SetTexture(t, ...) self.texture = t end
function M:SetVertexColor(r, g, b, a) self.vertexColor = { r, g, b, a } end
function M:SetAlpha(a) self.alpha = a end
function M:GetAlpha() return self.alpha or 1 end
function M:Enable() self.enabled = true end
function M:Disable() self.enabled = false end
function M:IsEnabled() return self.enabled and 1 or nil end
function M:LockHighlight() self.highlight = true end
function M:UnlockHighlight() self.highlight = false end
function M:SetChecked(v) self.checked = v end
function M:GetChecked() return self.checked end
function M:SetModel(m) self.model = m end
function M:SetCamera(c) self.camera = c end
function M:SetSequence(s) self.sequence = s end
function M:SetBackdropColor(r, g, b, a) self.backdropColor = { r, g, b, a } end
function M:SetBackdropBorderColor(r, g, b, a) self.backdropBorderColor = { r, g, b, a } end
-- GetTextWidth: no font metrics in the stub; 7 px per byte stands in (only layout arithmetic depends on it)
function M:GetTextWidth() return 7 * #(self.text or "") end
-- Click(): runs the button's OnClick as a left click (the client does when Click() is called on an enabled button)
function M:Click(button) if self.enabled and self.scripts.OnClick then self.scripts.OnClick(self, button or "LeftButton", false) end end
function M:GetRegions()
	local out = {}
	for _, c in ipairs(self.children or {}) do
		if c.type == "Texture" or c.type == "FontString" then table.insert(out, c) end
	end
	return unpack(out)
end

return S
