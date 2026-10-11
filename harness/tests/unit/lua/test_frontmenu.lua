-- Front-menu unit tests (client-mods/glue/HSFrontMenu.lua under the stub runtime; test-pyramid task 1.1).
-- Scenarios: login.fresh_menu_waits / login.cancel_shows_list (no login before Continue, simulated 60 s),
-- login.auto_login (Continue logs in the current profile), login.backdrop_choice (pick -> stored -> applied next
-- start), and the menu's keyboard rules (design.md of in-client-login, "Menu layout").

local PROFILES = {
	{ account = "HSPLAYER", password = "pw-one", name = "Main", context = "Arthas · Level 23 Paladin" },
	{ account = "HSALT", password = "pw-two", name = "Alt" },
}

local function show(T, opts)
	local w = T.boot(opts or { profiles = PROFILES, default = "HSPLAYER" })
	w.env.AccountLogin_OnShow(w.env.AccountLogin)        -- the client shows the login screen
	return w
end

return {
	menu_shows_continue_preselected = function(T)
		local w = show(T)
		local e = w.env
		T.eq(e.HSMenuPanel:IsShown(), true, "menu panel shown")
		T.eq(e.HSMenuSlot1:GetText(), "Continue", "first slot")
		T.eq(e.HSMenuSlot1.highlight, true, "Continue preselected")
		T.eq(e.HSContinueContextText:GetText(), "Arthas · Level 23 Paladin", "context line")
		for _, n in ipairs({ "AccountLoginAccountEdit", "AccountLoginPasswordEdit", "AccountLoginLoginButton" }) do
			T.eq(e[n]:IsShown(), false, n .. " hidden")
		end
	end,

	no_login_for_60s_without_continue = function(T)
		local w = show(T)
		w.advance(0.1, 600)                                  -- 60 s of frames on the simulated clock
		T.eq(#w.called("DefaultServerLogin"), 0, "logins in 60 s without input")
		T.eq(w.env.HSMenuPanel:IsShown(), true, "menu still shown")
		T.truthy(w.clock >= 59.99, "clock advanced 60 s")
	end,

	continue_logs_in_current_profile_once = function(T)
		local w = show(T)
		w.env.AccountLogin_OnKeyDown("ENTER")
		local logins = w.called("DefaultServerLogin")
		T.eq(#logins, 1, "logins after Enter")
		T.eq(logins[1].args[1], "HSPLAYER", "account")
		T.eq(logins[1].args[2], "pw-one", "password")
		T.eq(w.saved.accountName, "HSPLAYER", "saved account name")
	end,

	backdrop_choice_stored_and_applied_next_start = function(T)
		local w = show(T)
		local e = w.env
		e.AccountLogin_OnKeyDown("DOWN"); e.AccountLogin_OnKeyDown("DOWN")   -- Settings
		e.AccountLogin_OnKeyDown("ENTER")
		T.eq(e.HSMenuTitle:GetText(), "Settings", "settings menu")
		e.AccountLogin_OnKeyDown("DOWN")                                    -- Login screen: < name >
		e.AccountLogin_OnKeyDown("RIGHT")
		T.eq(w.saved.accountList, "HSBDTBC|", "stored backdrop")
		T.eq(e.AccountLogin.model, e.HS_BACKDROPS[2].model, "model switched live")
		-- next start: the stored choice comes back from the saved account list
		local w2 = show(T, { profiles = PROFILES, default = "HSPLAYER", saved = { accountList = w.saved.accountList } })
		T.eq(w2.env.AccountLogin.model, w2.env.HS_BACKDROPS[2].model, "backdrop applied at the next start")
		T.eq(w2.env.CurrentGlueMusic, "GS_BurningCrusade", "music of the stored backdrop")
		T.eq(#w2.called("SetSavedAccountList"), 0, "showing doesn't rewrite the choice")
	end,

	escape_on_main_moves_to_quit_and_keys_wrap = function(T)
		local w = show(T)
		local e = w.env
		e.AccountLogin_OnKeyDown("ESCAPE")
		T.eq(e.HSFrontMenuState.sel, 5, "Escape on main selects Quit")
		T.eq(#w.called("AccountLogin_Exit"), 0, "Escape doesn't quit by itself")
		e.AccountLogin_OnKeyDown("DOWN")
		T.eq(e.HSFrontMenuState.sel, 1, "Down from Quit wraps to Continue")
		e.AccountLogin_OnKeyDown("UP")
		T.eq(e.HSFrontMenuState.sel, 5, "Up from Continue wraps to Quit")
		T.eq(#w.called("DefaultServerLogin"), 0, "no login from moving")
	end,
}
