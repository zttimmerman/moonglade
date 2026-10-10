-- Moonglade saves in the game (openspec/changes/front-menu-saves, design.md 5 and 8): a "Saves" button in the game
-- menu (Esc) opens a window with this character's saves (newest first), Save (with an optional name), Load and Delete
-- (own saves only), and two key bindings, Quicksave and Quickload (Bindings.xml, "Moonglade" in Key Bindings).
--
-- The add-on talks to mod-moonglade (MoongladeSaves.cpp) over the add-on channel true-pause uses: an add-on whisper to
-- the player's own character with prefix "MGSAVE" (the server handles and swallows it) and the server's add-on
-- whispers back. Verbs: LIST, "SAVE manual <name>", QUICK, "LOAD <generation> <index>", QLOAD, "DELETE <generation>
-- <index>". LIST answers one line per save, "E\t<generation>\t<index>\t<kind>\t<when>\t<level>\t<zone>\t<label>", then
-- "END\t<generation>\t<count>"; a save's id never reaches the client. Results are "OK\t<verb>\t<text>" or
-- "NO\t<verb>\t<reason>"; a refusal shows as the standard red error text.
--
-- Loading logs the character out at once (the server does it, then restores the save); the character screen then
-- lists the character as saved. The world stays paused while the Saves window is open, as it is in the game menu.
--
-- Default keys: F6 quicksave, F7 quickload. The Skyrim keys F5 and F9 are taken in the client's own default bindings
-- (WTF\DefaultBindings.wtf in Data\patch-3.MPQ: "bind F5 TARGETPARTYMEMBER4", "bind F9 TOGGLEBAG2"); F6 and F7 are the
-- nearest keys bound to nothing there (saves.addon_static reads that file). They are set once, only if the key is still
-- free, and remembered in MoongladeSettings, so a player's own change sticks.
--
-- Test hook (harness checks only): MoongladeTest.hook = "saves" with MoongladeTest.name = <save name> (planted into a
-- scratch play client by the harness): once the world UI is up, the hook opens the window, saves with that name,
-- quicksaves through the binding's function, records what the window lists, then logs out (the report is written then).
-- A second world entry in the same run (the report is already there) quits the game.

BINDING_HEADER_MOONGLADE = "Moonglade";
BINDING_NAME_MOONGLADE_QUICKSAVE = "Quicksave";
BINDING_NAME_MOONGLADE_QUICKLOAD = "Quickload";

local PREFIX = "MGSAVE";
local DEFAULT_KEYS = { MOONGLADE_QUICKSAVE = "F6", MOONGLADE_QUICKLOAD = "F7" };
local ROWS = 10;
local KIND_TEXT = { manual = "Saved", quick = "Quicksave", auto = "Autosave", ["before-load"] = "Before loading" };
local TEXT = {
	title = "Saves",
	save = "Save",
	load = "Load",
	delete = "Delete",
	close = "Close",
	name_hint = "Name (optional)",
	empty = "No saves yet.",
	loading_list = "Reading your saves" .. "\226\128\166",
	saved = "Saved.",
	quicksaved = "Quicksaved.",
	deleted = "Deleted.",
	loading = "Loading" .. "\226\128\166",
	confirm_load = "Load \"%s\"?\n%s goes back to %s. Your other characters aren't affected.",
	confirm_delete = "Delete \"%s\"? This can't be undone.",
	only_own = "Only your own saves can be deleted.",
};
MOONGLADE_SAVES_TEXT = TEXT;

local S = { rows = {}, gen = nil, pending = {}, selected = nil, page = 1, listing = false };
MoongladeSavesState = S;
local hook = { on = false, sent = {}, replies = {}, phase = nil, clock = 0, list = nil };

