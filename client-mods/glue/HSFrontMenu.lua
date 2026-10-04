-- Hearthstone front menu (openspec/changes/in-client-login, patch 0002).
--
-- The stock 3.3.5a login screen (animated backdrop, music, era logo) turned into a single-player title menu
-- (design.md "Menu layout"): one centred column over a subtle dark panel.
--   Main:     Continue (preselected; under it the last-played character, "Arthas · Level 23 Paladin", computed by the
--             launcher; clicking it presses Continue), Profiles, Settings, Extras, Quit
--   Profiles: every profile, the current one marked; choosing one makes it Continue's target; Back
--   Settings: Game options (the stock option panels), Login screen < name > (backdrop, logo and music switch live), Back
--   Extras:   Cinematics, Credits, About (read-only: game and Hearthstone versions, current world), Back
-- Keyboard: Up/Down move, Enter selects, Left/Right change the selector, Escape goes back a level (on the main menu
-- it moves to Quit). The mouse works too (hovering highlights a button the stock way but doesn't move the
-- keyboard selection, so a resting cursor can't steal Continue's preselection). Every stock login control (edit boxes, corner buttons) is hidden.
-- No typing: the logins come from HSProfiles.lua, which the launcher generates at each start inside the play client
-- (never committed, never in client/) and removes when the game closes.
-- Where the choices live: the glue screens only know the client's own console variables (GetCVar on an unknown name
-- errors and RegisterCVar doesn't exist there), so the last profile is the stock saved account name (accountName) and
-- the backdrop is kept in the stock saved account list (accountList, "HSBD<key>|"; it only holds Battle.net
-- game-account lists, which an offline client never gets). The client writes both to WTF/Config.wtf when it saves
-- its settings, which it does on entering the world (EnterWorld @0x004dc510 -> 0x004d9bd0 -> 0x00767100).
-- A choice made and then quit from the login screen without playing is not saved (ADR-0009 appendix A notes).
-- Model, logo and sound-kit names come from the installed client (docs/research/login-screen-spike.md, Q2):
-- SoundEntries.dbc rows GS_LichKing / GS_BurningCrusade / GS_Retail.

HS_BACKDROPS = {
	{ key = "WOTLK", label = "Lich King",
	  model = "Interface\\Glues\\Models\\UI_MainMenu_Northrend\\UI_MainMenu_Northrend.m2",
	  logo = "Interface\\Glues\\Common\\Glues-WoW-WotLKLogo", music = "GS_LichKing" },
	{ key = "TBC", label = "Burning Crusade",
	  model = "Interface\\Glues\\Models\\UI_MainMenu_BurningCrusade\\UI_MainMenu_BurningCrusade.m2",
	  logo = "Interface\\Glues\\Common\\Glues-WoW-BCLogo", music = "GS_BurningCrusade" },
	{ key = "VANILLA", label = "Classic",
	  model = "Interface\\Glues\\Models\\UI_MainMenu\\UI_MainMenu.m2",
	  logo = "Interface\\Glues\\Common\\Glues-WoW-Logo", music = "GS_Retail" },
};

HS_PROFILES = HS_PROFILES or {};

local state = { shown = false, backdrop = nil, current = nil, menu = "main", sel = 1, items = nil,
                testClock = 0, testDone = {}, quitAt = nil, testLog = "" };
HSFrontMenuState = state;

-- ---------------------------------------------------------------- test hook marks

-- Test hook: only when the generated HSProfiles.lua carries HS_TEST (harness checks). Records `event` in
-- HSFrontMenuState.testLog (in memory; checks read the marker squares and server/client logs instead).
function HSFrontMenu_Mark(event)
	if ( not HS_TEST ) then return; end
	if ( state.testLog ~= "" ) then state.testLog = state.testLog .. ";"; end
	state.testLog = state.testLog .. event;
end

-- ---------------------------------------------------------------- backdrop

local function backdropByKey(key)
	for i, b in ipairs(HS_BACKDROPS) do
		if ( b.key == key ) then return b, i; end
	end
	return nil;
end

function HSFrontMenu_StoredBackdrop()
	local key = string.match(GetSavedAccountList() or "", "HSBD(%u+)");
	return backdropByKey(key) or HS_BACKDROPS[1];
end

