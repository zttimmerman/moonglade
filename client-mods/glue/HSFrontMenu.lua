-- Moonglade front menu (openspec/changes/in-client-login, patch 0002).
--
-- The stock 3.3.5a login screen (animated backdrop, music, era logo) turned into a single-player title menu
-- (design.md "Menu layout"): one centred column over a subtle dark panel.
--   Main:     Continue (preselected; under it the last-played character, "Arthas · Level 23 Paladin", computed by the
--             launcher; clicking it presses Continue), Profiles, Settings, Extras, Quit
--   Profiles: every profile, the current one marked; choosing one makes it Continue's target; Back
--   Settings: Game options (the stock option panels), Login screen < name > (backdrop, logo and music switch live), Back
--   Extras:   Cinematics, Credits, About (read-only: game and Moonglade versions, current world), Back
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

-- DefaultServerLogin wipes the password string it's given, in place (seamless-experience, measured: a second login
-- with the same profile in one glue session, e.g. Try again or Main Menu then Continue, failed with "Please enter your
-- password."). Lua strings are shared, so the profile's own copy went with it. The password is therefore kept as
-- bytes and a fresh string is built for each login.
function HSFrontMenu_Login(profile, how)
	if ( not profile ) then return; end
	HSFrontMenu_Mark("login:" .. profile.account .. ":" .. (how or "?"));
	PlaySound("gsLogin");
	SetSavedAccountName(profile.account);
	if ( not profile.pwbytes ) then profile.pwbytes = { string.byte(profile.password, 1, -1) }; end
	DefaultServerLogin(profile.account, string.char(unpack(profile.pwbytes)));
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
		{ text = "Continue", context = cur and (cur.context or pname(cur)) or "No profile: start from the Moonglade app",
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

-- About: read-only. Game version from the client itself (GetBuildInfo); Moonglade version, commit, pins and the
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
		line("Moonglade:", (a.version or "?") .. " (" .. (a.commit or "?") .. ")"),
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
	-- seamless-experience: while the loading overlay is up, keys don't reach the menu (Enter would log in again);
	-- Escape is the overlay's "Back to menu" once it's offered. The friendly error: Enter = Try again, Escape = Back.
	if ( HSLoadingOverlay and HSLoadingOverlay:IsShown() ) then
		if ( key == "ESCAPE" and HSLoadingOverlayBack:IsShown() ) then HSSeamless_OverlayBack(); end
		return;
	end
	if ( HSSeamless_ErrorShown and HSSeamless_ErrorShown() ) then
		if ( key == "ENTER" ) then HSSeamless_Retry(); elseif ( key == "ESCAPE" ) then HSSeamless_BackToMenu(); end
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
		-- only after a login in this glue session: back from the world, the glue screens start at character select
		-- again and must not repeat the scripted actions (seamless-experience)
		local loggedIn = HSSeamlessState and HSSeamlessState.logins > 0 and not HSSeamlessState.enteredWorld;
		if ( HS_TEST.quit_after_charlist and not state.quitAt ) then
			state.quitAt = state.testClock + HS_TEST.quit_after_charlist;
		end
		if ( HS_TEST.enter_world_after_charlist and n > 0 and not state.enterAt and loggedIn ) then
			state.enterAt = state.testClock + HS_TEST.enter_world_after_charlist;
		end
		-- front-menu-saves prototype (task 1.1): HS_TEST.charselect_back = steps timed from the first character list
		-- after coming back from the world (a fresh glue session: state.round > 1; or the same one: enteredWorld)
		if ( HS_TEST.charselect_back and n > 0 and not state.cbAt
		     and (state.round > 1 or (HSSeamlessState and HSSeamlessState.enteredWorld)) ) then
			state.cbAt = state.testClock;
		end
		-- HS_TEST.charselect = { {t = seconds after the character list, act = ...}, ... } (seamless-experience)
		if ( HS_TEST.charselect and n > 0 and not state.csAt and loggedIn ) then
			state.csAt = state.testClock;
		-- HS_TEST.charselect_again: steps timed from the character list that follows a "mainmenu" + "continue" from the
		-- first round (seamless-polish: a quick second load)
		elseif ( HS_TEST.charselect_again and n > 0 and state.csAgain and loggedIn ) then
			state.csAgain = nil;
			state.cs2At = state.testClock;
		end
	end
end

-- HS_TEST.script = { {t = seconds after the glue screens load (the login screen shows within a frame or two), act = "continue" | "shot" | "quit" |
-- "backdrop:TBC" | "menu:settings" | "key:DOWN" | "details" | "retry" | ...}, ... }; HS_TEST.quit_after_charlist = seconds;
-- HS_TEST.charselect = steps timed from the character list (HSFrontMenu_TestAct). "continue"
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
	-- Lua errors caught by the test error handler: white = none, red = one or more
	if ( HS_TEST_ERRORS == 0 ) then
		HSTestMarkerErrors:SetTexture(1, 1, 1, 1);
	else
		HSTestMarkerErrors:SetTexture(1, 0, 0, 1);
	end
	-- seamless-experience, second row: green = loading overlay visible, blue = friendly error visible, white = neither
	if ( HSLoadingOverlay and HSLoadingOverlay:IsVisible() ) then
		HSTestMarkerOverlay:SetTexture(0, 1, 0, 1);
	elseif ( HSErrorDialog and HSErrorDialog:IsVisible() ) then
		HSTestMarkerOverlay:SetTexture(0, 0, 1, 1);
	else
		HSTestMarkerOverlay:SetTexture(1, 1, 1, 1);
	end
	-- saves-channel, second row: control request (magenta = pending with our overlay up; then the last result:
	-- green = accepted, red = refused, blue = couldn't reach the launcher; white = none yet) and the stock status
	-- dialog (red = on screen, white = not)
	local c = HSControlState;
	if ( c.pending and HSControlOverlay:IsVisible() ) then
		HSTestMarkerControl:SetTexture(1, 0, 1, 1);
	elseif ( c.last ) then
		local rgb = ({ A = {0, 1, 0}, R = {1, 0, 0}, U = {0, 0, 1} })[c.last.cls] or {0, 0, 0};
		HSTestMarkerControl:SetTexture(rgb[1], rgb[2], rgb[3], 1);
	else
		HSTestMarkerControl:SetTexture(1, 1, 1, 1);
	end
	if ( GlueDialog:IsVisible() ) then
		HSTestMarkerStock:SetTexture(1, 0, 0, 1);
	else
		HSTestMarkerStock:SetTexture(1, 1, 1, 1);
	end
	HSTestMarker:Show();
end

-- Test mode only: count every Lua error the glue screens raise (runtime errors don't reach Logs/GlueXML.log),
-- then hand it on to the previous handler. The count shows in the marker's fourth square.
HS_TEST_ERRORS = 0;
if ( HS_TEST and seterrorhandler ) then
	local previous = geterrorhandler and geterrorhandler();
	seterrorhandler(function(msg, ...)
		HS_TEST_ERRORS = HS_TEST_ERRORS + 1;
		HSFrontMenu_Mark("error:" .. tostring(msg));
		if ( previous ) then return previous(msg, ...); end
	end);
end
HS_TEST_ERRORHANDLER = (HS_TEST and seterrorhandler) and "installed" or "none";

function HSFrontMenu_TestUpdate(self, elapsed)
	if ( not HS_TEST ) then return; end
	-- the marker paints from the first frame on, even if the login screen's own setup failed
	if ( HS_TEST.marker ) then HSFrontMenu_UpdateMarker(); end
	state.testClock = state.testClock + elapsed;      -- seconds since the glue screens loaded
	if ( HS_TEST.script ) then
		for i, step in ipairs(HS_TEST.script) do
			if ( not state.testDone[i] and state.testClock >= step.t ) then
				state.testDone[i] = true;
				local act = step.act;
				if ( act == "continue" ) then
					HSFrontMenu_TestContinue();
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
				else
					HSFrontMenu_TestAct(act);
				end
			end
		end
	end
	if ( HS_TEST.charselect and state.csAt ) then
		state.csDone = state.csDone or {};
		for i, step in ipairs(HS_TEST.charselect) do
			if ( not state.csDone[i] and state.testClock >= state.csAt + step.t ) then
				state.csDone[i] = true;
				HSFrontMenu_TestAct(step.act);
			end
		end
	end
	if ( HS_TEST.charselect_back and state.cbAt ) then
		state.cbDone = state.cbDone or {};
		for i, step in ipairs(HS_TEST.charselect_back) do
			if ( not state.cbDone[i] and state.testClock >= state.cbAt + step.t ) then
				state.cbDone[i] = true;
				HSFrontMenu_TestAct(step.act);
			end
		end
	end
	if ( HS_TEST.charselect_again and state.cs2At ) then
		state.cs2Done = state.cs2Done or {};
		for i, step in ipairs(HS_TEST.charselect_again) do
			if ( not state.cs2Done[i] and state.testClock >= state.cs2At + step.t ) then
				state.cs2Done[i] = true;
				HSFrontMenu_TestAct(step.act);
			end
		end
	end
	-- repaint after this frame's actions too, so a screenshot taken by one of them shows the state they produced
	if ( HS_TEST.marker ) then HSFrontMenu_UpdateMarker(); end
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

-- "continue": click Continue once (the same handler a mouse click runs), only on the login screen (back from the
-- world, the glue screens restart at character select).
function HSFrontMenu_TestContinue()
	if ( CURRENT_GLUE_SCREEN == "login" ) then
		HSFrontMenu_Mark("press:continue");
		HSFrontMenu_Open("main", 1);
		HSMenuSlot1:Click();
	else
		HSFrontMenu_Mark("skip:continue:" .. tostring(CURRENT_GLUE_SCREEN));
	end
end

-- seamless-experience test actions: "details" / "retry" / "back" (the friendly error's controls, clicked),
-- "csinspect" (record character select's wording), "play" (click Play on character select), "shot", "quit",
-- "showlog" (debugging aid: the end of the in-memory test log as text on screen, for a following "shot");
-- seamless-polish: "mainmenu" (click Main Menu on character select; the next character list runs
-- HS_TEST.charselect_again), "continue" (as in HS_TEST.script).
-- saves-channel: "ctl:<verb><arg>" (one control request, HSControl_Send), "realm:reset" (realmList back without the
-- listener's port). Like every act here they exist only in HS_TEST, which only a test run's generated file carries.
function HSFrontMenu_TestAct(act)
	if ( act == "details" ) then
		HSFrontMenu_Mark("press:details");
		HSErrorDialogToggle:Click();
	elseif ( act == "retry" ) then
		HSFrontMenu_Mark("press:retry:" .. (HSErrorDialog:IsShown() and 1 or 0));
		HSErrorDialogRetry:Click();
	elseif ( act == "back" ) then
		HSFrontMenu_Mark("press:back");
		HSErrorDialogBack:Click();
	elseif ( act == "showlog" ) then
		-- debugging aid: the end of the in-memory test log on screen (for a following "shot")
		local lines = {};
		for e in string.gmatch(state.testLog, "[^;]+") do
			if ( strsub(e, 1, 5) ~= "open:" and strsub(e, 1, 9) ~= "backdrop:" ) then table.insert(lines, e); end
		end
		HSTestLogText:SetHeight(500);
		HSTestLogText:SetText(strsub(table.concat(lines, "\n"), -1500));
		HSTestLogText:Show();
	elseif ( act == "csinspect" ) then
		HSSeamless_InspectCharSelect();
	elseif ( act == "mainmenu" ) then
		HSFrontMenu_Mark("press:mainmenu");
		state.csAgain = true;
		CharacterSelectBackButton:Click();
	elseif ( act == "continue" ) then
		HSFrontMenu_TestContinue();
	elseif ( act == "play" ) then
		HSFrontMenu_Mark("press:play:" .. tostring(CharSelectEnterWorldButton:GetText()));
		CharSelectEnterWorldButton:Click();
	elseif ( act == "shot" ) then
		Screenshot();
		HSFrontMenu_Mark("shot");
	elseif ( act == "quit" ) then
		HSFrontMenu_Mark("quit");
		QuitGame();
	elseif ( strsub(act, 1, 4) == "ctl:" ) then         -- saves-channel: one request to the launcher ("ctl:S")
		HSFrontMenu_Mark("press:ctl:" .. strsub(act, 5));
		HSControl_Send(strsub(act, 5, 5), strsub(act, 6));
	elseif ( act == "realm:reset" ) then
		HSControl_ResetRealm();
	elseif ( strsub(act, 1, 8) == "ifround:" ) then      -- prototype: "ifround:N:<act>" runs <act> from round N on
		local n, rest = string.match(act, "^ifround:(%d+):(.+)$");
		if ( n and state.round >= tonumber(n) ) then HSFrontMenu_TestAct(rest); end
	elseif ( act == "csrec" ) then                       -- front-menu-saves prototype: what the screen knows now
		local parts = {};
		for i = 1, GetNumCharacters() do
			local name, race, class, level, zone = GetCharacterInfo(i);
			table.insert(parts, tostring(name) .. "=" .. tostring(level) .. "@" .. tostring(zone));
		end
		HSFrontMenu_Mark("csrec:r" .. state.round .. ":gen=" .. tostring(HS_SAVES and HS_SAVES.gen) .. ":src="
			.. tostring(HS_SAVES and HS_SAVES.src) .. ":screen=" .. tostring(CURRENT_GLUE_SCREEN) .. ":"
			.. table.concat(parts, ","));
		if ( HSSeamless_Rec ) then
			HSSeamless_Rec(format("Z%d:%s:%s:%s", state.round, tostring(HS_SAVES and HS_SAVES.gen),
				tostring(CURRENT_GLUE_SCREEN), table.concat(parts, ",")));
		end
	elseif ( strsub(act, 1, 10) == "roundgate:" ) then   -- prototype: enter the world again until round N, then quit
		if ( state.round >= tonumber(strsub(act, 11)) ) then
			HSFrontMenu_Mark("quit");
			QuitGame();
		else
			HSFrontMenu_TestAct("play");
		end
	else
		HSFrontMenu_Mark("unknown-act:" .. tostring(act));
	end
end

-- ---------------------------------------------------------------- saves-channel: requests to the launcher

-- How the menu asks the launcher something (openspec/changes/saves-channel; channel A1 of the front-menu-saves spike).
-- The launcher listens on 127.0.0.1 (harness/hslib/savesctl.py) and puts its port and a per-launch nonce into the
-- generated HSProfiles.lua as HS_CONTROL. A request points the client's realmList console variable at the listener
-- and "logs in" with the account name HS<nonce><verb><arg>: the client sends its ordinary logon challenge there, and
-- the listener answers with a failure code. The client shows that code as a stock status text (OPEN_STATUS_DIALOG
-- "CANCEL" with LOGIN_STATE_CONNECTING, then UPDATE_STATUS_DIALOG(<text>, OKAY)); the text is the reply. While a request
-- is pending, our small overlay is shown and the stock connecting and error dialogs are kept off screen; the result is
-- acknowledged the way the stock Okay button would (StatusDialogClick), and realmList goes back to what it was, on
-- the reply or after HS_CONTROL_TIMEOUT seconds. A forgotten reset couldn't stick either: the launcher rewrites
-- realmlist at every start (clientcfg.py).
-- Replies: the texts of the two codes the launcher uses (savesctl.py ACCEPTED / REFUSED; the live table in the
-- change's design.md). Any other text, or none in time, means the launcher couldn't be reached.
HS_CONTROL_REPLY = { accepted = "LOGIN_DBBUSY", refused = "LOGIN_BANNED" };
HS_CONTROL_TIMEOUT = 5;
HS_CONTROL_TEXT = { pending = "One moment" .. "\226\128\166", unreachable = "Couldn't reach Moonglade. Nothing changed." };

local CTL = { clock = 0, pending = nil, last = nil, n = 0, graceUntil = 0, saved = nil };
HSControlState = CTL;

local function ctlClean(s)
	return (gsub(gsub(tostring(s or ""), "[\r\n;|\"]", ","), ":", "."));
end

-- Send one request. Returns false (and does nothing) without a listener, while another request is pending, or away
-- from the login screen.
function HSControl_Send(verb, arg)
	local screenOk = CURRENT_GLUE_SCREEN == "login"
		or (HS_TEST and HS_TEST.ctl_charselect and CURRENT_GLUE_SCREEN == "charselect");   -- prototype Q2
	if ( not HS_CONTROL or not HS_CONTROL.port or not HS_CONTROL.nonce or CTL.pending
	     or not screenOk or (HSLoadingOverlay and HSLoadingOverlay:IsShown()) ) then
		HSFrontMenu_Mark("ctl:skip:" .. tostring(verb) .. ":" .. tostring(CURRENT_GLUE_SCREEN));
		return false;
	end
	CTL.n = CTL.n + 1;
	CTL.saved = GetCVar("realmList");
	SetCVar("realmList", "127.0.0.1:" .. HS_CONTROL.port);
	CTL.pending = { verb = verb, t0 = CTL.clock, stockFrames = 0, events = 0, result = nil,
	                realmSet = GetCVar("realmList") };
	HSControlOverlayText:SetText(HS_CONTROL_TEXT.pending);
	HSControlOverlay:Show();
	HSFrontMenu_Mark("ctl:send:" .. verb .. ":realm=" .. (CTL.pending.realmSet == ("127.0.0.1:" .. HS_CONTROL.port) and 1 or 0));
	-- DefaultServerLogin wipes the password string it's given, in place (HSFrontMenu_Login): a throwaway one per request
	DefaultServerLogin("HS" .. HS_CONTROL.nonce .. verb .. (arg or ""), format("HSCTL%dX", CTL.n));
	return true;
end

local function classify(text)
	if ( text and _G[HS_CONTROL_REPLY.accepted] and text == _G[HS_CONTROL_REPLY.accepted] ) then return "A"; end
	if ( text and _G[HS_CONTROL_REPLY.refused] and text == _G[HS_CONTROL_REPLY.refused] ) then return "R"; end
	return "U";
end

local function finish(text, why)
	local p = CTL.pending;
	CTL.pending = nil;
	CTL.graceUntil = CTL.clock + 2;           -- late status events of this request stay off screen too
	SetCVar("realmList", CTL.saved);
	local realmOk = (GetCVar("realmList") == CTL.saved) and 1 or 0;
	local cls = classify(text);
	HSControlOverlay:Hide();
	CTL.last = { verb = p.verb, cls = cls, text = text, why = why, secs = CTL.clock - p.t0, realmOk = realmOk,
	             stockFrames = p.stockFrames };
	HSFrontMenu_Mark(format("ctl:result:%s:%s:%s:%.1fs:realm=%d:stock=%d:%s", p.verb, cls, why, CTL.clock - p.t0,
		realmOk, p.stockFrames, ctlClean(strsub(text or "", 1, 60))));
	if ( HSSeamless_Rec ) then
		HSSeamless_Rec(format("K%s%d%d:%s:%d", cls, realmOk, p.stockFrames > 0 and 1 or 0,
			HSSeamless_Adler(text or ""), math.floor((CTL.clock - p.t0) * 10 + 0.5)));
	end
end

-- realm:reset (test action): realmList back to the value before the last request (or without a port).
function HSControl_ResetRealm()
	local v = CTL.saved or gsub(GetCVar("realmList") or "", ":%d+$", "");
	SetCVar("realmList", v);
	HSFrontMenu_Mark("realm:reset:" .. ((GetCVar("realmList") == v) and 1 or 0));
end

function HSControl_OnUpdate(self, elapsed)
	CTL.clock = CTL.clock + elapsed;
	local p = CTL.pending;
	if ( not p ) then return; end
	if ( GlueDialog:IsVisible() ) then p.stockFrames = p.stockFrames + 1; end
	if ( p.result ) then
		StatusDialogClick();                      -- what the stock Okay button does (GlueDialogTypes.OKAY.OnAccept)
		finish(p.result, "reply");
	elseif ( CTL.clock - p.t0 > HS_CONTROL_TIMEOUT ) then
		StatusDialogClick();                      -- what the stock Cancel button does (GlueDialogTypes.CANCEL.OnAccept)
		finish(nil, "timeout");
	end
end

-- While a request is pending (and briefly after it), the client's status dialogs are ours: recorded, never shown.
local HS_Ctl_DialogShow = GlueDialog_Show;
function GlueDialog_Show(which, text, data)
	if ( CTL.pending or CTL.clock < CTL.graceUntil ) then
		HSFrontMenu_Mark("ctl:icpt:" .. tostring(which));
		if ( CTL.pending and which ~= "CANCEL" and not CTL.pending.result ) then
			CTL.pending.result = text or (GlueDialogTypes[which] and GlueDialogTypes[which].text) or "";
		elseif ( not CTL.pending and which ~= "CANCEL" ) then
			StatusDialogClick();
		end
		return;
	end
	return HS_Ctl_DialogShow(which, text, data);
end

local HS_Ctl_DialogOnEvent = GlueDialog_OnEvent;
function GlueDialog_OnEvent(self, event, arg1, arg2, arg3)
	if ( (CTL.pending or CTL.clock < CTL.graceUntil)
	     and (event == "OPEN_STATUS_DIALOG" or event == "UPDATE_STATUS_DIALOG" or event == "CLOSE_STATUS_DIALOG") ) then
		local p = CTL.pending;
		if ( p ) then p.events = p.events + 1; end
		HSFrontMenu_Mark("ctl:ev:" .. event .. ":" .. ctlClean(strsub(tostring(arg1 or ""), 1, 40)) .. ":"
			.. ctlClean(strsub(tostring(arg2 or ""), 1, 40)));
		if ( event == "OPEN_STATUS_DIALOG" ) then
			GlueDialog_Show(arg1, arg2, arg3);
		elseif ( event == "UPDATE_STATUS_DIALOG" and arg1 and strlen(arg1) > 0 and arg2 and arg2 ~= CANCEL ) then
			if ( p and not p.result ) then
				p.result = arg1;
			elseif ( not p ) then
				StatusDialogClick();
			end
		end
		return;
	end
	return HS_Ctl_DialogOnEvent(self, event, arg1, arg2, arg3);
end

-- ---------------------------------------------------------------- load time

-- Called from HSMenuPanel's OnLoad (HSFrontMenu.xml).
function HSFrontMenu_Init()
	if ( HS_TEST ) then
		HSFrontMenu_Mark("stored=" .. HSFrontMenu_StoredBackdrop().key);
	end
end

-- front-menu-saves prototype (task 1.1): count the glue loads of this client run in the saved account list (a client
-- console variable, kept in memory across glue loads), so a check can tell a fresh glue session from a continued one.
state.round = 1;
if ( HS_TEST ) then
	local list = GetSavedAccountList() or "";
	state.round = (tonumber(string.match(list, "HSRN(%d+)|") or "0") or 0) + 1;
	SetSavedAccountList(gsub(list, "HSRN%d+|", "") .. "HSRN" .. state.round .. "|");
	HSFrontMenu_Mark("load:r" .. state.round .. ":gen=" .. tostring(HS_SAVES and HS_SAVES.gen));
end

-- Music for the very first PlayGlueMusic (SetGlueScreen / START_GLUE_MUSIC) follows the remembered backdrop.
CurrentGlueMusic = HSFrontMenu_StoredBackdrop().music;
