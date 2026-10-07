-- Moonglade wording add-on (openspec/changes/seamless-experience, client-connectivity "In-game wording add-on").
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
local test = { on = false, errors = 0, errorMsgs = {}, popup = nil, clock = 0, readyAt = nil, phase = nil, diag = {} };

local function diag(e)
	if ( #test.diag < 30 ) then table.insert(test.diag, format("%.2f %s", test.clock, e)); end
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
		build = select(4, GetBuildInfo()),
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
frame:SetScript("OnEvent", function(self, event, arg1)
	if ( event == "ADDON_LOADED" and arg1 == "Moonglade" ) then
		if ( type(MoongladeTest) == "table" and MoongladeTest.hook == 1 ) then
			test.on = true;
			MoongladeTest.report = nil;
			local previous = geterrorhandler();
			seterrorhandler(function(msg, ...)
				test.errors = test.errors + 1;
				if ( #test.errorMsgs < 5 ) then table.insert(test.errorMsgs, tostring(msg)); end
				if ( previous ) then return previous(msg, ...); end
			end);
		else
			MoongladeTest = nil;                -- nothing is saved outside the test hook
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
	if ( test.phase == "settle" ) then
		-- a StopCinematic() during the loading screen doesn't take (measured: the intro then played to its end, 88 s);
		-- repeat it once a second while a cinematic runs, as a player pressing Escape again would
		if ( InCinematic() and test.clock - (test.lastStop or -10) >= 1 ) then
			test.lastStop = test.clock;
			StopCinematic();
		end
		-- the world UI must be up (no loading screen, no cinematic) for SETTLE_S, as when a player opens the menu
		if ( not worldUp() ) then test.readyAt = nil; return; end
		test.readyAt = test.readyAt or test.clock;
		test.startedAt = test.startedAt or test.clock;
		if ( test.clock - test.readyAt >= SETTLE_S ) then test.phase = "show"; end
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
