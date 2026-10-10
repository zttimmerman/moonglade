-- Moonglade wording add-on (openspec/changes/seamless-experience, client-connectivity "In-game wording add-on"), and the
-- game menu's true pause (openspec/changes/true-pause): Esc pauses the world, closing the menu resumes.
--
-- Shipped in the play client as data (patch 0003, Interface\AddOns\Moonglade\, a normal add-on folder: no FrameXML
-- file is changed) and enabled by default (a new add-on is enabled unless the account's AddOns.txt disables it).
-- At load it replaces server-flavoured interface strings with game wording. The originals are read from the client
-- (the globals FrameXML's GlobalStrings.lua defined) just before they're replaced; every replacement keeps the
-- original's format specifiers. Strings FrameXML copied into its own frames at load time are updated there too:
-- the game menu's Logout button (GameMenuFrame.xml text="LOGOUT") and the logout countdown popup
-- (StaticPopupDialogs["CAMP"].text = CAMP_TIMER in StaticPopup.lua).
-- The glue screens' disconnect messages (GlueStrings DISCONNECTED, RESPONSE_DISCONNECTED) live in GlueXML, which an
-- add-on can't reach; the front menu shows them as a friendly error instead (client-mods/glue/HSSeamless.lua).
--
-- Test hook (harness checks only): when the SavedVariables table MoongladeTest holds hook = 1, the add-on counts Lua
-- errors from its load on, shows the logout countdown popup once the world UI is up and records what it reads
-- (StaticPopup_Show("CAMP"), as UIParent.lua does on PLAYER_CAMPING), then logs out, and writes a report into MoongladeTest at logout (the client saves SavedVariables then). Without the hook MoongladeTest
-- is cleared at load, so nothing is saved. The client reads SavedVariables from the logged-in account's own folder, and
-- only the harness writes hook = 1, only into WTF/Account/HSTEST/ of a scratch play client
-- (seamless_checks.plant_addon_hook refuses any other client), so the hook never reaches the player's account.
-- (The account name itself isn't readable here: GetCVar("accountName") didn't match in the world, measured.)
-- With hook = "pause" the same planted table runs the true-pause test instead (client.pause_menu; see "Pause test
-- mode" below): no popup, two screenshots around the harness's Esc presses, the pause messages sent and answered.

local WORDING = {
	LOGOUT = "Exit to Menu",
	CAMP_TIMER = "%d %s until you exit to the menu",
	CAMP_NOW = "Exit now",
	ERR_LOGOUT_FAILED = "You can't exit to the menu now.",
	PLAYER_LOGOUT_FAILED = "Couldn't exit to the menu",
	PLAYER_LOGOUT_FAILED_ERROR = "You can't exit to the menu because you can't sit down right now.",
	IDLE_MESSAGE = "You have been inactive for some time and will return to the menu. If you wish to keep playing, hit the cancel button.",
	ERR_COMSAT_DISCONNECT = "Voice chat stopped.",
	SERVER_MESSAGE_PREFIX = "[World]",
};
MOONGLADE_WORDING = WORDING;

local original = {};
for name, text in pairs(WORDING) do
	original[name] = _G[name];
	_G[name] = text;
end

if ( GameMenuButtonLogout ) then GameMenuButtonLogout:SetText(LOGOUT); end
if ( StaticPopupDialogs and StaticPopupDialogs["CAMP"] ) then StaticPopupDialogs["CAMP"].text = CAMP_TIMER; end

-- ---------------------------------------------------------------- true pause (openspec/changes/true-pause)

-- Opening the game menu (Esc) pauses the whole world; closing it resumes. The add-on asks the server with a hidden
-- add-on whisper to the player's own character ("MGPAUSE" "1" / "0"); mod-moonglade (MoongladePause.cpp) handles and
-- swallows it, and answers "MGPAUSE" "paused" | "running" | "refused" (on a flight path, boat or zeppelin) |
-- "disabled". The "Paused" banner and a dimmed world view (a black full-screen layer at 50 % opacity, below the menu
-- and the banner: the client keeps drawing idle animations while the server is paused, so the dimming makes the
-- pause read as paused; Zach, 2026-10-09) show only on "paused", so they never claim a pause the server refused. Esc is the
-- only key (no binding) and nothing pauses on losing focus (Zach, 2026-10-09). Logout and Exit resume first: their
-- buttons' PreClick runs before the menu's own OnClick (which hides the menu and logs out), and the server also
-- resumes on any logout request.
local PAUSE_PREFIX = "MGPAUSE";
local pauseHooks = { sent = {}, acks = {} };       -- the test hook's record (harness checks only)

local DIM_ALPHA = 0.5;
-- The dim covers every other piece of UI too (tutorial popups are HIGH, dialogs DIALOG): it is FULLSCREEN, and while it
-- shows the game menu is lifted from DIALOG to FULLSCREEN_DIALOG (put back when the menu closes), so only the menu and
-- the banner stay above it.
local dim = CreateFrame("Frame", "MoongladePauseDim", UIParent);
dim:SetFrameStrata("FULLSCREEN");
dim:SetAllPoints(UIParent);
dim.tex = dim:CreateTexture(nil, "BACKGROUND");
dim.tex:SetAllPoints(dim);
dim.tex:SetTexture(0, 0, 0, DIM_ALPHA);
dim:Hide();
MOONGLADE_PAUSE_DIM = dim;
local menuStrata;

local function setDim(on)
	if ( on ) then
		if ( not menuStrata ) then
			menuStrata = GameMenuFrame:GetFrameStrata();
			GameMenuFrame:SetFrameStrata("FULLSCREEN_DIALOG");
		end
		dim:Show();
	else
		dim:Hide();
		if ( menuStrata ) then
			GameMenuFrame:SetFrameStrata(menuStrata);
			menuStrata = nil;
		end
	end
end

local banner = CreateFrame("Frame", "MoongladePauseBanner", UIParent);
banner:SetFrameStrata("TOOLTIP");                -- above the dim and the lifted game menu
banner:SetWidth(320);
banner:SetHeight(72);
banner:SetPoint("TOP", UIParent, "TOP", 0, -64);
banner:SetBackdrop({ bgFile = "Interface\\Tooltips\\UI-Tooltip-Background",
                     edgeFile = "Interface\\Tooltips\\UI-Tooltip-Border",
                     tile = true, tileSize = 16, edgeSize = 16, insets = { left = 4, right = 4, top = 4, bottom = 4 } });
banner:SetBackdropColor(0, 0, 0, 0.8);
banner.text = banner:CreateFontString(nil, "OVERLAY", "GameFontNormalHuge");
banner.text:SetPoint("CENTER");
banner.text:SetText("Paused");
banner:Hide();
MOONGLADE_PAUSE_BANNER = banner;

local function askPause(on)
	-- the in-game Saves window (MoongladeSaves.lua) keeps the pause the game menu started when it opens from it (the
	-- window shows before the menu hides); the window's own OnHide resumes unless the menu is up again
	if ( not on and MoongladeSavesFrame and MoongladeSavesFrame:IsShown() ) then return; end
	local me = UnitName("player");
	if ( me ) then
		SendAddonMessage(PAUSE_PREFIX, on and "1" or "0", "WHISPER", me);
		if ( #pauseHooks.sent < 20 ) then table.insert(pauseHooks.sent, on and "1" or "0"); end
	end
	if ( not on ) then banner:Hide(); setDim(false); end
end

MoongladePause_Ask = askPause;
GameMenuFrame:HookScript("OnShow", function() askPause(true); end);
GameMenuFrame:HookScript("OnHide", function() askPause(false); end);
for _, b in ipairs({ GameMenuButtonLogout, GameMenuButtonQuit }) do
	if ( b ) then b:HookScript("PreClick", function() askPause(false); end); end
end

local pauseFrame = CreateFrame("Frame");
pauseFrame:RegisterEvent("CHAT_MSG_ADDON");
pauseFrame:RegisterEvent("PLAYER_LEAVING_WORLD");
pauseFrame:SetScript("OnEvent", function(self, event, prefix, message)
	if ( event == "CHAT_MSG_ADDON" ) then
		if ( prefix ~= PAUSE_PREFIX ) then return; end
		if ( message == "paused" and (GameMenuFrame:IsShown() or (MoongladeSavesFrame and MoongladeSavesFrame:IsShown())) ) then
			banner:Show(); setDim(true);
		else
			banner:Hide(); setDim(false);
		end
		if ( #pauseHooks.acks < 20 ) then table.insert(pauseHooks.acks, message); end
		if ( MoongladePauseTestAck ) then MoongladePauseTestAck(message); end
	else
		banner:Hide();
		setDim(false);
	end
end);

-- ---------------------------------------------------------------- test hook (harness checks only)

-- Sequenced by events and states, not by fixed delays (verifier cycle 1 flake): a freshly created character's first
-- world login plays the intro cinematic (the server starts it, CharacterHandler.cpp "Show cinematic at the first time
-- that player login"; measured: CINEMATIC_START right after PLAYER_ENTERING_WORLD, UIParent hidden, InCinematic() 1),
-- and StaticPopup_Show refuses popups during a cinematic (StaticPopup.lua: `InCinematic() and not
-- info.interruptCinematic` returns nil), so a popup shown on a timer came out nil. Now: the hook skips the cinematic
-- the way the player's Escape does (CinematicFrame.xml: StopCinematic(), repeated while it still runs), waits until the
-- world UI is up, shows the countdown popup, reads it once its countdown text is filled in (StaticPopup_OnUpdate), and only then logs out.
-- If the popup can't be shown and read within GIVE_UP_S, it logs out anyway and the report says why (the check fails).
local SETTLE_S, GIVE_UP_S = 3, 20
local test = { on = false, errors = 0, errorMsgs = {}, popup = nil, clock = 0, readyAt = nil, phase = nil, diag = {}, ticks = 0 };

local function diag(e)
	if ( #test.diag < 30 ) then table.insert(test.diag, format("%.2f %s", test.clock, e)); end
end

-- Pause test mode (MoongladeTest.hook = "pause", client.pause_menu): once the world UI is up the add-on waits for the
-- harness's hardware Esc presses (it takes a screenshot of the world view first, before any Esc). When the server
-- answers "paused" it takes a screenshot a second later (the menu, the banner and the dimming up), records what it shows, and when the server answers "running" after the second Esc it takes another
-- one, then logs out (the report is written then). PAUSE_GIVE_UP_S after the world UI is up it logs out regardless.
local PAUSE_SHOT_DELAY_S, PAUSE_GIVE_UP_S = 1.0, 150
local pauseTest = { shots = {} };

local function pauseShot(tag)
	Screenshot();
	table.insert(pauseTest.shots, { tag = tag, at = format("%.2f", test.clock), banner = banner:IsVisible() and 1 or 0,
	                                 dim = dim:IsVisible() and 1 or 0, menu_strata = GameMenuFrame:GetFrameStrata(),
	                                 tutorial = (TutorialFrame and TutorialFrame:IsVisible()) and 1 or 0,
	                                 menu = GameMenuFrame:IsShown() and 1 or 0, text = banner.text:GetText() });
end

function MoongladePauseTestAck(message)
	if ( not test.on or test.mode ~= "pause" ) then return; end
	diag("ack " .. message);
	if ( message == "paused" and test.phase == "wait_menu" ) then
		test.phase, test.shotAt = "shot_paused", test.clock + PAUSE_SHOT_DELAY_S;
	elseif ( message == "running" and test.phase == "wait_close" ) then
		test.phase, test.shotAt = "shot_running", test.clock + PAUSE_SHOT_DELAY_S;
	end
end

local function report(at)
	local name, title, notes, enabled, loadable, reason = GetAddOnInfo("Moonglade");
	local strings = {};
	for k, v in pairs(WORDING) do
		strings[k] = { original = original[k], new = v, now = _G[k] };
	end
	MoongladeTest.report = {
		at = at, loaded = IsAddOnLoaded("Moonglade") and 1 or 0, enabled = enabled and 1 or 0,
		loadable = loadable and 1 or 0, reason = reason, title = title,
		strings = strings,
		ui = { logout_button = GameMenuButtonLogout and GameMenuButtonLogout:GetText(),
		       camp_popup_template = StaticPopupDialogs["CAMP"] and StaticPopupDialogs["CAMP"].text,
		       camp_popup_shown = test.popup },
		errors = test.errors, error_msgs = test.errorMsgs, diag = test.diag,
		at_logout = { phase = test.phase, ticks = test.ticks, clock = test.clock, ui_visible = UIParent:IsVisible() and 1 or 0,
		              cinematic = InCinematic() and 1 or 0 },
		build = select(4, GetBuildInfo()),
		pause = { mode = test.mode, sent = pauseHooks.sent, acks = pauseHooks.acks, shots = pauseTest.shots,
		          banner_strata = banner:GetFrameStrata() },
	};
end

local function worldUp()
	return UIParent:IsVisible() and not InCinematic();
end

local frame = CreateFrame("Frame");
frame:RegisterEvent("ADDON_LOADED");
frame:RegisterEvent("PLAYER_ENTERING_WORLD");
frame:RegisterEvent("PLAYER_LOGOUT");
frame:RegisterEvent("CINEMATIC_START");
frame:RegisterEvent("CINEMATIC_STOP");
frame:RegisterEvent("PLAYER_LEAVING_WORLD");
frame:RegisterEvent("PLAYER_CAMPING");
frame:RegisterEvent("LOGOUT_CANCEL");
frame:SetScript("OnEvent", function(self, event, arg1)
	if ( event == "ADDON_LOADED" and arg1 == "Moonglade" ) then
		if ( type(MoongladeTest) == "table" and (MoongladeTest.hook == 1 or MoongladeTest.hook == "pause") ) then
			test.on = true;
			test.mode = (MoongladeTest.hook == "pause") and "pause" or "wording";
			MoongladeTest.report = nil;
			for _, fn in ipairs({ "Logout", "Quit", "ForceQuit", "ForceLogout" }) do
				if ( _G[fn] ) then
					hooksecurefunc(fn, function() diag("called " .. fn .. " from " .. gsub(debugstack(3, 2, 0) or "?", "\n", " | ")); end);
				end
			end
			local previous = geterrorhandler();
			seterrorhandler(function(msg, ...)
				test.errors = test.errors + 1;
				if ( #test.errorMsgs < 5 ) then table.insert(test.errorMsgs, tostring(msg)); end
				if ( previous ) then return previous(msg, ...); end
			end);
		elseif ( not (type(MoongladeTest) == "table" and MoongladeTest.hook == "saves") ) then
			MoongladeTest = nil;                -- nothing is saved outside the test hooks (MoongladeSaves.lua: "saves")
		end
		return;
	end
	if ( not test.on ) then return; end
	if ( event ~= "PLAYER_LOGOUT" ) then diag(event); end
	if ( event == "PLAYER_ENTERING_WORLD" and not test.phase ) then
		test.phase = "settle";
	elseif ( event == "CINEMATIC_START" ) then
		StopCinematic();                    -- what the player's Escape does (CinematicFrame.xml)
		diag("cinematic skipped");
	elseif ( event == "PLAYER_LOGOUT" ) then
		report("PLAYER_LOGOUT");
	end
end);

frame:SetScript("OnUpdate", function(self, elapsed)
	if ( not test.on or not test.phase ) then return; end
	test.clock = test.clock + elapsed;
	test.ticks = test.ticks + 1;
	if ( test.phase == "settle" ) then
		-- a StopCinematic() during the loading screen doesn't take (measured: the intro then played to its end, 88 s);
		-- repeat it once a second while a cinematic runs, as a player pressing Escape again would
		if ( InCinematic() and test.clock - (test.lastStop or -10) >= 1 ) then
			test.lastStop = test.clock;
			diag("settle: StopCinematic (InCinematic, UIParent " .. (UIParent:IsVisible() and 1 or 0) .. ")");
			StopCinematic();
		end
		-- the world UI must be up (no loading screen, no cinematic) for SETTLE_S, as when a player opens the menu
		if ( not worldUp() ) then test.readyAt = nil; return; end
		test.readyAt = test.readyAt or test.clock;
		test.startedAt = test.startedAt or test.clock;
		if ( test.clock - test.readyAt >= SETTLE_S ) then
			test.phase = (test.mode == "pause") and "wait_menu" or "show";
			diag("ready " .. test.phase);
			if ( test.mode == "pause" ) then pauseShot("before"); end   -- the world view before Esc
		end
	elseif ( test.mode == "pause" ) then
		if ( (test.phase == "shot_paused" or test.phase == "shot_running") and test.clock >= test.shotAt ) then
			pauseShot(test.phase == "shot_paused" and "paused" or "running");
			diag("shot " .. test.phase);
			if ( test.phase == "shot_paused" ) then test.phase = "wait_close";
			else test.phase, test.logoutAt = "after_shot", test.clock + 1; end
		elseif ( test.phase == "after_shot" and test.clock >= test.logoutAt ) then
			test.phase = "logout";
		end
		if ( test.phase ~= "logout" and test.startedAt and test.clock - test.startedAt > PAUSE_GIVE_UP_S ) then
			diag("gave up in phase " .. test.phase);
			test.phase = "logout";
		end
		if ( test.phase == "logout" ) then
			test.phase = "done";
			Logout();
		end
		return;
	elseif ( test.phase == "show" ) then
		-- the logout countdown popup, the way UIParent.lua shows it on PLAYER_CAMPING
		local d = StaticPopup_Show("CAMP");
		if ( d ) then
			test.phase = "read";
			diag("shown " .. d:GetName());
		end
	elseif ( test.phase == "read" ) then
		-- read it once StaticPopup_OnUpdate has filled in the countdown (the text starts as " ")
		local f = StaticPopup_Visible("CAMP");
		local t = f and _G[f .. "Text"] and _G[f .. "Text"]:GetText();
		if ( t and t ~= " " ) then
			test.popup = t;
			diag("read");
			StaticPopup_Hide("CAMP");
			test.phase = "logout";
		elseif ( not f ) then
			diag("popup closed before it was read");
			test.phase = "show";
		end
	end
	if ( test.phase == "logout" or (test.startedAt and test.clock - test.startedAt > GIVE_UP_S) ) then
		if ( test.phase ~= "logout" ) then diag("gave up in phase " .. test.phase); end
		test.phase = "done";
		Logout();
	end
end);