-- Apply a backdrop: login model, logo, and glue music (CurrentGlueMusic also plays on the character screens).
function HSFrontMenu_ApplyBackdrop(b, remember)
	state.backdrop = b;
	CurrentGlueMusic = b.music;
	AccountLogin:SetModel(b.model);
	AccountLogin:SetCamera(0);
	AccountLogin:SetSequence(0);
	AccountLoginLogo:SetTexture(b.logo);
	if ( state.shown ) then
		if ( StopGlueMusic ) then StopGlueMusic(); end
		PlayGlueMusic(CurrentGlueMusic);
	end
	if ( remember ) then
		SetSavedAccountList("HSBD" .. b.key .. "|");
	end
	HSFrontMenu_Mark("backdrop:" .. b.key .. ":" .. b.model .. ":" .. b.music);
end


-- ---------------------------------------------------------------- profiles and login

local function profileByAccount(account)
	if ( not account or account == "" ) then return nil; end
	for i, p in ipairs(HS_PROFILES) do
		if ( strupper(p.account) == strupper(account) ) then return p, i; end
	end
	return nil;
end

local function pname(p) return p.name or p.account; end

-- The current profile (Continue's target): chosen in Profiles this session, else the last one used (saved account
-- name), else the launcher's default, else the first.
function HSFrontMenu_CurrentProfile()
	return state.current or profileByAccount(GetSavedAccountName()) or profileByAccount(HS_PROFILE_DEFAULT)
		or HS_PROFILES[1];
end

function HSFrontMenu_Login(profile, how)
	if ( not profile ) then return; end
	HSFrontMenu_Mark("login:" .. profile.account .. ":" .. (how or "?"));
	PlaySound("gsLogin");
	SetSavedAccountName(profile.account);
	DefaultServerLogin(profile.account, profile.password);
end

function HSFrontMenu_Continue(how)
	HSFrontMenu_Login(HSFrontMenu_CurrentProfile(), how or "continue");
end

