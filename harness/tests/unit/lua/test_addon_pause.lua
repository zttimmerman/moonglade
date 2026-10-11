-- The Moonglade add-on's true-pause menu hooks, banner and dimming (client-mods/addons/Moonglade/Moonglade.lua under
-- the stub runtime; test-pyramid part 2). Scenario: client.pause_menu (its live half is journey.pause); the protocol
-- is MoongladePause.cpp's: the add-on whispers "MGPAUSE" "1" / "0" to its own character and the server answers
-- "paused" | "running" | "refused" | "disabled".

local function boot(T) return T.boot_addon({ player = "Hsprobe" }) end
local function sent(w)
	local out = {}
	for _, c in ipairs(w.called("SendAddonMessage")) do
		table.insert(out, table.concat({ c.args[1], c.args[2], c.args[3], c.args[4] }, " "))
	end
	return out
end
local function ack(w, msg) w.fire("CHAT_MSG_ADDON", "MGPAUSE", msg, "WHISPER", "Hsprobe") end

return {
	esc_asks_the_server_to_pause = function(T)
		local w = boot(T)
		w.menu:Show()
		T.eq(table.concat(sent(w), ";"), "MGPAUSE 1 WHISPER Hsprobe", "pause request on menu open")
		T.eq(w.env.MoongladePauseBanner:IsShown(), false, "no banner before the server answers")
		T.eq(w.env.MoongladePauseDim:IsShown(), false, "no dimming before the server answers")
	end,

	paused_answer_shows_banner_dims_and_lifts_menu = function(T)
		local w = boot(T)
		w.menu:Show(); ack(w, "paused")
		T.eq(w.env.MoongladePauseBanner:IsShown(), true, "banner")
		T.eq(w.env.MoongladePauseBanner.text:GetText(), "Paused", "banner text")
		T.eq(w.env.MoongladePauseDim:IsShown(), true, "dim layer")
		T.eq(w.env.MoongladePauseDim:GetFrameStrata(), "FULLSCREEN", "dim strata")
		T.eq(w.menu:GetFrameStrata(), "FULLSCREEN_DIALOG", "menu lifted above the dim")
		T.eq(w.env.MoongladePauseBanner:GetFrameStrata(), "TOOLTIP", "banner above everything")
	end,

	refused_or_disabled_never_claims_a_pause = function(T)
		for _, msg in ipairs({ "refused", "disabled", "running" }) do
			local w = boot(T)
			w.menu:Show(); ack(w, msg)
			T.eq(w.env.MoongladePauseBanner:IsShown(), false, msg .. ": banner")
			T.eq(w.env.MoongladePauseDim:IsShown(), false, msg .. ": dim")
			T.eq(w.menu:GetFrameStrata(), "DIALOG", msg .. ": menu strata unchanged")
		end
	end,

	closing_the_menu_resumes_and_restores = function(T)
		local w = boot(T)
		w.menu:Show(); ack(w, "paused"); w.menu:Hide()
		T.eq(table.concat(sent(w), ";"), "MGPAUSE 1 WHISPER Hsprobe;MGPAUSE 0 WHISPER Hsprobe", "pause then resume")
		T.eq(w.env.MoongladePauseBanner:IsShown(), false, "banner gone")
		T.eq(w.env.MoongladePauseDim:IsShown(), false, "dim gone")
		T.eq(w.menu:GetFrameStrata(), "DIALOG", "menu strata restored")
	end,

	late_paused_answer_after_close_shows_nothing = function(T)
		local w = boot(T)
		w.menu:Show(); w.menu:Hide(); ack(w, "paused")
		T.eq(w.env.MoongladePauseBanner:IsShown(), false, "no banner once the menu is closed")
		T.eq(w.env.MoongladePauseDim:IsShown(), false, "no dim once the menu is closed")
	end,

	logout_and_quit_resume_first = function(T)
		for _, b in ipairs({ "GameMenuButtonLogout", "GameMenuButtonQuit" }) do
			local w = boot(T)
			w.menu:Show(); ack(w, "paused")
			w.env[b].scripts.PreClick(w.env[b], "LeftButton")
			T.eq(sent(w)[2], "MGPAUSE 0 WHISPER Hsprobe", b .. ": resume before the click")
			T.eq(w.env.MoongladePauseBanner:IsShown(), false, b .. ": banner hidden")
		end
	end,

	leaving_the_world_clears_the_banner = function(T)
		local w = boot(T)
		w.menu:Show(); ack(w, "paused")
		w.fire("PLAYER_LEAVING_WORLD")
		T.eq(w.env.MoongladePauseBanner:IsShown(), false, "banner")
		T.eq(w.env.MoongladePauseDim:IsShown(), false, "dim")
	end,

	other_prefixes_are_ignored = function(T)
		local w = boot(T)
		w.menu:Show()
		w.fire("CHAT_MSG_ADDON", "OTHER", "paused", "WHISPER", "Hsprobe")
		T.eq(w.env.MoongladePauseBanner:IsShown(), false, "another add-on's message")
	end,

	wording_replaced_keeping_format_specifiers = function(T)
		local w = boot(T)
		T.eq(w.env.LOGOUT, "Exit to Menu", "LOGOUT")
		T.eq(w.env.GameMenuButtonLogout:GetText(), "Exit to Menu", "the menu button copied at load")
		T.eq(w.env.StaticPopupDialogs.CAMP.text, w.env.CAMP_TIMER, "the countdown popup's template")
		local _, n = string.gsub(w.env.CAMP_TIMER, "%%[ds]", "")
		T.eq(n, 2, "CAMP_TIMER keeps %d and %s")
	end,
}
