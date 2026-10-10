-- Moonglade seamless experience (openspec/changes/seamless-experience, patch 0002): nothing on the glue screens says
-- server, realm, connecting or account unless the player opens "Show details".
--
-- 1. Loading overlay. From the Continue press until the character list is on screen, a themed "Entering Azeroth..."
--    overlay replaces the stock status dialog. The client reports login progress through GlueDialog.lua: the C++ side
--    fires OPEN_STATUS_DIALOG("CANCEL", <LOGIN_STATE_* / REALM_LIST_* / CHAR_LIST_* text>), then UPDATE_STATUS_DIALOG
--    with the next text, then CLOSE_STATUS_DIALOG; GlueDialog_OnEvent hands OPEN to the global GlueDialog_Show. Both are
--    looked up by name at call time (GlueDialog.xml OnEvent; GlueDialog.lua), so wrapping the globals intercepts them:
--    while the overlay is up, a progress dialog is recorded and not shown. The login state machine itself is untouched:
--    nothing here calls into it except what the stock dialog's own buttons would (StatusDialogClick via OnAccept).
--    The overlay's bar shows real progress (seamless-polish): each login step the client reports through those dialogs
--    moves it forward to that step's mark (STEPS); between steps it creeps toward the next mark without reaching it; it
--    never goes down during one load, starts empty at every load (BeginPhase) and is full when the character list is
--    on screen (EndPhase "charlist").
-- 2. Friendly errors. An error dialog during that time (and a disconnect at any time) becomes a plain message with
--    Try again / Back to menu and a "Show details" toggle that reveals the client's original text. Both buttons first do
--    what the stock dialog's Okay button does (its OnAccept / OnCancel, e.g. StatusDialogClick), then retry the login
--    (HSFrontMenu_Continue) or return to the menu.
-- 3. Character select. The realm name (CharSelectRealmName) and Change Realm (CharSelectChangeRealmButton) are hidden;
--    Enter World (CharSelectEnterWorldButton) reads "Play" and Back (CharacterSelectBackButton) reads "Main Menu".
--    Their OnClick scripts (CharacterSelect_EnterWorld / CharacterSelect_Exit, CharacterSelect.xml) are unchanged.
--    With the realm controls hidden, the first character button moves up into their place (seamless-polish), so the
--    list starts at the top of its panel (CharacterSelectCharacterFrame) with stock WoW's spacing.
-- Frame names, dialog keys and strings are the installed client's (Interface\GlueXML\GlueDialog.lua, GlueParent.lua,
-- CharacterSelect.xml/.lua, GlueStrings.lua, read with `harness/hs mpq`).
--
-- Test hook (HS_TEST only, see HSFrontMenu.lua): every intercepted dialog, overlay change and error action is recorded
-- in a compact test record, handed out through the client's own settings file: just before EnterWorld it is appended
-- to the saved account list (the accountList console variable, which the client writes to WTF/Config.wtf when it
-- enters the world; ADR-0009 appendix A) as "HSLOG<record>|". A console variable holds at most 255 characters
-- (measured: a longer value is cut there), so the record is short tokens, and texts are given as their length and
-- Adler-32 (hex), which the harness recomputes from the client's own string files and our sources. A record that
-- doesn't fit ends in "~" (the checks then fail). Tokens (';'-separated):
--   C Continue pressed   S screenshot   O:<why>:<caption adler> overlay shown   I:<which> a stock progress dialog intercepted (first of
--   each kind per overlay)   V:<which> a stock dialog visible during the overlay   H:<why>:<frames>:<overlay frames>:
--   <stock dialog frames>:<intercepted> overlay hidden   X:<which>:<kind>:<raw len>:<raw adler>:<title adler>:<text
--   adler> friendly error shown   D:<visible>:<len>:<adler> details opened (what they show)   R / B Try again / Back to
--   menu   L<n> character list with n characters   Q:<screen>:<realm shown>:<change realm shown>:<play label>:<back
--   label>:<play shown>:<overlay shown> character select inspected   P:<label> Play clicked   W EnterWorld
--   E<n> Lua errors counted by the test error handler (at the hand-out)   G:<first>:<samples>:<decreases>:<step
--   advances>:<last> the loading bar during one overlay (per mille of full, read back from the bar's width every
--   frame; a step advance is the bar growing inside a reported step's event handler; just before its H)   Y:<panel top>:<first button top>:<panel bottom>:<lowest button bottom> character list
--   layout (GetTop/GetBottom, rounded; with Q)   M Main Menu clicked on character select. Lower case: the test hook's own clicks
--   (d details, r<1|0> Try again while the error was shown or not).
-- Without HS_TEST nothing is recorded and nothing extra is stored.

local ELLIPSIS = "\226\128\166";                -- UTF-8 "..." (one glyph)
local CAPTION_LOGIN = "Entering Azeroth" .. ELLIPSIS;
local CAPTION_LIST = "Loading" .. ELLIPSIS;
local BACK_AFTER_S = 15;                        -- the overlay offers "Back to menu" if a login takes this long

-- The login steps the client reports (the texts of its progress dialog: OPEN_STATUS_DIALOG / UPDATE_STATUS_DIALOG),
-- as their GlueStrings.lua keys (Interface\GlueXML\GlueStrings.lua in the installed client, patch-enUS-3.MPQ, read
-- with `harness/hs mpq`), in login order, with the bar's mark for each. The texts are looked up from the client's own
-- globals at run time, so they are always the installed client's wording.
local STEPS = {
	{ "LOGIN_STATE_CONNECTING", 0.10 },         -- "Connecting"
	{ "LOGIN_STATE_HANDSHAKING", 0.20 },        -- "Handshaking"
	{ "LOGIN_STATE_AUTHENTICATING", 0.30 },     -- "Authenticating"
	{ "LOGIN_STATE_CHECKINGVERSIONS", 0.35 },   -- "Validating Version"
	{ "LOGIN_STATE_AUTHENTICATED", 0.45 },      -- "Success!"
	{ "REALM_LIST_IN_PROGRESS", 0.55 },         -- "Retrieving realm list"
	{ "REALM_LIST_SUCCESS", 0.65 },             -- "Realm list retrieved"
	{ "CHAR_LIST_RETRIEVING", 0.75 },           -- "Retrieving character list"
	{ "CHAR_LIST_RETRIEVED", 0.90 },            -- "Character list retrieved"
};
local BAR_W = 290;                              -- HSLoadingOverlayBarBg's width (HSFrontMenu.xml)
local CREEP_SHARE = 0.7;                        -- between steps the bar creeps up to this share of the way to the next mark
local CREEP_RATE = 1.2;                         -- how fast it creeps (per second, exponential approach)
local UNKNOWN_SHARE = 0.3;                      -- an unknown step text moves the reached mark this share toward the next

-- Character select (Interface\GlueXML\CharacterSelect.xml, patch-enUS-3.MPQ, read with `harness/hs mpq`): inside
-- CharacterSelectCharacterFrame, CharSelectRealmName is anchored TOP y=-10 with height 13, CharSelectChangeRealmButton
-- (height 33) hangs below it, and CharSelectCharacterButton1 is anchored TOPLEFT x=24 y=-65: 9 units below Change Realm.
-- With both hidden, the first button keeps x and that 9-unit gap below the panel's top edge.
local CS_FIRST_X, CS_FIRST_Y = 24, -65;
local CS_REALM_BLOCK = 10 + 13 + 33;

-- The friendly wording. Never server, realm, connect, authenticate or account (login-screen spec, "Friendly errors").
HS_FRIENDLY = {
	login_title = "Couldn't enter Azeroth",
	login_text = "The world didn't answer this time. It usually works on the next try.",
	lost_title = "You left Azeroth",
	lost_text = "The game stopped unexpectedly and brought you back to the menu.",
	retry = "Try again",
	back = "Back to menu",
	show = "Show details",
	hide = "Hide details",
	play = "Play",
	main_menu = "Main Menu",
};

-- Stock dialogs that only report progress while logging in (GlueDialogTypes in GlueDialog.lua).
local PROGRESS = { CANCEL = true };
-- Stock dialogs that report a failure and only need acknowledging (GlueDialogTypes in GlueDialog.lua).
local ERRORS = { OKAY = true, OKAY_HTML = true, OKAY_HTML_EXIT = true, OKAY_WITH_URL = true,
                 CONNECTION_HELP = true, CONNECTION_HELP_HTML = true, DISCONNECTED = true };

local S = { phase = nil, t = 0, since = 0, frames = 0, overlayFrames = 0, dialogFrames = 0, intercepted = 0,
            pending = nil, err = nil, logins = 0, visibleSeen = {}, enteredWorld = false,
            bar = 0, reached = 0, bs = nil };
HSSeamlessState = S;

local function mark(e)
	HSFrontMenu_Mark(e);
end

-- ---------------------------------------------------------------- the compact test record (HS_TEST only)

local REC_MAX = 255 - 16;                       -- the cvar limit, minus "HSBD<backdrop>|", "HSLOG" and "|"
S.rec = {};

local function rec(tok)
	if ( HS_TEST ) then table.insert(S.rec, tok); end
end
HSSeamless_Rec = rec;

-- Adler-32 of a string's bytes as 8 hex digits (Python: format(zlib.adler32(b), "08x")).
function HSSeamless_Adler(s)
	s = tostring(s or "");
	local a, b = 1, 0;
	for i = 1, string.len(s) do
		a = (a + string.byte(s, i)) % 65521;
		b = (b + a) % 65521;
	end
	return format("%04x%04x", b, a);
end
local adler = HSSeamless_Adler;

-- One field of a test record: no separators, quotes or colour codes (Config.wtf quotes the value).
local function clean(s)
	s = tostring(s or "");
	s = gsub(s, "[\r\n]", " ");
	s = gsub(s, "|", "/");
	s = gsub(s, "\"", "'");
	s = gsub(s, ";", ",");
	return s;
end

-- What the player would have read in the stock dialog, as plain text (HTML tags and |n line breaks removed).
function HSSeamless_PlainText(s)
	s = tostring(s or "");
	s = gsub(s, "|n", "\n");
	s = gsub(s, "<[^>]*>", "");
	s = gsub(s, "&amp;", "&");
	return s;
end

-- ---------------------------------------------------------------- the overlay

-- The mark of a reported step text (nil if it isn't one of STEPS), and the next mark above `v` (1 if none).
local function stepMark(text)
	if ( not text ) then return nil; end
	for _, st in ipairs(STEPS) do
		if ( _G[st[1]] and text == _G[st[1]] ) then return st[2]; end
	end
	return nil;
end

local function nextMark(v)
	for _, st in ipairs(STEPS) do
		if ( st[2] > v + 1e-6 ) then return st[2]; end
	end
	return 1;
end

-- The bar as drawn, and the test record's view of it: every frame's width, read back, in per mille of full.
local function barSample()
	if ( not HS_TEST or not S.bs ) then return; end
	local v = math.floor(HSLoadingOverlayBarFill:GetWidth() * 1000 / BAR_W + 0.5);
	local b = S.bs;
	if ( b.n == 0 ) then b.first = v; end
	if ( b.n > 0 and v < b.last ) then b.dec = b.dec + 1; end
	b.n = b.n + 1;
	b.last = v;
end

local function setBar(v)
	S.bar = math.max(S.bar, math.min(1, v));        -- forward only
	HSLoadingOverlayBarFill:SetWidth(math.max(1, BAR_W * S.bar));
end

-- A login step reported by the client (the progress dialog's text): the bar moves forward to its mark at once.
function HSSeamless_Step(text)
	if ( not S.phase ) then return; end
	local m = stepMark(text);
	if ( m ) then
		S.reached = math.max(S.reached, m);
	else
		S.reached = S.reached + UNKNOWN_SHARE * (nextMark(S.reached) - S.reached);
	end
	setBar(S.reached);
end

-- A reported step, and the test record's view of it: a "step advance" is the bar's drawn width growing inside the
-- step's own event handler (no time passes there, so a bar that only moves with time never counts).
local function stepped(text)
	local w0 = HSLoadingOverlayBarFill:GetWidth();
	HSSeamless_Step(text);
	if ( S.bs and HSLoadingOverlayBarFill:GetWidth() > w0 + 0.5 ) then S.bs.adv = S.bs.adv + 1; end
end

function HSSeamless_BeginPhase(caption, why)
	if ( HSErrorDialog:IsShown() ) then HSErrorDialog:Hide(); end
	S.phase = why or "?";
	S.since = S.t;
	S.frames, S.overlayFrames, S.dialogFrames, S.intercepted = 0, 0, 0, 0;
	S.visibleSeen = {};
	-- every load starts from an empty bar
	S.bar, S.reached = 0, 0;
	HSLoadingOverlayBarFill:SetWidth(1);
	S.bs = HS_TEST and { n = 0, dec = 0, adv = 0, first = -1, last = -1 } or nil;
	barSample();
	local b = HSFrontMenuState and HSFrontMenuState.backdrop;
	if ( b and b.logo ) then HSLoadingOverlayLogo:SetTexture(b.logo); end
	HSLoadingOverlayText:SetText(caption);
	HSLoadingOverlayBack:Hide();
	HSLoadingOverlay:Show();
	S.kinds = {};
	mark("ov:show:" .. clean(why) .. ":" .. clean(caption));
	rec("O:" .. clean(why) .. ":" .. adler(caption));
end

function HSSeamless_EndPhase(why)
	if ( not S.phase ) then return; end
	if ( why == "charlist" ) then setBar(1); end      -- the character list is on screen: the bar is full
	barSample();
	if ( S.bs ) then
		rec(format("G:%d:%d:%d:%d:%d", S.bs.first, S.bs.n, S.bs.dec, S.bs.adv, S.bs.last));
		S.bs = nil;
	end
	mark(format("ov:hide:%s:frames=%d,overlay=%d,dialog=%d,icpt=%d,secs=%.1f", clean(why), S.frames, S.overlayFrames,
		S.dialogFrames, S.intercepted, S.t - S.since));
	rec(format("H:%s:%d:%d:%d:%d", clean(why), S.frames, S.overlayFrames, S.dialogFrames, S.intercepted));
	S.phase = nil;
	S.pending = nil;
	HSLoadingOverlay:Hide();
end

-- "Back to menu" on the overlay (after BACK_AFTER_S): exactly what the stock Cancel button would do, then the menu.
function HSSeamless_OverlayBack()
	local p = S.pending;
	mark("ov:back:" .. clean(p and p.which));
	HSSeamless_EndPhase("back");
	if ( p and GlueDialogTypes[p.which] and GlueDialogTypes[p.which].OnAccept ) then
		GlueDialogTypes[p.which].OnAccept();
	end
	if ( CURRENT_GLUE_SCREEN == "charselect" ) then
		CharacterSelect_Exit();
	end
end

function HSSeamless_OnUpdate(self, elapsed)
	S.t = S.t + elapsed;
	if ( not S.phase ) then return; end
	S.frames = S.frames + 1;
	if ( HSLoadingOverlay:IsVisible() ) then S.overlayFrames = S.overlayFrames + 1; end
	if ( GlueDialog:IsVisible() ) then
		S.dialogFrames = S.dialogFrames + 1;
		local w = GlueDialog.which or "?";
		if ( not S.visibleSeen[w] ) then
			S.visibleSeen[w] = true;
			mark("visible:" .. clean(w) .. ":" .. clean(GlueDialogText:GetText()));
			rec("V:" .. clean(w));
		end
	end
	-- the loading bar creeps toward the next step's mark (never reaching it before the client reports that step)
	local ceiling = S.reached + CREEP_SHARE * (nextMark(S.reached) - S.reached);
	setBar(S.bar + (ceiling - S.bar) * (1 - math.exp(-CREEP_RATE * elapsed)));
	barSample();
	-- the "Back to menu" link once a login takes unusually long
	if ( S.t - S.since > BACK_AFTER_S and not HSLoadingOverlayBack:IsShown() ) then
		HSLoadingOverlayBack:Show();
	end
end

-- ---------------------------------------------------------------- friendly errors

local function ackFor(which)
	local info = GlueDialogTypes[which];
	if ( not info ) then return nil; end
	if ( info.button1 == OKAY ) then return info.OnAccept; end
	if ( info.button2 == OKAY ) then return info.OnCancel; end
	return info.OnAccept;
end

-- kind: "login" (a failure while entering) or "lost" (a disconnect); raw: the client's own message.
function HSSeamless_ShowError(which, raw, kind, ack)
	HSSeamless_EndPhase("error");
	S.err = { which = which, raw = raw or "", kind = kind, ack = ack };
	HSErrorDialogTitle:SetText(kind == "lost" and HS_FRIENDLY.lost_title or HS_FRIENDLY.login_title);
	HSErrorDialogText:SetText(kind == "lost" and HS_FRIENDLY.lost_text or HS_FRIENDLY.login_text);
	HSErrorDialogDetails:SetText(HSSeamless_PlainText(raw));
	HSErrorDialogDetails:Hide();
	HSErrorDialogToggle:SetText(HS_FRIENDLY.show);
	HSSeamless_LayoutError();
	if ( HSMenuPanel ) then HSMenuPanel:Hide(); end
	HSErrorDialog:Show();
	mark("err:show:" .. clean(which) .. ":" .. clean(kind) .. ":title=" .. clean(HSErrorDialogTitle:GetText())
		.. ":text=" .. clean(HSErrorDialogText:GetText()) .. ":raw=" .. clean(raw));
	rec(format("X:%s:%s:%d:%s:%s:%s", clean(which), clean(kind), strlen(raw or ""), adler(raw or ""),
		adler(HSErrorDialogTitle:GetText()), adler(HSErrorDialogText:GetText())));
end

function HSSeamless_LayoutError()
	local h = 24 + HSErrorDialogTitle:GetHeight() + 12 + HSErrorDialogText:GetHeight() + 18 + 40 + 10 + 20 + 16;
	if ( HSErrorDialogDetails:IsShown() ) then h = h + HSErrorDialogDetails:GetHeight() + 12; end
	HSErrorDialogBox:SetHeight(h);
end

function HSSeamless_ToggleDetails()
	if ( HSErrorDialogDetails:IsShown() ) then
		HSErrorDialogDetails:Hide();
		HSErrorDialogToggle:SetText(HS_FRIENDLY.show);
		mark("err:details:hide");
	else
		HSErrorDialogDetails:Show();
		HSErrorDialogToggle:SetText(HS_FRIENDLY.hide);
		mark("err:details:show:visible=" .. (HSErrorDialogDetails:IsVisible() and 1 or 0) .. ":shown="
			.. clean(HSErrorDialogDetails:GetText()));
		local shown = HSErrorDialogDetails:GetText() or "";
		rec(format("D:%d:%d:%s", HSErrorDialogDetails:IsVisible() and 1 or 0, strlen(shown), adler(shown)));
	end
	HSSeamless_LayoutError();
end

local function closeError(how)
	local e = S.err;
	S.err = nil;
	HSErrorDialog:Hide();
	mark("err:" .. how);
	rec(how == "retry" and "R" or "B");
	if ( e and e.ack ) then e.ack(); end
	if ( CURRENT_GLUE_SCREEN ~= "login" ) then SetGlueScreen("login"); end
end

function HSSeamless_Retry()
	PlaySound("gsTitleOptionOK");
	closeError("retry");
	HSFrontMenu_Continue("retry");
end

function HSSeamless_BackToMenu()
	PlaySound("gsTitleOptionExit");
	closeError("back");
	HSFrontMenu_Open("main", 1);
end

function HSSeamless_ErrorShown()
	return HSErrorDialog:IsShown();
end

-- ---------------------------------------------------------------- the stock dialogs, intercepted

local HS_Stock_DialogShow = GlueDialog_Show;
function GlueDialog_Show(which, text, data)
	local info = GlueDialogTypes[which];
	if ( PROGRESS[which] and (S.phase or (CURRENT_GLUE_SCREEN == "charselect" and text == CHAR_LIST_RETRIEVING)) ) then
		if ( not S.phase ) then HSSeamless_BeginPhase(CAPTION_LIST, "charlist"); end
		S.pending = { which = which, text = text };
		S.intercepted = S.intercepted + 1;
		stepped(text);
		mark("icpt:" .. clean(which) .. ":" .. clean(text));
		if ( not S.kinds[which] ) then S.kinds[which] = true; rec("I:" .. clean(which)); end
		return;
	end
	if ( info and ERRORS[which] and (S.phase or which == "DISCONNECTED") ) then
		local raw = text or info.text;
		mark("icpt:" .. clean(which) .. ":" .. clean(raw));
		if ( info.OnShow ) then info.OnShow(); end        -- the stock dialog's own side effects when it opens
		HSSeamless_ShowError(which, raw, which == "DISCONNECTED" and "lost" or "login", ackFor(which));
		return;
	end
	return HS_Stock_DialogShow(which, text, data);
end

local HS_Stock_DialogOnEvent = GlueDialog_OnEvent;
function GlueDialog_OnEvent(self, event, arg1, arg2, arg3)
	if ( event == "UPDATE_STATUS_DIALOG" and S.pending and not GlueDialog:IsShown() ) then
		mark("upd:" .. clean(arg1) .. ":" .. clean(arg2));
		if ( arg1 and strlen(arg1) > 0 ) then
			if ( arg2 and arg2 ~= CANCEL ) then        -- the progress dialog turned into an error (its button changed)
				local p = S.pending;
				HSSeamless_ShowError(p.which, arg1, "login", GlueDialogTypes[p.which] and GlueDialogTypes[p.which].OnAccept);
			else
				S.pending.text = arg1;
				stepped(arg1);
			end
		end
		return;
	elseif ( event == "CLOSE_STATUS_DIALOG" ) then
		mark("close:" .. clean(S.pending and S.pending.which));
		S.pending = nil;
	end
	return HS_Stock_DialogOnEvent(self, event, arg1, arg2, arg3);
end

-- ---------------------------------------------------------------- login and character select

local HS_Login = HSFrontMenu_Login;
function HSFrontMenu_Login(profile, how)
	if ( not profile ) then return; end
	S.logins = S.logins + 1;
	HSSeamless_BeginPhase(CAPTION_LOGIN, how or "continue");
	return HS_Login(profile, how);
end

function HSSeamless_CharSelectWording()
	CharSelectRealmName:Hide();
	CharSelectChangeRealmButton:Hide();
	-- the first character moves up into the realm controls' place (the other buttons hang below it)
	CharSelectCharacterButton1:ClearAllPoints();
	CharSelectCharacterButton1:SetPoint("TOPLEFT", CharacterSelectCharacterFrame, "TOPLEFT", CS_FIRST_X,
		CS_FIRST_Y + CS_REALM_BLOCK);
	CharSelectEnterWorldButton:SetText(HS_FRIENDLY.play);
	CharacterSelectBackButton:SetText(HS_FRIENDLY.main_menu);
	CharacterSelectBackButton:SetWidth(math.max(100, CharacterSelectBackButton:GetTextWidth() + 30));
end

local HS_Stock_CharSelectOnShow = CharacterSelect_OnShow;
function CharacterSelect_OnShow()
	HS_Stock_CharSelectOnShow();
	HSSeamless_CharSelectWording();
end

function HSSeamless_OnEvent(self, event)
	if ( event == "CHARACTER_LIST_UPDATE" and S.phase and CURRENT_GLUE_SCREEN == "charselect" ) then
		HSSeamless_CharSelectWording();
		HSSeamless_EndPhase("charlist");
	end
end

-- What the test hook reads at character select (login.charselect_wording).
function HSSeamless_InspectCharSelect()
	mark(format("cs:screen=%s,realm_shown=%d,realm_text=%s,change_shown=%d,play=%s,back=%s,play_shown=%d,overlay=%d",
		clean(CURRENT_GLUE_SCREEN), CharSelectRealmName:IsVisible() and 1 or 0, clean(CharSelectRealmName:GetText()),
		CharSelectChangeRealmButton:IsVisible() and 1 or 0, clean(CharSelectEnterWorldButton:GetText()),
		clean(CharacterSelectBackButton:GetText()), CharSelectEnterWorldButton:IsVisible() and 1 or 0,
		HSLoadingOverlay:IsVisible() and 1 or 0));
	rec(format("Q:%s:%d:%d:%s:%s:%d:%d", clean(CURRENT_GLUE_SCREEN), CharSelectRealmName:IsVisible() and 1 or 0,
		CharSelectChangeRealmButton:IsVisible() and 1 or 0, clean(CharSelectEnterWorldButton:GetText()),
		clean(CharacterSelectBackButton:GetText()), CharSelectEnterWorldButton:IsVisible() and 1 or 0,
		HSLoadingOverlay:IsVisible() and 1 or 0));
	-- the list's layout: the panel's edges, the first button's top and the lowest button's bottom (all ten buttons)
	local low;
	for i = 1, MAX_CHARACTERS_DISPLAYED or 10 do
		local btn = _G["CharSelectCharacterButton" .. i];
		if ( btn and btn:GetBottom() and (not low or btn:GetBottom() < low) ) then low = btn:GetBottom(); end
	end
	local function r(x) return x and math.floor(x + 0.5) or -1; end
	mark(format("cs:layout:panel=%s-%s,first=%s,low=%s", tostring(CharacterSelectCharacterFrame:GetTop()),
		tostring(CharacterSelectCharacterFrame:GetBottom()), tostring(CharSelectCharacterButton1:GetTop()), tostring(low)));
	rec(format("Y:%d:%d:%d:%d", r(CharacterSelectCharacterFrame:GetTop()), r(CharSelectCharacterButton1:GetTop()),
		r(CharacterSelectCharacterFrame:GetBottom()), r(low)));
end

-- ---------------------------------------------------------------- test record out (HS_TEST only)

function HSSeamless_FlushLog(why)
	if ( not HS_TEST ) then return; end
	mark("flush:" .. clean(why));
	rec("W");
	rec("E" .. (HS_TEST_ERRORS or -1));
	local r = table.concat(S.rec, ";");
	if ( strlen(r) > REC_MAX ) then r = strsub(r, 1, REC_MAX - 1) .. "~"; end
	local keep = (string.match(GetSavedAccountList() or "", "HSBD%u+|") or "")
		.. (string.match(GetSavedAccountList() or "", "HSRN%d+|") or "");
	SetSavedAccountList(keep .. "HSLOG" .. r .. "|");
end

-- the front menu's own marks that the record needs (HSFrontMenu.lua: Continue, screenshots, the character list, the
-- test actions on the friendly error and character select)
local HS_Mark = HSFrontMenu_Mark;
function HSFrontMenu_Mark(event)
	HS_Mark(event);
	if ( not HS_TEST ) then return; end
	if ( event == "press:continue" ) then rec("C");
	elseif ( event == "shot" ) then rec("S");
	elseif ( strsub(event, 1, 9) == "charlist:" ) then rec("L" .. (string.match(event, "^charlist:(%d+)") or "?"));
	elseif ( strsub(event, 1, 11) == "press:play:" ) then rec("P:" .. clean(strsub(event, 12)));
	elseif ( strsub(event, 1, 12) == "press:retry:" ) then rec("r" .. strsub(event, 13));
	elseif ( event == "press:details" ) then rec("d");
	elseif ( event == "press:mainmenu" ) then rec("M");
	end
end

if ( HS_TEST ) then
	local HS_Stock_EnterWorld = EnterWorld;
	EnterWorld = function(...)
		S.enteredWorld = true;
		HSSeamless_FlushLog("enterworld");
		return HS_Stock_EnterWorld(...);
	end
end
