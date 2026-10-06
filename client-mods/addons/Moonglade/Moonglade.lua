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
-- Test hook (harness checks only): when the SavedVariables table MoongladeTest holds hook = 1 (planted by a check into
-- a scratch play client's WTF/Account/<test account>/SavedVariables/Moonglade.lua), the add-on counts Lua errors from
-- its load on, shows the logout countdown popup once and records what it read (StaticPopup_Show("CAMP"), as
-- UIParent.lua does on PLAYER_CAMPING), logs out logout_after seconds after entering the world, and
-- writes a report into MoongladeTest at logout (the client saves SavedVariables then). Without the hook MoongladeTest
-- is cleared at load, so nothing is saved.

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

local test = { on = false, errors = 0, errorMsgs = {}, popup = nil, logoutAt = nil, clock = 0 };

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
		errors = test.errors, error_msgs = test.errorMsgs,
		build = select(4, GetBuildInfo()),
	};
end

local frame = CreateFrame("Frame");
frame:RegisterEvent("ADDON_LOADED");
frame:RegisterEvent("PLAYER_ENTERING_WORLD");
frame:RegisterEvent("PLAYER_LOGOUT");
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
			MoongladeTest = nil;
		end
	elseif ( event == "PLAYER_ENTERING_WORLD" and test.on and not test.logoutAt ) then
		test.logoutAt = test.clock + (tonumber(MoongladeTest.logout_after) or 8);
	elseif ( event == "PLAYER_LOGOUT" and test.on ) then
		report("PLAYER_LOGOUT");
	end
end);
frame:SetScript("OnUpdate", function(self, elapsed)
	if ( not test.on ) then return; end
	test.clock = test.clock + elapsed;
	-- before logging out, show the logout countdown popup the way UIParent.lua does on PLAYER_CAMPING
	-- (StaticPopup_Show("CAMP"); its OnUpdate fills in the countdown) and record what it reads; a logout in a rest
	-- area is instant and never shows it
	if ( test.logoutAt and not test.popupShown and test.clock >= test.logoutAt - 3 ) then
		test.popupShown = true;
		StaticPopup_Show("CAMP");
	end
	if ( test.popupShown and not test.popupRead and test.clock >= test.logoutAt - 1 ) then
		test.popupRead = true;
		local f = StaticPopup_Visible("CAMP");
		local t = f and _G[f .. "Text"];
		test.popup = t and t:GetText();
		StaticPopup_Hide("CAMP");
	end
	if ( test.logoutAt and test.clock >= test.logoutAt ) then
		test.logoutAt = math.huge;
		Logout();
	end
end);
