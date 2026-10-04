-- Hearthstone front menu (openspec/changes/in-client-login, patch 0002).
--
-- The stock 3.3.5a login screen (animated backdrop, music, logo, Options/Credits/Cinematics/Quit) with the
-- account/password boxes replaced by a menu:
--   * a prominent Continue button for the last-used profile (GetSavedAccountName); Enter or a click logs in;
--   * the other profiles below it, each with Play;
--   * no timer, no typing: the logins come from HSProfiles.lua, which the launcher generates at each start inside
--     the play client (never committed, never in client/) and removes when the game closes;
--   * a backdrop picker (Lich King / Burning Crusade / Classic) that swaps the login model, logo and music and
--     remembers the choice in the client's own settings.
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
HS_MAX_OTHERS = 4;

HS_PROFILES = HS_PROFILES or {};

local state = { shown = false, backdrop = nil, continueProfile = nil, others = {},
                testClock = 0, testDone = {}, quitAt = nil, testLog = "" };
HSFrontMenuState = state;

-- ---------------------------------------------------------------- test hook marks

-- Test hook: only when the generated HSProfiles.lua carries HS_TEST (harness checks). Records `event` in
-- HSFrontMenuState.testLog; the "showlog" script action puts it on screen (HSMenuWho) so a screenshot carries it.
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
	for i = 1, #HS_BACKDROPS do
		local button = _G["HSBackdropButton" .. i];
		if ( HS_BACKDROPS[i] == b ) then button:LockHighlight(); else button:UnlockHighlight(); end
	end
	HSFrontMenu_Mark("backdrop:" .. b.key .. ":" .. b.model .. ":" .. b.music);
end

function HSFrontMenu_BackdropClicked(self)
	PlaySound("gsTitleOptionOK");
	HSFrontMenu_ApplyBackdrop(HS_BACKDROPS[self:GetID()], true);
end

-- ---------------------------------------------------------------- profiles and login

local function profileByAccount(account)
	if ( not account or account == "" ) then return nil; end
	for i, p in ipairs(HS_PROFILES) do
		if ( strupper(p.account) == strupper(account) ) then return p, i; end
	end
	return nil;
end

-- The Continue profile: the last one used (saved account name), else the launcher's default, else the first.
function HSFrontMenu_ContinueProfile()
	return profileByAccount(GetSavedAccountName()) or profileByAccount(HS_PROFILE_DEFAULT) or HS_PROFILES[1];
end

function HSFrontMenu_Login(profile, how)
	if ( not profile ) then return; end
	HSFrontMenu_Mark("login:" .. profile.account .. ":" .. (how or "?"));
	PlaySound("gsLogin");
	SetSavedAccountName(profile.account);
	DefaultServerLogin(profile.account, profile.password);
end

function HSFrontMenu_Continue(how)
	HSFrontMenu_Login(state.continueProfile, how or "continue");
end

function HSFrontMenu_RowClicked(row)
	HSFrontMenu_Login(state.others[row:GetID()], "play");
end