local function send(text)
	local me = UnitName("player");
	if ( not me ) then return; end
	SendAddonMessage(PREFIX, text, "WHISPER", me);
	if ( hook.on and #hook.sent < 20 ) then table.insert(hook.sent, text); end
end

local function split(s)
	local out = {};
	for part in string.gmatch(s .. "\t", "([^\t]*)\t") do table.insert(out, part); end
	return out;
end

local function errorText(msg)
	UIErrorsFrame:AddMessage(msg, 1.0, 0.1, 0.1, 1.0);
end

local function infoText(msg)
	UIErrorsFrame:AddMessage(msg, 1.0, 1.0, 0.0, 1.0);
end

-- ---------------------------------------------------------------- the window

local frame = CreateFrame("Frame", "MoongladeSavesFrame", UIParent);
frame:SetFrameStrata("FULLSCREEN_DIALOG");
frame:SetToplevel(true);
frame:SetWidth(460);
frame:SetHeight(400);
frame:SetPoint("CENTER");
frame:SetBackdrop({ bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
                    edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
                    tile = true, tileSize = 32, edgeSize = 32, insets = { left = 11, right = 12, top = 12, bottom = 11 } });
frame:EnableMouse(true);
frame:Hide();
table.insert(UISpecialFrames, "MoongladeSavesFrame");     -- Escape closes it

frame.title = frame:CreateFontString(nil, "ARTWORK", "GameFontNormalLarge");
frame.title:SetPoint("TOP", 0, -18);
frame.title:SetText(TEXT.title);

frame.status = frame:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall");
frame.status:SetPoint("BOTTOM", 0, 82);
frame.status:SetWidth(420);

local function rowText(r)
	return r.label, (KIND_TEXT[r.kind] or r.kind) .. "  \194\183  " .. r.when .. "  \194\183  Level " .. r.level
		.. (r.zone ~= "" and ("  \194\183  " .. r.zone) or "");
end

local buttons = {};
for i = 1, ROWS do
	local b = CreateFrame("Button", "MoongladeSavesRow" .. i, frame);
	b:SetWidth(420);
	b:SetHeight(26);
	b:SetPoint("TOPLEFT", 20, -44 - (i - 1) * 27);
	b:SetHighlightTexture("Interface\\QuestFrame\\UI-QuestTitleHighlight", "ADD");
	b.sel = b:CreateTexture(nil, "BACKGROUND");
	b.sel:SetAllPoints(b);
	b.sel:SetTexture(1, 0.82, 0, 0.25);
	b.sel:Hide();
	b.label = b:CreateFontString(nil, "ARTWORK", "GameFontNormal");
	b.label:SetPoint("TOPLEFT", 4, -1);
	b.label:SetWidth(412);
	b.label:SetJustifyH("LEFT");
	b.info = b:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall");
	b.info:SetPoint("BOTTOMLEFT", 4, 1);
	b.info:SetWidth(412);
	b.info:SetJustifyH("LEFT");
	b:SetScript("OnClick", function(self) S.selected = self.index; MoongladeSaves_Render(); end);
	b:SetScript("OnDoubleClick", function(self) S.selected = self.index; MoongladeSaves_Load(); end);
	buttons[i] = b;
end

local function makeButton(name, text, width, onClick)
	local b = CreateFrame("Button", name, frame, "UIPanelButtonTemplate");
	b:SetWidth(width);
	b:SetHeight(22);
	b:SetText(text);
	b:SetScript("OnClick", onClick);
	return b;
end

local nameBox = CreateFrame("EditBox", "MoongladeSavesName", frame, "InputBoxTemplate");
nameBox:SetWidth(250);
nameBox:SetHeight(20);
nameBox:SetPoint("BOTTOMLEFT", 28, 50);
nameBox:SetAutoFocus(false);
nameBox:SetMaxLetters(48);
nameBox:SetScript("OnEnterPressed", function() MoongladeSaves_Save(); end);
nameBox:SetScript("OnEscapePressed", function(self) self:ClearFocus(); end);
nameBox.hint = frame:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall");
nameBox.hint:SetPoint("BOTTOMLEFT", nameBox, "TOPLEFT", 0, 2);
nameBox.hint:SetText(TEXT.name_hint);

local saveButton = makeButton("MoongladeSavesSave", TEXT.save, 110, function() MoongladeSaves_Save(); end);
saveButton:SetPoint("LEFT", nameBox, "RIGHT", 16, 0);
local loadButton = makeButton("MoongladeSavesLoad", TEXT.load, 100, function() MoongladeSaves_Load(); end);
loadButton:SetPoint("BOTTOMLEFT", 20, 18);
local deleteButton = makeButton("MoongladeSavesDelete", TEXT.delete, 100, function() MoongladeSaves_Delete(); end);
deleteButton:SetPoint("LEFT", loadButton, "RIGHT", 8, 0);
local prevButton = makeButton("MoongladeSavesPrev", "<", 30, function() S.page = math.max(1, S.page - 1); MoongladeSaves_Render(); end);
prevButton:SetPoint("LEFT", deleteButton, "RIGHT", 24, 0);
local nextButton = makeButton("MoongladeSavesNext", ">", 30, function() S.page = S.page + 1; MoongladeSaves_Render(); end);
nextButton:SetPoint("LEFT", prevButton, "RIGHT", 4, 0);
local closeButton = makeButton("MoongladeSavesClose", TEXT.close, 100, function() frame:Hide(); end);
closeButton:SetPoint("BOTTOMRIGHT", -20, 18);

function MoongladeSaves_Render()
	local n = #S.rows;
	local pages = math.max(1, math.ceil(n / ROWS));
	if ( S.page > pages ) then S.page = pages; end
	for i = 1, ROWS do
		local k = (S.page - 1) * ROWS + i;
		local r, b = S.rows[k], buttons[i];
		if ( r ) then
			local label, info = rowText(r);
			b.index = k;
			b.label:SetText(label);
			b.info:SetText(info);
			if ( S.selected == k ) then b.sel:Show(); else b.sel:Hide(); end
			b:Show();
		else
			b:Hide();
		end
	end
	if ( S.listing ) then frame.status:SetText(TEXT.loading_list);
	elseif ( n == 0 ) then frame.status:SetText(TEXT.empty);
	elseif ( frame.status.sticky ) then frame.status:SetText(frame.status.sticky);
	else frame.status:SetText(""); end
	local sel = S.selected and S.rows[S.selected];
	if ( sel ) then loadButton:Enable(); else loadButton:Disable(); end
	if ( sel and sel.kind == "manual" ) then deleteButton:Enable(); else deleteButton:Disable(); end
	if ( S.page > 1 ) then prevButton:Enable(); else prevButton:Disable(); end
	if ( S.page < pages ) then nextButton:Enable(); else nextButton:Disable(); end
end

function MoongladeSaves_Refresh()
	S.listing, S.incoming, S.selected, S.page = true, {}, nil, 1;
	frame.status.sticky = nil;
	MoongladeSaves_Render();
	send("LIST");
end

function MoongladeSaves_Save()
	local name = gsub(nameBox:GetText() or "", "[\t|]", " ");
	nameBox:ClearFocus();
	send("SAVE manual " .. name);
end

function MoongladeSaves_Load()
	local r = S.selected and S.rows[S.selected];
	if ( not r ) then return; end
	StaticPopup_Show("MOONGLADE_SAVES_LOAD", format(TEXT.confirm_load, r.label, UnitName("player") or "", r.when), nil,
		{ gen = S.gen, index = S.selected });
end

function MoongladeSaves_Delete()
	local r = S.selected and S.rows[S.selected];
	if ( not r ) then return; end
	if ( r.kind ~= "manual" ) then errorText(TEXT.only_own); return; end
	StaticPopup_Show("MOONGLADE_SAVES_DELETE", format(TEXT.confirm_delete, r.label), nil, { gen = S.gen, index = S.selected });
end

function MoongladeSaves_QuickSave() send("QUICK"); end
function MoongladeSaves_QuickLoad() send("QLOAD"); end

-- the dialogs' text is formatted here and handed in whole ("%s"): StaticPopup only formats two arguments
StaticPopupDialogs["MOONGLADE_SAVES_LOAD"] = {
	text = "%s", button1 = TEXT.load, button2 = CANCEL,
	OnAccept = function(self, data)
		if ( data and data.gen ) then
			frame.status.sticky = TEXT.loading;
			MoongladeSaves_Render();
			send("LOAD " .. data.gen .. " " .. data.index);
		end
	end,
	timeout = 0, whileDead = 1, hideOnEscape = 1,
};
StaticPopupDialogs["MOONGLADE_SAVES_DELETE"] = {
	text = "%s", button1 = TEXT.delete, button2 = CANCEL,
	OnAccept = function(self, data)
		if ( data and data.gen ) then send("DELETE " .. data.gen .. " " .. data.index); end
	end,
	timeout = 0, whileDead = 1, hideOnEscape = 1, showAlert = 1,
};

-- ---------------------------------------------------------------- the server's answers

local replies = CreateFrame("Frame");
replies:RegisterEvent("CHAT_MSG_ADDON");
replies:SetScript("OnEvent", function(self, event, prefix, message)
	if ( prefix ~= PREFIX ) then return; end
	if ( hook.on and #hook.replies < 40 ) then table.insert(hook.replies, strsub(message, 1, 60)); end
	local f = split(message);
	local kind = f[1];
	if ( kind == "E" ) then
		S.incoming = S.incoming or {};
		table.insert(S.incoming, { gen = f[2], index = tonumber(f[3]), kind = f[4] or "", when = f[5] or "",
		                           level = f[6] or "", zone = f[7] or "", label = f[8] or "" });
	elseif ( kind == "END" ) then
		S.rows, S.gen, S.listing, S.incoming = S.incoming or {}, f[2], false, nil;
		if ( hook.on ) then hook.list = { gen = f[2], count = tonumber(f[3]), kinds = {}, labels = {} };
			for i, r in ipairs(S.rows) do
				if ( i <= 5 ) then table.insert(hook.list.kinds, r.kind); table.insert(hook.list.labels, r.label); end
			end
		end
		MoongladeSaves_Render();
	elseif ( kind == "OK" ) then
		local verb, text = f[2], f[3] or "";
		if ( verb == "SAVE" ) then infoText(TEXT.saved);
		elseif ( verb == "QUICK" ) then infoText(TEXT.quicksaved);
		elseif ( verb == "DELETE" ) then infoText(TEXT.deleted); end
		if ( frame:IsShown() and verb ~= "LOAD" and verb ~= "QLOAD" ) then MoongladeSaves_Refresh(); end
	elseif ( kind == "NO" ) then
		local text = f[3] or "";
		errorText(text);
		if ( frame:IsShown() ) then frame.status.sticky = text; MoongladeSaves_Render(); end
	end
end);

-- ---------------------------------------------------------------- the game menu's Saves button

local menuButton = CreateFrame("Button", "GameMenuButtonMoongladeSaves", GameMenuFrame, "GameMenuButtonTemplate");
menuButton:SetText(TEXT.title);
local function placeMenuButton()
	local above = (GameMenuButtonRatings and GameMenuButtonRatings:IsShown()) and GameMenuButtonRatings or GameMenuButtonMacros;
	menuButton:ClearAllPoints();
	menuButton:SetPoint("TOP", above, "BOTTOM", 0, -1);
	GameMenuButtonLogout:ClearAllPoints();
	GameMenuButtonLogout:SetPoint("TOP", menuButton, "BOTTOM", 0, -1);
end
placeMenuButton();
GameMenuFrame:SetHeight(GameMenuFrame:GetHeight() + menuButton:GetHeight() + 1);
GameMenuButtonLogout:HookScript("OnShow", placeMenuButton);      -- after its own OnShow re-anchors to Ratings
menuButton:SetScript("OnClick", function()
	PlaySound("igMainMenuOption");
	frame:Show();                     -- before the menu hides, so the world stays paused (Moonglade.lua askPause)
	HideUIPanel(GameMenuFrame);
end);

frame:SetScript("OnShow", function()
	nameBox:SetText("");
	MoongladeSaves_Refresh();
	if ( MoongladePause_Ask ) then MoongladePause_Ask(true); end
end);
frame:SetScript("OnHide", function()
	StaticPopup_Hide("MOONGLADE_SAVES_LOAD");
	StaticPopup_Hide("MOONGLADE_SAVES_DELETE");
	if ( MoongladePause_Ask and not GameMenuFrame:IsShown() ) then MoongladePause_Ask(false); end
end);

-- ---------------------------------------------------------------- default keys, once

-- Once the client has its key bindings (VARIABLES_LOADED can come before them: GetCurrentBindingSet() isn't 1 or 2
-- yet, and SaveBindings then raises "Usage: SaveBindings(1||2)"), so this waits for a valid binding set at
-- PLAYER_ENTERING_WORLD or UPDATE_BINDINGS, and marks it done only after the save.
local keys = CreateFrame("Frame");
keys:RegisterEvent("PLAYER_ENTERING_WORLD");
keys:RegisterEvent("UPDATE_BINDINGS");
MoongladeSavesKeys = { set = nil, changed = false };
keys:SetScript("OnEvent", function()
	MoongladeSettings = type(MoongladeSettings) == "table" and MoongladeSettings or {};
	if ( MoongladeSettings.keysSet ) then MoongladeSavesKeys.set = "already"; return; end
	local set = GetCurrentBindingSet();
	if ( set ~= 1 and set ~= 2 ) then return; end          -- not loaded yet: the next event tries again
	local changed = false;
	for action, key in pairs(DEFAULT_KEYS) do
		if ( not GetBindingKey(action) and (GetBindingAction(key) or "") == "" ) then
			SetBinding(key, action);
			changed = true;
		end
	end
	if ( changed ) then SaveBindings(set); end
	MoongladeSettings.keysSet = 1;
	MoongladeSavesKeys.set, MoongladeSavesKeys.changed = set, changed;
	keys:UnregisterAllEvents();
end);

-- ---------------------------------------------------------------- test hook (harness checks only)

local tf = CreateFrame("Frame");
tf:RegisterEvent("ADDON_LOADED");
tf:RegisterEvent("PLAYER_ENTERING_WORLD");
tf:RegisterEvent("PLAYER_LOGOUT");
tf:SetScript("OnEvent", function(self, event, arg1)
	if ( event == "ADDON_LOADED" and arg1 == "Moonglade" ) then
		hook.on = type(MoongladeTest) == "table" and MoongladeTest.hook == "saves";
		if ( hook.on ) then
			hook.errors, hook.errorMsgs = 0, {};
			local previous = geterrorhandler();
			seterrorhandler(function(msg, ...)
				hook.errors = hook.errors + 1;
				if ( #hook.errorMsgs < 5 ) then table.insert(hook.errorMsgs, tostring(msg)); end
				if ( previous ) then return previous(msg, ...); end
			end);
		end
		-- the second world entry of the same test run (its report is already there) only quits the game
		hook.again = hook.on and type(MoongladeTest.saves) == "table";
	elseif ( event == "PLAYER_ENTERING_WORLD" and hook.on and not hook.phase ) then
		hook.phase = "settle";
	elseif ( event == "PLAYER_LOGOUT" and hook.on and hook.again ) then
		MoongladeTest.again = { phase = hook.phase, errors = hook.errors, error_msgs = hook.errorMsgs,
		                        keys = MoongladeSavesKeys.set, quicksave_key = GetBindingKey("MOONGLADE_QUICKSAVE"),
		                        quickload_key = GetBindingKey("MOONGLADE_QUICKLOAD") };
	elseif ( event == "PLAYER_LOGOUT" and hook.on ) then
		MoongladeTest.saves = { phase = hook.phase, sent = hook.sent, replies = hook.replies, list = hook.list,
		                        quicksave_key = GetBindingKey("MOONGLADE_QUICKSAVE"),
		                        quickload_key = GetBindingKey("MOONGLADE_QUICKLOAD"),
		                        menu_button = GameMenuButtonMoongladeSaves:GetText(), errors = hook.errors,
		                        error_msgs = hook.errorMsgs, keys = MoongladeSavesKeys.set,
		                        keys_changed = MoongladeSavesKeys.changed and 1 or 0 };
	end
end);

local function has(prefix)
	for _, r in ipairs(hook.replies) do if ( strsub(r, 1, #prefix) == prefix ) then return true; end end
	return false;
end

tf:SetScript("OnUpdate", function(self, elapsed)
	if ( not hook.on or not hook.phase or hook.phase == "done" ) then return; end
	hook.clock = hook.clock + elapsed;
	local up = UIParent:IsVisible() and not InCinematic();
	if ( InCinematic() ) then StopCinematic(); end
	if ( hook.phase == "settle" ) then
		if ( not up ) then hook.readyAt = nil; return; end
		hook.readyAt = hook.readyAt or hook.clock;
		if ( hook.again and hook.clock - hook.readyAt >= 2 ) then
			hook.phase = "done";
			Quit();
		elseif ( hook.clock - hook.readyAt >= 3 ) then
			hook.phase = "save";
			GameMenuButtonMoongladeSaves:Click();       -- (the menu needn't be shown for the button's script)
			nameBox:SetText(MoongladeTest.name or "Test save");
			MoongladeSaves_Save();
			hook.t = hook.clock;
		end
	elseif ( hook.phase == "save" and (has("OK\tSAVE") or hook.clock - hook.t > 10) ) then
		hook.phase = "quick";
		MoongladeSaves_QuickSave();
		hook.t = hook.clock;
	elseif ( hook.phase == "quick" and (has("OK\tQUICK") or hook.clock - hook.t > 10) ) then
		hook.phase = "list";
		hook.list = nil;
		MoongladeSaves_Refresh();
		hook.t = hook.clock;
	elseif ( hook.phase == "list" and (hook.list or hook.clock - hook.t > 10) ) then
		hook.phase = "shot";
		Screenshot();                      -- the window with its list, for a human look (harness/out/saves-gui)
		hook.t = hook.clock;
	elseif ( hook.phase == "shot" and hook.clock - hook.t > 1 ) then
		hook.phase = "done";
		frame:Hide();
		Logout();
	end
end);
