-- Moonglade saves at the character screen (openspec/changes/front-menu-saves, design.md 5, 6 and 8).
--
-- A "Saves" button next to Delete Character opens a panel with the selected character's saves, newest first: label,
-- kind ("Quicksave", "Autosave", "Before loading", "Saved") and time. Load asks "Load '<label>'? <Name> goes back to
-- <time>. Your other characters aren't affected." and then asks the launcher over the control channel (HSControl_Send
-- "L<generation><index>", HSFrontMenu.lua; the launcher maps it to the save and has the server load it). While it runs,
-- "Loading your save..." is on screen. On success the character list is fetched again (GetCharacterListUpdate), so
-- the restored level and place show without leaving the screen, and a line says "Loaded '<label>'".
--
-- The list is HS_SAVES (+ HS_SAVES_END), which the launcher keeps current in the play client (harness/hslib/
-- charsaves.py). The glue screens read it each time they load: at game start and back from the world. It carries
-- labels and indexes only, never a save's id or path. A torn read (the launcher rewrote it while the glue loaded) shows
-- as HS_SAVES_END ~= HS_SAVES.gen; then, or without a list at all, the panel says the saves can't be shown right now.
--
-- Right after a login the client keeps its login-server connection open for a while (measured 4-27 s), and a request
-- sent meanwhile never leaves the client (design.md "Prototype results"). Such an attempt ends early as "noconnect";
-- the load is then sent again every RETRY_S until LOAD_LIMIT_S, with the overlay up all along. Retrying is safe:
-- nothing reached the launcher.
--
-- Test actions (HS_TEST only, via HSFrontMenu_TestAct "saves:<act>"): open, pick:<n> (the n-th row), load (the Load
-- button), confirm (the dialog's Load), close. Test record tokens: V<count>:<kind1 initial><kind2 initial>:<first 4
-- hex of label1's adler>:<same for label2> (panel opened; the first two rows), J<class>:<attempts>:<tenths from confirm>:<stock dialog frames> (load
-- result), N:<adler> (the notice), U<level> (the selected character's level in the first character list after a load).

HS_SAVES_TEXT = {
	button = "Saves",
	title = "%s's saves",
	load = "Load",
	close = "Close",
	cancel = "Cancel",
	empty = "No saves yet. Save in the game: Esc, then Saves.",
	unavailable = "Your saves can't be shown right now. They come back after you play.",
	confirm = "Load \"%s\"?\n%s goes back to %s. Your other characters aren't affected.",
	loading = "Loading your save" .. "\226\128\166",
	loaded = "Loaded \"%s\"",
	failed = "Couldn't load that save. Nothing changed.",
	unreachable = "Couldn't reach Moonglade. Nothing changed.",
	info = "%s  \194\183  %s  \194\183  Level %s",
};
local T = HS_SAVES_TEXT;
local ROWS = 10;
local RETRY_S, LOAD_LIMIT_S = 2, 60;

local CS = { page = 1, sel = nil, rows = {}, name = nil, load = nil, afterLoad = false, clock = 0 };
HSCharSavesState = CS;

local function rec(tok) if ( HSSeamless_Rec ) then HSSeamless_Rec(tok); end end
local function adler(s) return HSSeamless_Adler and HSSeamless_Adler(s or "") or ""; end

function HSCharSaves_Valid()
	return type(HS_SAVES) == "table" and type(HS_SAVES.chars) == "table" and HS_SAVES.gen ~= nil
		and HS_SAVES_END == HS_SAVES.gen;
end

local function savesOf(name)
	if ( not HSCharSaves_Valid() or not name ) then return nil; end
	for _, c in ipairs(HS_SAVES.chars) do
		if ( c.name == name ) then return c.saves or {}; end
	end
	return {};
end

local function selectedName()
	local i = CharacterSelect and CharacterSelect.selectedIndex;
	if ( not i or i < 1 ) then return nil; end
	return (GetCharacterInfo(i));
end

function HSCharSaves_Render()
	local rows = CS.rows or {};
	local pages = math.max(1, math.ceil(#rows / ROWS));
	if ( CS.page > pages ) then CS.page = pages; end
	for i = 1, ROWS do
		local k = (CS.page - 1) * ROWS + i;
		local r, b = rows[k], _G["HSSavesRow" .. i];
		if ( r ) then
			b.k = k;
			_G["HSSavesRow" .. i .. "Label"]:SetText(r.label);
			_G["HSSavesRow" .. i .. "Info"]:SetText(format(T.info, r.kind or "", r.when or "", tostring(r.level or "?")));
			if ( CS.sel == k ) then _G["HSSavesRow" .. i .. "Sel"]:Show(); else _G["HSSavesRow" .. i .. "Sel"]:Hide(); end
			b:Show();
		else
			b:Hide();
		end
	end
	if ( CS.rows == nil or not HSCharSaves_Valid() ) then
		HSSavesPanelEmpty:SetText(T.unavailable);
		HSSavesPanelEmpty:Show();
	elseif ( #rows == 0 ) then
		HSSavesPanelEmpty:SetText(T.empty);
		HSSavesPanelEmpty:Show();
	else
		HSSavesPanelEmpty:Hide();
	end
	if ( CS.sel and rows[CS.sel] ) then HSSavesLoad:Enable(); else HSSavesLoad:Disable(); end
	if ( CS.page > 1 ) then HSSavesPrev:Enable(); else HSSavesPrev:Disable(); end
	if ( CS.page < pages ) then HSSavesNext:Enable(); else HSSavesNext:Disable(); end
end

function HSCharSaves_Open()
	CS.name = selectedName();
	if ( not CS.name ) then return; end
	PlaySound("gsTitleOptionOK");
	CS.rows = savesOf(CS.name);
	CS.page, CS.sel = 1, (CS.rows and CS.rows[1]) and 1 or nil;
	HSSavesPanelTitle:SetText(format(T.title, CS.name));
	HSSavesNotice:Hide();
	HSSavesPanel:Show();
	HSCharSaves_Render();
	local r1, r2 = CS.rows and CS.rows[1], CS.rows and CS.rows[2];
	HSFrontMenu_Mark("saves:open:" .. tostring(CS.name) .. ":" .. (CS.rows and #CS.rows or -1));
	rec(format("V%d:%s%s:%s:%s", CS.rows and #CS.rows or -1, strsub(r1 and r1.kind or "-", 1, 1),
		strsub(r2 and r2.kind or "-", 1, 1), strsub(adler(r1 and r1.label), 1, 4), strsub(adler(r2 and r2.label), 1, 4)));
end

function HSCharSaves_Close()
	HSSavesConfirm:Hide();
	HSSavesPanel:Hide();
end

function HSCharSaves_Page(d)
	CS.page = math.max(1, CS.page + d);
	HSCharSaves_Render();
end

function HSCharSaves_RowClicked(self)
	CS.sel = self.k;
	HSCharSaves_Render();
end

function HSCharSaves_AskLoad()
	local r = CS.sel and CS.rows and CS.rows[CS.sel];
	if ( not r or CS.load ) then return; end
	HSSavesConfirmText:SetText(format(T.confirm, r.label, CS.name or "", r.when or ""));
	HSSavesConfirm:Show();
end

local function notice(text)
	HSSavesNoticeText:SetText(text);
	HSSavesNotice:Show();
	HSFrontMenu_Mark("saves:notice:" .. adler(text));
	rec("N:" .. adler(text));
end

local function attempt()
	local L = CS.load;
	L.attempts = L.attempts + 1;
	L.nextAt = nil;
	local sent = HSControl_Send("L", L.token, function(cls, why, stock)
		L.stock = L.stock + (stock or 0);
		if ( cls == "U" and why == "noconnect" and CS.clock - L.t0 < LOAD_LIMIT_S ) then
			HSControlOverlayText:SetText(T.loading);
			HSControlOverlay:Show();             -- stays up between attempts
			L.nextAt = CS.clock + RETRY_S;
			return;
		end
		HSControlOverlay:Hide();
		CS.load = nil;
		rec(format("J%s:%d:%d:%d", cls, L.attempts, math.floor((CS.clock - L.t0) * 10 + 0.5), L.stock));
		HSFrontMenu_Mark(format("saves:result:%s:%s:%d", cls, tostring(why), L.attempts));
		if ( cls == "A" ) then
			HSCharSaves_Close();
			CS.afterLoad = true;
			notice(format(T.loaded, L.label));
			GetCharacterListUpdate();
		else
			notice(cls == "R" and T.failed or T.unreachable);
		end
	end, T.loading);
	if ( not sent ) then
		L.nextAt = CS.clock + RETRY_S;           -- e.g. another request still pending: try again shortly
		if ( CS.clock - L.t0 >= LOAD_LIMIT_S ) then
			HSControlOverlay:Hide();
			CS.load = nil;
			rec(format("J%s:%d:%d:%d", "U", L.attempts, math.floor((CS.clock - L.t0) * 10 + 0.5), L.stock));
			notice(T.unreachable);
		end
	end
end

function HSCharSaves_Confirm()
	HSSavesConfirm:Hide();
	local r = CS.sel and CS.rows and CS.rows[CS.sel];
	if ( not r or CS.load or not HSCharSaves_Valid() ) then return; end
	PlaySound("gsTitleOptionOK");
	CS.load = { token = format("%s%02d", HS_SAVES.gen, r.idx), label = r.label, t0 = CS.clock, attempts = 0, stock = 0 };
	HSControlOverlayText:SetText(T.loading);
	HSControlOverlay:Show();
	attempt();
end

function HSCharSaves_OnUpdate(self, elapsed)
	CS.clock = CS.clock + elapsed;
	if ( CS.load and CS.load.nextAt and CS.clock >= CS.load.nextAt ) then attempt(); end
end

function HSCharSaves_OnEvent(self, event)
	if ( event == "CHARACTER_LIST_UPDATE" and CS.afterLoad ) then
		CS.afterLoad = false;
		local i = CharacterSelect and CharacterSelect.selectedIndex or 1;
		local _, _, _, level = GetCharacterInfo(i);
		HSFrontMenu_Mark("saves:list:" .. tostring(level));
		rec("U" .. tostring(level));
	end
end

-- the button's text and the panel's buttons (their XML text stays empty: the wording lives here)
local HS_CS_OnShow = CharacterSelect_OnShow;
function CharacterSelect_OnShow(...)
	if ( HSFrontMenuState and not HSFrontMenuState.startScreen ) then HSFrontMenuState.startScreen = "charselect"; end
	HS_CS_OnShow(...);
	HSSavesButton:SetText(T.button);
	HSSavesLoad:SetText(T.load);
	HSSavesClose:SetText(T.close);
	HSSavesConfirmYes:SetText(T.load);
	HSSavesConfirmNo:SetText(T.cancel);
	HSSavesPanel:Hide();
	HSSavesConfirm:Hide();
end

-- keys while the panel or the dialog is up: Escape closes, Enter loads / confirms, Up and Down move the selection;
-- they never reach the character screen's own keys (Enter there would enter the world)
local HS_CS_OnKeyDown = CharacterSelect_OnKeyDown;
function CharacterSelect_OnKeyDown(self, key)
	if ( HSControlOverlay and HSControlOverlay:IsShown() ) then return; end
	if ( HSSavesConfirm:IsShown() ) then
		if ( key == "ESCAPE" ) then HSSavesConfirm:Hide(); elseif ( key == "ENTER" ) then HSCharSaves_Confirm(); end
		return;
	end
	if ( HSSavesPanel:IsShown() ) then
		local n = CS.rows and #CS.rows or 0;
		if ( key == "ESCAPE" ) then HSCharSaves_Close();
		elseif ( key == "ENTER" ) then HSCharSaves_AskLoad();
		elseif ( (key == "UP" or key == "DOWN") and n > 0 ) then
			CS.sel = (((CS.sel or 1) - 1 + (key == "UP" and -1 or 1)) % n) + 1;
			CS.page = math.floor((CS.sel - 1) / ROWS) + 1;
			HSCharSaves_Render();
		end
		return;
	end
	return HS_CS_OnKeyDown(self, key);
end

function HSCharSaves_TestAct(act)
	HSFrontMenu_Mark("press:saves:" .. act);
	if ( act == "open" ) then
		HSSavesButton:Click();
	elseif ( strsub(act, 1, 5) == "pick:" ) then
		local n = tonumber(strsub(act, 6));
		if ( n and CS.rows and CS.rows[n] ) then CS.sel = n; CS.page = math.floor((n - 1) / ROWS) + 1; HSCharSaves_Render(); end
	elseif ( act == "load" ) then
		HSSavesLoad:Click();
	elseif ( act == "confirm" ) then
		HSSavesConfirmYes:Click();
	elseif ( act == "close" ) then
		HSSavesClose:Click();
	end
end