function HSFrontMenu_Layout()
	local cont = HSFrontMenu_ContinueProfile();
	state.continueProfile = cont;
	state.others = {};
	for _, p in ipairs(HS_PROFILES) do
		if ( p ~= cont ) then table.insert(state.others, p); end
	end
	if ( cont ) then
		HSMenuWho:SetText(cont.name or cont.account);
		HSContinueButton:Enable();
	else
		HSMenuWho:SetText("No profiles: start the game from the Hearthstone app");
		HSContinueButton:Disable();
	end
	HSMenu:Show();
	local n = math.min(#state.others, HS_MAX_OTHERS);
	for i = 1, HS_MAX_OTHERS do
		local row = _G["HSProfileRow" .. i];
		if ( i <= n ) then
			local p = state.others[i];
			_G["HSProfileRow" .. i .. "Name"]:SetText(p.name or p.account);
			row:ClearAllPoints();
			row:SetPoint("TOP", HSOthers, "TOP", 0, -30 - (i - 1) * 28);
			row:Show();
		else
			row:Hide();
		end
	end
	if ( n > 0 ) then
		HSOthers:SetHeight(42 + n * 28);
		HSOthers:Show();
	else
		HSOthers:Hide();
	end
	HSFrontMenu_Mark("menu:" .. #HS_PROFILES .. ":continue=" .. (cont and cont.account or "-") .. ":others=" .. n);
end

-- ---------------------------------------------------------------- wrapping the stock login screen

local stockHide = { "AccountLoginAccountEdit", "AccountLoginPasswordEdit", "AccountLoginTokenEdit",
	"AccountLoginLoginButton", "AccountLoginSaveAccountName", "AccountLoginSaveAccountNameText",
	"AccountLoginDropDown", "AccountLoginManageAccountButton", "AccountLoginCommunityButton",
	"AccountLoginShowLauncher", "AccountLoginTOSButton", "AccountLoginUpgradeAccountButton" };

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
	-- the stock OnHide clears the saved account list (our backdrop) unless "remember account name" is checked
	AccountLoginSaveAccountName:SetChecked(1);
	HSFrontMenu_ApplyBackdrop(b, false);
	state.shown = true;
	HSFrontMenu_Layout();
end

function AccountLogin_OnKeyDown(key)
	if ( key == "ENTER" ) then
		if ( TOSAccepted() and not TOSFrame:IsShown() and not ConnectionHelpFrame:IsShown()
		     and not GlueDialog:IsShown() and HSContinueButton:IsEnabled() == 1 ) then
			HSFrontMenu_Continue("enter");
		end
		return;
	end
	HS_Stock_OnKeyDown(key);
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
-- "backdrop:TBC" | ...}, ... }; HS_TEST.quit_after_charlist = seconds. "continue" clicks the Continue button once
-- (the same handler a mouse click runs); nothing here injects keystrokes.
function HSFrontMenu_TestUpdate(self, elapsed)
	if ( not HS_TEST or not state.shown ) then return; end
	state.testClock = state.testClock + elapsed;
	if ( HS_TEST.script ) then
		for i, step in ipairs(HS_TEST.script) do
			if ( not state.testDone[i] and state.testClock >= step.t ) then
				state.testDone[i] = true;
				local act = step.act;
				if ( act == "continue" ) then
					HSFrontMenu_Mark("press:continue");
					HSContinueButton:Click();
				elseif ( act == "shot" ) then
					Screenshot();
					HSFrontMenu_Mark("shot");
				elseif ( act == "quit" ) then
					HSFrontMenu_Mark("quit");
					QuitGame();
				elseif ( act == "enterworld" ) then
					HSFrontMenu_Mark("press:enterworld");
					EnterWorld();
				elseif ( act == "showlog" ) then
					HSMenuWho:SetText(state.testLog .. " | saved=" .. (GetSavedAccountName() or "") .. "/"
					                  .. (GetSavedAccountList() or ""));
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

-- Called from HSBackdropPanel's OnLoad (HSFrontMenu.xml), once its buttons exist.
function HSFrontMenu_Init()
	HSContinueButton:SetText("Continue");
	for i = 1, HS_MAX_OTHERS do
		_G["HSProfileRow" .. i .. "Play"]:SetText("Play");
	end
	HSOthersTitle:SetText("Other profiles");
	HSBackdropPanelLabel:SetText("Login screen");
	for i, b in ipairs(HS_BACKDROPS) do
		_G["HSBackdropButton" .. i]:SetText(b.label);
	end
	if ( HS_TEST ) then
		HSFrontMenu_Mark("stored=" .. HSFrontMenu_StoredBackdrop().key);
	end
end

-- Music for the very first PlayGlueMusic (SetGlueScreen / START_GLUE_MUSIC) follows the remembered backdrop.
CurrentGlueMusic = HSFrontMenu_StoredBackdrop().music;