function HSFrontMenu_CycleBackdrop(dir)
	local _, i = backdropByKey((state.backdrop or HS_BACKDROPS[1]).key);
	i = (((i or 1) - 1 + dir) % #HS_BACKDROPS) + 1;
	PlaySound("gsTitleOptionOK");
	HSFrontMenu_ApplyBackdrop(HS_BACKDROPS[i], true);
	HSFrontMenu_Render();
end

-- ---------------------------------------------------------------- the menus

local MENUS = {};

MENUS.main = { back = nil, items = function()
	local cur = HSFrontMenu_CurrentProfile();
	return {
		{ text = "Continue", context = cur and (cur.context or pname(cur)) or "No profile: start from the Hearthstone app",
		  disabled = not cur, action = function() HSFrontMenu_Continue("continue"); end },
		{ text = "Profiles", action = function() HSFrontMenu_Open("profiles"); end },
		{ text = "Settings", action = function() HSFrontMenu_Open("settings"); end },
		{ text = "Extras", action = function() HSFrontMenu_Open("extras"); end },
		{ text = "Quit", action = function() AccountLogin_Exit(); end },
	};
end };

MENUS.profiles = { title = "Profiles", back = "main", items = function()
	local list, cur = {}, HSFrontMenu_CurrentProfile();
	for i = 1, math.min(#HS_PROFILES, 5) do
		local p = HS_PROFILES[i];
		table.insert(list, { text = pname(p) .. ((p == cur) and "  (current)" or ""), action = function()
			state.current = p;
			PlaySound("gsTitleOptionOK");
			HSFrontMenu_Mark("profile:" .. p.account);
			HSFrontMenu_Open("main");
		end });
	end
	table.insert(list, { text = "Back", back = true });
	return list;
end };

MENUS.settings = { title = "Settings", back = "main", items = function()
	return {
		{ text = "Game options", action = function()
			PlaySound("gsTitleOptions");
			OptionsSelectFrame:Show();
		end },
		{ text = (state.backdrop or HS_BACKDROPS[1]).label, selector = true, label = "Login screen:",
		  action = function() HSFrontMenu_CycleBackdrop(1); end },
		{ text = "Back", back = true },
	};
end };

MENUS.extras = { title = "Extras", back = "main", items = function()
	return {
		{ text = "Cinematics", action = function() AccountLogin_Cinematics(); end },
		{ text = "Credits", action = function() AccountLogin_Credits(); end },
		{ text = "About", action = function() HSFrontMenu_Open("about"); end },
		{ text = "Back", back = true },
	};
end };

-- About: read-only. Game version from the client itself (GetBuildInfo); Hearthstone version, commit, pins and the
-- world the launcher started come from HS_ABOUT in the generated HSProfiles.lua (no credentials in it).
function HSFrontMenu_AboutText()
	local _, buildType, version, build, date = GetBuildInfo();
	local a = HS_ABOUT or {};
	local gold, white = "|cffffd100", "|cffffffff";
	local function line(k, v) return gold .. k .. "|r  " .. white .. (v or "unknown") .. "|r"; end
	local t = {
		line("Game:", "World of Warcraft " .. (version or "?") .. " (" .. (build or "?") .. ")"),
		"|cff9d9d9d" .. (date or "") .. "|r",
		"",
		line("Hearthstone:", (a.version or "?") .. " (" .. (a.commit or "?") .. ")"),
		line("Server core:", a.core),
		line("Playerbots:", a.playerbots),
		"",
		line("Vanilla mode:", a.vanilla),
		line("World size:", a.world),
		"",
		BLIZZ_DISCLAIMER or "",
	};
	return table.concat(t, "\n");
end

MENUS.about = { title = "About", back = "extras", text = HSFrontMenu_AboutText, items = function()
	return { { text = "Back", back = true } };
end };

local SLOT_H, SLOT_GAP = 40, 6;

function HSFrontMenu_Render()
	local menu = MENUS[state.menu];
	local items = menu.items();
	state.items = items;
	if ( state.sel > #items ) then state.sel = #items; end
	local top = menu.title and -42 or -18;
	if ( menu.title ) then HSMenuTitle:SetText(menu.title); HSMenuTitle:Show(); else HSMenuTitle:Hide(); end
	HSContinueContext:Hide();
	HSMenuSelLabel:Hide();
	HSArrowLeft:Hide();
	HSArrowRight:Hide();
	local y = top;
	if ( menu.text ) then
		HSMenuText:SetText(menu.text());
		HSMenuText:ClearAllPoints();
		HSMenuText:SetPoint("TOP", HSMenuPanel, "TOP", 0, y);
		HSMenuText:Show();
		y = y - HSMenuText:GetHeight() - 14;
	else
		HSMenuText:Hide();
	end
	for i = 1, 6 do
		local slot = _G["HSMenuSlot" .. i];
		local it = items[i];
		if ( it ) then
			slot:ClearAllPoints();
			slot:SetPoint("TOP", HSMenuPanel, "TOP", it.selector and 48 or 0, y);   -- centres label+arrows+button
			slot:SetText(it.text);
			if ( it.disabled ) then slot:Disable(); else slot:Enable(); end
			if ( i == state.sel ) then slot:LockHighlight(); else slot:UnlockHighlight(); end
			slot:Show();
			y = y - SLOT_H - SLOT_GAP;
			if ( it.context ) then          -- tight under its button, then a clearly larger gap before the next
				HSContinueContext:ClearAllPoints();
				HSContinueContext:SetPoint("TOP", slot, "BOTTOM", 0, 3);
				HSContinueContextText:SetText(it.context);
				HSContinueContext:Show();
				y = y - 14 - 10;
			end
			if ( it.selector ) then
				slot:SetWidth(170);
				HSArrowLeft:ClearAllPoints();
				HSArrowLeft:SetPoint("RIGHT", slot, "LEFT", 2, 0);
				HSArrowRight:ClearAllPoints();
				HSArrowRight:SetPoint("LEFT", slot, "RIGHT", -2, 0);
				HSArrowLeft:Show();
				HSArrowRight:Show();
				HSMenuSelLabel:SetText(it.label or "");
				HSMenuSelLabel:ClearAllPoints();
				HSMenuSelLabel:SetPoint("RIGHT", HSArrowLeft, "LEFT", -2, 0);
				HSMenuSelLabel:Show();
			else
				slot:SetWidth(240);
			end
		else
			slot:Hide();
		end
	end
	HSFrontMenu_ContinueBrightness();
	HSMenuPanel:SetHeight(-y + 10);
	HSMenuPanel:Show();
end

-- The context line brightens with Continue: when Continue is the selection or the mouse is over either of them.
function HSFrontMenu_ContinueBrightness()
	if ( state.menu == "main" and (state.sel == 1 or state.continueHover) ) then
		HSContinueContextText:SetTextColor(1.0, 0.95, 0.75);
	else
		HSContinueContextText:SetTextColor(0.6, 0.5, 0.3);
	end
	if ( state.menu == "main" ) then
		if ( state.continueHover or state.sel == 1 ) then HSMenuSlot1:LockHighlight(); else HSMenuSlot1:UnlockHighlight(); end
	end
end

function HSFrontMenu_ContinueHover(on)
	state.continueHover = on;
	HSFrontMenu_ContinueBrightness();
end

function HSFrontMenu_Open(name, sel)
	state.menu = name;
	state.sel = sel or 1;
	HSFrontMenu_Mark("open:" .. name);
	HSFrontMenu_Render();
end

function HSFrontMenu_Activate(i)
	local it = state.items and state.items[i];
	if ( not it or it.disabled ) then return; end
	state.sel = i;
	if ( it.back ) then
		HSFrontMenu_Back();
	else
		if ( not it.selector ) then PlaySound("gsTitleOptionOK"); end
		it.action();
	end
end

function HSFrontMenu_Back()
	local menu = MENUS[state.menu];
	PlaySound("gsTitleOptionExit");
	if ( menu.back ) then
		HSFrontMenu_Open(menu.back);
	else
		state.sel = #state.items;          -- Escape on the main menu moves to Quit
		HSFrontMenu_Render();
	end
end

function HSFrontMenu_Move(dir)
	local n = #state.items;
	local i = state.sel;
	repeat
		i = ((i - 1 + dir) % n) + 1;
	until ( not state.items[i].disabled or i == state.sel );
	state.sel = i;
	HSFrontMenu_Render();
end

function HSFrontMenu_SlotClicked(self)
	HSFrontMenu_Activate(self:GetID());
end


-- ---------------------------------------------------------------- wrapping the stock login screen

local stockHide = { "AccountLoginAccountEdit", "AccountLoginPasswordEdit", "AccountLoginTokenEdit",
	"AccountLoginLoginButton", "AccountLoginSaveAccountName", "AccountLoginSaveAccountNameText",
	"AccountLoginDropDown", "AccountLoginManageAccountButton", "AccountLoginCommunityButton",
	"AccountLoginShowLauncher", "AccountLoginTOSButton", "AccountLoginUpgradeAccountButton",
	"AccountLoginRealmName", "OptionsButton", "AccountLoginCinematicsButton", "AccountLoginCreditsButton",
	"AccountLoginExitButton" };

local HS_Stock_OnShow = AccountLogin_OnShow;
local HS_Stock_OnKeyDown = AccountLogin_OnKeyDown;

function AccountLogin_OnShow(self)
	local b = state.backdrop or HSFrontMenu_StoredBackdrop();
	CurrentGlueMusic = b.music;              -- the stock OnShow starts CurrentGlueMusic
	state.shown = false;
	HS_Stock_OnShow(self);
	for _, name in ipairs(stockHide) do
		local f = _G[name];
		if ( f ) then f:Hide(); end
	end
	-- nothing on the bottom edge: the Blizzard logo, copyright and version text (AccountLoginUI's own regions,
	-- except the era logo) and the ESRB notice (WorldOfWarcraftRating, placed by GlueLocalization.lua)
	if ( AccountLoginUI.GetRegions ) then
		for _, r in ipairs({ AccountLoginUI:GetRegions() }) do
			if ( r ~= AccountLoginLogo ) then r:Hide(); end
		end
	end
	if ( WorldOfWarcraftRating ) then WorldOfWarcraftRating:Hide(); end
	-- the stock OnHide clears the saved account list (our backdrop) unless "remember account name" is checked
	AccountLoginSaveAccountName:SetChecked(1);
	HSFrontMenu_ApplyBackdrop(b, false);
	state.shown = true;
	HSFrontMenu_Open("main", 1);
	local cur = HSFrontMenu_CurrentProfile();
	HSFrontMenu_Mark("menu:" .. #HS_PROFILES .. ":continue=" .. (cur and cur.account or "-"));
end

function AccountLogin_OnKeyDown(key)
	if ( key == "PRINTSCREEN" ) then
		Screenshot();
		return;
	end
	if ( not state.items or GlueDialog:IsShown() or TOSFrame:IsShown() or ConnectionHelpFrame:IsShown() ) then
		if ( key ~= "ENTER" ) then HS_Stock_OnKeyDown(key); end
		return;
	end
	if ( key == "UP" ) then
		HSFrontMenu_Move(-1);
	elseif ( key == "DOWN" or key == "TAB" ) then
		HSFrontMenu_Move(1);
	elseif ( key == "ENTER" ) then
		HSFrontMenu_Activate(state.sel);
	elseif ( key == "ESCAPE" ) then
		HSFrontMenu_Back();
	elseif ( (key == "LEFT" or key == "RIGHT") and state.items[state.sel].selector ) then
		HSFrontMenu_CycleBackdrop(key == "LEFT" and -1 or 1);
	end
end

-- The stock Login function reads the (hidden) edit boxes; route it to Continue instead.
function AccountLogin_Login()
	HSFrontMenu_Continue("login");
end

-- ---------------------------------------------------------------- test hook (harness checks only)

function HSFrontMenu_OnEvent(self, event)
	if ( event == "CHARACTER_LIST_UPDATE" and HS_TEST ) then
		local names = "";
		local n = GetNumCharacters();
		for i = 1, n do
			local name = GetCharacterInfo(i);
			names = names .. (i > 1 and "," or "") .. (name or "?");
		end
		HSFrontMenu_Mark("charlist:" .. n .. ":" .. names);
		if ( HS_TEST.quit_after_charlist and not state.quitAt ) then
			state.quitAt = state.testClock + HS_TEST.quit_after_charlist;
		end
		if ( HS_TEST.enter_world_after_charlist and n > 0 and not state.enterAt ) then
			state.enterAt = state.testClock + HS_TEST.enter_world_after_charlist;
		end
	end
end

-- HS_TEST.script = { {t = seconds after the login screen first shows, act = "continue" | "shot" | "quit" |
-- "backdrop:TBC" | "menu:settings" | "key:DOWN" | ...}, ... }; HS_TEST.quit_after_charlist = seconds. "continue"
-- clicks the Continue button once (the same handler a mouse click runs); "key:X" calls the login screen's own
-- key handler; nothing here injects keystrokes into the client.
-- Marker colours (HS_TEST.marker): backdrop and music squares red / green / blue for the 1st / 2nd / 3rd entry of
-- HS_BACKDROPS (by model requested and by CurrentGlueMusic, independently); menu square white while the main menu is
-- up, magenta otherwise. harness/hslib/checks/login_checks.py decodes them from the screenshots.
local MARK_RGB = { {1, 0, 0}, {0, 1, 0}, {0, 0, 1} };

function HSFrontMenu_UpdateMarker()
	local bi, mi = 0, 0;
	for i, b in ipairs(HS_BACKDROPS) do
		if ( state.backdrop == b ) then bi = i; end
		if ( CurrentGlueMusic == b.music ) then mi = i; end
	end
	local function paint(tex, i)
		local c = MARK_RGB[i] or {0, 0, 0};
		tex:SetTexture(c[1], c[2], c[3], 1);
	end
	paint(HSTestMarkerBackdrop, bi);
	paint(HSTestMarkerMusic, mi);
	if ( state.menu == "main" and HSMenuPanel:IsVisible() ) then
		HSTestMarkerMenu:SetTexture(1, 1, 1, 1);
	else
		HSTestMarkerMenu:SetTexture(1, 0, 1, 1);
	end
	HSTestMarker:Show();
end

function HSFrontMenu_TestUpdate(self, elapsed)
	if ( not HS_TEST or not state.shown ) then return; end
	state.testClock = state.testClock + elapsed;
	if ( HS_TEST.marker ) then HSFrontMenu_UpdateMarker(); end
	if ( HS_TEST.script ) then
		for i, step in ipairs(HS_TEST.script) do
			if ( not state.testDone[i] and state.testClock >= step.t ) then
				state.testDone[i] = true;
				local act = step.act;
				if ( act == "continue" ) then
					HSFrontMenu_Mark("press:continue");
					HSFrontMenu_Open("main", 1);
					HSMenuSlot1:Click();
				elseif ( strsub(act, 1, 5) == "menu:" ) then
					HSFrontMenu_Open(strsub(act, 6));
				elseif ( strsub(act, 1, 4) == "key:" ) then
					AccountLogin_OnKeyDown(strsub(act, 5));
				elseif ( act == "shot" ) then
					Screenshot();
					HSFrontMenu_Mark("shot");
				elseif ( act == "quit" ) then
					HSFrontMenu_Mark("quit");
					QuitGame();
				elseif ( act == "enterworld" ) then
					HSFrontMenu_Mark("press:enterworld");
					EnterWorld();
				elseif ( strsub(act, 1, 9) == "backdrop:" ) then
					local b = backdropByKey(strsub(act, 10));
					if ( b ) then HSFrontMenu_ApplyBackdrop(b, true); end
				end
			end
		end
	end
	if ( state.enterAt and state.testClock >= state.enterAt ) then
		state.enterAt = nil;
		HSFrontMenu_Mark("press:enterworld");
		EnterWorld();
	end
	if ( state.quitAt and state.testClock >= state.quitAt ) then
		state.quitAt = nil;
		HSFrontMenu_Mark("quit");
		QuitGame();
	end
end

-- ---------------------------------------------------------------- load time

-- Called from HSMenuPanel's OnLoad (HSFrontMenu.xml).
function HSFrontMenu_Init()
	if ( HS_TEST ) then
		HSFrontMenu_Mark("stored=" .. HSFrontMenu_StoredBackdrop().key);
	end
end

-- Music for the very first PlayGlueMusic (SetGlueScreen / START_GLUE_MUSIC) follows the remembered backdrop.
CurrentGlueMusic = HSFrontMenu_StoredBackdrop().music;
