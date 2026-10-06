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
-- 2. Friendly errors. An error dialog during that time (and a disconnect at any time) becomes a plain message with
--    Try again / Back to menu and a "Show details" toggle that reveals the client's original text. Both buttons first do
--    what the stock dialog's Okay button does (its OnAccept / OnCancel, e.g. StatusDialogClick), then retry the login
--    (HSFrontMenu_Continue) or return to the menu.
-- 3. Character select. The realm name (CharSelectRealmName) and Change Realm (CharSelectChangeRealmButton) are hidden;
--    Enter World (CharSelectEnterWorldButton) reads "Play" and Back (CharacterSelectBackButton) reads "Main Menu".
--    Their OnClick scripts (CharacterSelect_EnterWorld / CharacterSelect_Exit, CharacterSelect.xml) are unchanged.
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
--   E<n> Lua errors counted by the test error handler (at the hand-out). Lower case: the test hook's own clicks
--   (d details, r<1|0> Try again while the error was shown or not).
-- Without HS_TEST nothing is recorded and nothing extra is stored.

local ELLIPSIS = "\226\128\166";                -- UTF-8 "..." (one glyph)
local CAPTION_LOGIN = "Entering Azeroth" .. ELLIPSIS;
local CAPTION_LIST = "Loading" .. ELLIPSIS;
local BACK_AFTER_S = 15;                        -- the overlay offers "Back to menu" if a login takes this long

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
            pending = nil, err = nil, logins = 0, visibleSeen = {}, enteredWorld = false };
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

function HSSeamless_BeginPhase(caption, why)
	if ( HSErrorDialog:IsShown() ) then HSErrorDialog:Hide(); end
	S.phase = why or "?";
	S.since = S.t;
	S.frames, S.overlayFrames, S.dialogFrames, S.intercepted = 0, 0, 0, 0;
	S.visibleSeen = {};
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
	-- a slow sweep on the loading bar; the "Back to menu" link once a login takes unusually long
	local f = (S.t * 0.6) % 1;
	HSLoadingOverlayBarFill:SetWidth(math.max(1, 290 * (f < 0.5 and f * 2 or (1 - f) * 2)));
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
end

-- ---------------------------------------------------------------- test record out (HS_TEST only)

function HSSeamless_FlushLog(why)
	if ( not HS_TEST ) then return; end
	mark("flush:" .. clean(why));
	rec("W");
	rec("E" .. (HS_TEST_ERRORS or -1));
	local r = table.concat(S.rec, ";");
	if ( strlen(r) > REC_MAX ) then r = strsub(r, 1, REC_MAX - 1) .. "~"; end
	local keep = string.match(GetSavedAccountList() or "", "HSBD%u+|") or "";
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
