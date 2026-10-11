-- The Moonglade add-on's in-game Saves window, its server protocol and default keys
-- (client-mods/addons/Moonglade/MoongladeSaves.lua under the stub runtime; test-pyramid part 2). The protocol is
-- MoongladeSaves.cpp's (prefix "MGSAVE", whispered to the player's own character): LIST -> "E\t<gen>\t<i>\t<kind>\t
-- <when>\t<level>\t<zone>\t<label>" lines then "END\t<gen>\t<count>"; "OK\t<verb>\t<text>" / "NO\t<verb>\t<reason>".

local BOTH = { "Moonglade.lua", "MoongladeSaves.lua" }
local function boot(T, opts)
	opts = opts or {}
	opts.player, opts.files = "Hsprobe", opts.files or BOTH
	return T.boot_addon(opts)
end
local function sent(w, prefix)
	local out = {}
	for _, c in ipairs(w.called("SendAddonMessage")) do
		if c.args[1] == prefix then table.insert(out, c.args[2]) end
	end
	return out
end
local function reply(w, msg) w.fire("CHAT_MSG_ADDON", "MGSAVE", msg, "WHISPER", "Hsprobe") end
local function list(w, gen, rows)
	for i, r in ipairs(rows) do
		reply(w, table.concat({ "E", gen, tostring(i), r[1], r[2], r[3], r[4], r[5] }, "\t"))
	end
	reply(w, table.concat({ "END", gen, tostring(#rows) }, "\t"))
end
local ROWS = {
	{ "manual", "Today 10:02", "12", "Elwynn Forest", "Before the boss" },
	{ "quick", "Today 09:58", "12", "Goldshire", "Quicksave" },
	{ "auto", "Today 09:40", "11", "", "Rested" },
}

return {
	opening_lists_this_characters_saves = function(T)
		local w = boot(T)
		local e = w.env
		e.MoongladeSavesFrame:Show()
		T.eq(table.concat(sent(w, "MGSAVE"), ";"), "LIST", "the window asks for the list")
		T.eq(e.MoongladeSavesFrame.status:GetText(), e.MOONGLADE_SAVES_TEXT.loading_list, "reading status")
		list(w, "7", ROWS)
		T.eq(e.MoongladeSavesRow1.label:GetText(), "Before the boss", "first row label")
		T.eq(e.MoongladeSavesRow1.info:GetText(), "Saved  \194\183  Today 10:02  \194\183  Level 12  \194\183  Elwynn Forest", "first row info")
		T.eq(e.MoongladeSavesRow3.info:GetText(), "Autosave  \194\183  Today 09:40  \194\183  Level 11", "no zone, no separator")
		T.eq(e.MoongladeSavesRow4:IsShown(), false, "no fourth row")
		T.eq(e.MoongladeSavesLoad:IsEnabled(), nil, "Load needs a selection")
		T.eq(e.MoongladeSavesFrame.status:GetText(), "", "status cleared")
	end,

	empty_list_says_so = function(T)
		local w = boot(T)
		w.env.MoongladeSavesFrame:Show(); list(w, "1", {})
		T.eq(w.env.MoongladeSavesFrame.status:GetText(), "No saves yet.", "empty")
	end,

	delete_only_own_saves = function(T)
		local w = boot(T)
		local e = w.env
		e.MoongladeSavesFrame:Show(); list(w, "7", ROWS)
		e.MoongladeSavesRow2:Click()
		T.eq(e.MoongladeSavesDelete:IsEnabled(), nil, "Delete off for a quicksave")
		e.MoongladeSaves_Delete()
		T.eq(e.UIErrorsFrame.messages[1][1], "Only your own saves can be deleted.", "refusal text")
		T.eq(e.UIErrorsFrame.messages[1][3], 0.1, "shown as the red error text")
		T.eq(#w.called("StaticPopup_Show"), 0, "no confirmation")
		e.MoongladeSavesRow1:Click(); e.MoongladeSaves_Delete()
		T.truthy(w.popups.MOONGLADE_SAVES_DELETE, "confirmation for an own save")
		w.accept("MOONGLADE_SAVES_DELETE")
		T.eq(sent(w, "MGSAVE")[2], "DELETE 7 1", "delete by generation and index")
	end,

	load_confirms_then_sends_generation_and_index = function(T)
		local w = boot(T)
		local e = w.env
		e.MoongladeSavesFrame:Show(); list(w, "9", ROWS)
		e.MoongladeSavesRow3:Click()
		T.eq(e.MoongladeSavesLoad:IsEnabled(), 1, "Load on with a selection")
		e.MoongladeSaves_Load()
		T.eq(#sent(w, "MGSAVE"), 1, "nothing sent before the confirmation")
		T.truthy(string.find(w.popups.MOONGLADE_SAVES_LOAD.text, "Hsprobe goes back to Today 09:40", 1, true), "confirmation names who and when")
		w.accept("MOONGLADE_SAVES_LOAD")
		T.eq(sent(w, "MGSAVE")[2], "LOAD 9 3", "load by generation and index (never a save id)")
		T.eq(e.MoongladeSavesFrame.status:GetText(), "Loading\226\128\166", "loading status")
	end,

	save_sends_a_clean_name_and_refreshes_on_ok = function(T)
		local w = boot(T)
		local e = w.env
		e.MoongladeSavesFrame:Show(); list(w, "3", ROWS)
		e.MoongladeSavesName:SetText("Boss\tfight|one")
		e.MoongladeSaves_Save()
		T.eq(sent(w, "MGSAVE")[2], "SAVE manual Boss fight one", "tabs and pipes become spaces")
		reply(w, "OK\tSAVE\tSaved")
		T.eq(e.UIErrorsFrame.messages[1][1], "Saved.", "info text")
		T.eq(e.UIErrorsFrame.messages[1][3], 1.0, "shown in yellow")
		T.eq(sent(w, "MGSAVE")[3], "LIST", "the open window lists again")
	end,

	refusal_is_red_and_sticks_in_the_window = function(T)
		local w = boot(T)
		local e = w.env
		e.MoongladeSavesFrame:Show(); list(w, "3", ROWS)
		reply(w, "NO\tSAVE\tYou can't save during a fight.")
		T.eq(e.UIErrorsFrame.messages[1][1], "You can't save during a fight.", "the server's reason")
		T.eq(e.UIErrorsFrame.messages[1][3], 0.1, "red")
		T.eq(e.MoongladeSavesFrame.status:GetText(), "You can't save during a fight.", "kept in the window")
		T.eq(#sent(w, "MGSAVE"), 1, "a refusal doesn't re-list")
	end,

	pages_of_ten = function(T)
		local w = boot(T)
		local e = w.env
		local rows = {}
		for i = 1, 12 do rows[i] = { "auto", "t" .. i, "10", "", "Save " .. i } end
		e.MoongladeSavesFrame:Show(); list(w, "4", rows)
		T.eq(e.MoongladeSavesRow10.label:GetText(), "Save 10", "page 1 holds ten")
		T.eq(e.MoongladeSavesNext:IsEnabled(), 1, "next page")
		T.eq(e.MoongladeSavesPrev:IsEnabled(), nil, "no previous page")
		e.MoongladeSavesNext:Click()
		T.eq(e.MoongladeSavesRow2.label:GetText(), "Save 12", "page 2")
		T.eq(e.MoongladeSavesRow3:IsShown(), false, "page 2 holds two")
		T.eq(e.MoongladeSavesNext:IsEnabled(), nil, "last page")
	end,

	quick_keys_send_their_verbs = function(T)
		local w = boot(T)
		w.env.MoongladeSaves_QuickSave(); w.env.MoongladeSaves_QuickLoad()
		T.eq(table.concat(sent(w, "MGSAVE"), ";"), "QUICK;QLOAD", "verbs")
		T.eq(w.env.BINDING_NAME_MOONGLADE_QUICKSAVE, "Quicksave", "binding name")
	end,

	default_keys_once_and_only_if_free = function(T)
		local w = boot(T, { bindings = { F6 = "TOGGLESOMETHING" } })
		w.fire("PLAYER_ENTERING_WORLD")
		T.eq(w.env.GetBindingKey("MOONGLADE_QUICKSAVE"), nil, "F6 taken: quicksave left unbound")
		T.eq(w.env.GetBindingKey("MOONGLADE_QUICKLOAD"), "F7", "F7 free: quickload bound")
		T.eq(#w.called("SaveBindings"), 1, "saved once")
		T.eq(w.env.MoongladeSettings.keysSet, 1, "remembered")
		w.bindings.F7 = nil                       -- the player unbinds it later
		w.fire("UPDATE_BINDINGS")
		T.eq(w.env.GetBindingKey("MOONGLADE_QUICKLOAD"), nil, "the player's change sticks")
		local w2 = boot(T, { bindings = {} })
		w2.env.MoongladeSettings = { keysSet = 1 }
		w2.fire("PLAYER_ENTERING_WORLD")
		T.eq(#w2.called("SetBinding"), 0, "already set once: never again")
		T.eq(w2.env.MoongladeSavesKeys.set, "already", "recorded")
	end,

	no_binding_set_yet_waits = function(T)
		local w = boot(T, { binding_set = 0 })
		w.fire("PLAYER_ENTERING_WORLD")
		T.eq(#w.called("SetBinding"), 0, "nothing before the client has its bindings")
		T.eq(w.env.MoongladeSettings.keysSet, nil, "not marked done")
	end,

	the_window_keeps_the_menus_pause = function(T)
		local w = boot(T)
		local e = w.env
		e.GameMenuFrame:Show()
		e.GameMenuButtonMoongladeSaves:Click()
		T.eq(e.MoongladeSavesFrame:IsShown(), true, "window open")
		T.eq(e.GameMenuFrame:IsShown(), false, "menu closed")
		T.eq(table.concat(sent(w, "MGPAUSE"), ";"), "1;1", "paused; never resumed while the window shows")
		e.MoongladeSavesFrame:Hide()
		T.eq(sent(w, "MGPAUSE")[3], "0", "closing the window resumes")
		T.eq(#w.called("StaticPopup_Hide"), 2, "its dialogs close with it")
	end,

	menu_button_sits_above_exit = function(T)
		local w = boot(T)
		local e = w.env
		T.eq(e.GameMenuButtonMoongladeSaves:GetText(), "Saves", "label")
		local p = e.GameMenuButtonLogout.points[#e.GameMenuButtonLogout.points]
		T.eq(p[2], e.GameMenuButtonMoongladeSaves, "Exit to Menu anchored under Saves")
		T.eq(e.GameMenuFrame:GetHeight(), 250 + e.GameMenuButtonMoongladeSaves:GetHeight() + 1, "menu grows by the button's height + 1")
	end,
}
