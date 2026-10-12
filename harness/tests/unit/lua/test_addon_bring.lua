-- The Moonglade add-on's /mg bring and /mg dismiss (client-mods/addons/Moonglade/MoongladeParty.lua under the stub
-- runtime; openspec/changes/m4-alt-companions, companion-bots "The slash commands send the right command lists";
-- check id unit:lua:test_addon_bring.bring_and_dismiss_send_the_command_lists). The server side of the same lists
-- is alt.bring_dismiss's.

local FILES = { "Moonglade.lua", "MoongladeSaves.lua", "MoongladeParty.lua" }

local function boot(T, opts)
	opts = opts or {}
	opts.player, opts.files = "Hsprobe", FILES
	return T.boot_addon(opts)
end

local function sent(w)
	local out = {}
	for _, c in ipairs(w.called("SendChatMessage")) do
		table.insert(out, c.args[2] == "WHISPER" and ("w " .. c.args[4] .. ": " .. c.args[1]) or ("say " .. c.args[1]))
	end
	return out
end

local function messages(w)
	local out = {}
	for _, m in ipairs(w.env.DEFAULT_CHAT_FRAME.messages) do table.insert(out, m[1]) end
	return out
end

local function last(w) local m = messages(w); return m[#m] end

-- the server's side: the alt joins the party (SMSG_GROUP_LIST -> PARTY_MEMBERS_CHANGED) under its own spelling
local function join(w, name, token)
	table.insert(w.party, { name = name, token = token or "WARRIOR" })
	w.fire("PARTY_MEMBERS_CHANGED")
end

local FORBIDDEN = { "autogear", "talents", "init", "maintenance" }

return {
	bring_and_dismiss_send_the_command_lists = function(T)
		local w = boot(T, { class = "MAGE", talents = { 0, 0, 0 } })
		local plan = w.env.MG_BRING_PLAN
		-- the bring list, expanded from the plan table independently of the add-on's own code
		w.env.SlashCmdList.MOONGLADE("bring hsalt")
		T.eq(table.concat(sent(w), ";"), "say " .. plan.add .. "hsalt", "first the stock add, nothing whispered yet")
		T.eq(sent(w)[1], "say .playerbots bot add hsalt", "the stock command")
		join(w, "Hsalt")
		T.eq(sent(w)[2], "w Hsalt: " .. plan.steps[1].send[1], "then the reset, to the name it joined under")
		T.eq(sent(w)[2], "w Hsalt: reset botAI", "old orders forgotten")
		T.eq(#sent(w), 2, "the whole bring list")
		w.fire("CHAT_MSG_WHISPER", "AI was reset to defaults", "Hsalt")
		T.eq(last(w), string.format(w.env.MOONGLADE_BRING_TEXT.ready, "Hsalt"), "says it's ready")
		w.advance(1.0, 40)
		T.eq(#sent(w), 2, "nothing more once it answered")
		w.env.SlashCmdList.MOONGLADE("dismiss Hsalt")
		T.eq(sent(w)[3], "say .playerbots bot remove Hsalt", "the stock removal")
		T.eq(#sent(w), 3, "one command")
		for _, line in ipairs(sent(w)) do
			for _, bad in ipairs(FORBIDDEN) do
				T.truthy(not line:find(bad, 1, true), "nothing that changes an alt's gear or talents: " .. line)
			end
		end
		-- a missing name: the usage line, nothing sent
		local w2 = boot(T, {})
		w2.env.SlashCmdList.MOONGLADE("bring")
		w2.env.SlashCmdList.MOONGLADE("dismiss   ")
		T.eq(#w2.called("SendChatMessage"), 0, "nothing sent without a name")
		T.eq(messages(w2)[1], w2.env.MOONGLADE_BRING_TEXT.usage, "bring: usage")
		T.eq(messages(w2)[2], w2.env.MOONGLADE_BRING_TEXT.usage, "dismiss: usage")
	end,

	the_reset_is_sent_again_until_answered = function(T)
		local w = boot(T, {})
		w.env.SlashCmdList.MOONGLADE("bring Hsalt")
		join(w, "Hsalt")
		w.advance(1.0, 6)
		T.eq(table.concat(sent(w), ";"), "say .playerbots bot add Hsalt;w Hsalt: reset botAI;w Hsalt: reset botAI",
			"again after resend seconds")
		w.fire("CHAT_MSG_WHISPER", "AI was reset to defaults", "Someone")
		w.advance(1.0, 5)
		T.eq(#sent(w), 4, "an answer from someone else doesn't count")
		w.advance(1.0, 25)
		T.eq(last(w), string.format(w.env.MOONGLADE_BRING_TEXT.no_answer, "Hsalt", 30), "gives up at the timeout")
		local n = #sent(w)
		w.advance(1.0, 10)
		T.eq(#sent(w), n, "and stops sending")
	end,

	a_refused_or_unknown_character_never_joins = function(T)
		local w = boot(T, {})
		w.env.SlashCmdList.MOONGLADE("bring Stranger")
		w.advance(1.0, 31)
		T.eq(last(w), string.format(w.env.MOONGLADE_BRING_TEXT.timeout, "Stranger", 30), "says so")
		join(w, "Stranger")
		T.eq(#sent(w), 1, "a late joiner gets nothing")
		w.env.SlashCmdList.MOONGLADE("bring Other")
		T.eq(sent(w)[2], "say .playerbots bot add Other", "a new bring after the timeout")
		w.env.SlashCmdList.MOONGLADE("bring Third")
		T.eq(last(w), string.format(w.env.MOONGLADE_BRING_TEXT.busy, "Other"), "one at a time")
		T.eq(#sent(w), 2, "nothing sent while busy")
	end,

	party_never_whispers_a_brought_alt = function(T)
		local w = boot(T, { class = "MAGE", talents = { 0, 0, 0 } })
		w.env.SlashCmdList.MOONGLADE("bring Hsalt")
		w.env.SlashCmdList.MOONGLADE("party")      -- the add-on refuses only once the alt is in the party
		local n = #sent(w)
		T.eq(sent(w)[n], "say .playerbots bot addclass warrior", "the party call started")
		join(w, "Hsalt", "WARRIOR")                 -- the alt is a warrior, joining while a warrior tank is awaited
		for i = n + 1, #sent(w) do
			T.truthy(not sent(w)[i]:find("autogear", 1, true) and not sent(w)[i]:find("talents", 1, true),
				"no gear or talent command to the alt: " .. sent(w)[i])
		end
		T.eq(sent(w)[#sent(w)], "w Hsalt: reset botAI", "only the bring's reset")
		join(w, "Bot1", "WARRIOR")
		T.eq(sent(w)[#sent(w) - 0], "w Bot1: reset botAI", "the addclass companion gets the party steps")
		w.fire("CHAT_MSG_WHISPER", "AI was reset to defaults", "Bot1")
		T.eq(sent(w)[#sent(w)], "w Bot1: talents spec prot pve", "its spec")
	end,

	dismiss_forgets_the_alt = function(T)
		local w = boot(T, {})
		w.env.SlashCmdList.MOONGLADE("bring Hsalt")
		join(w, "Hsalt")
		T.truthy(w.env.MoongladeBring_IsAlt("hsalt"), "known as an alt, any case")
		w.env.SlashCmdList.MOONGLADE("dismiss hsalt")
		T.truthy(not w.env.MoongladeBring_IsAlt("Hsalt"), "forgotten")
		T.eq(sent(w)[#sent(w)], "say .playerbots bot remove hsalt", "as typed")
		w.advance(1.0, 10)
		T.eq(#sent(w), 3, "a dismissed alt's pending reset stops")
	end,
}
