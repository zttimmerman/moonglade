-- The Moonglade add-on's /mg party (client-mods/addons/Moonglade/MoongladeParty.lua under the stub runtime;
-- openspec/changes/m4-party-preset, dungeon-companions "The slash command sends the plan"; check id
-- unit:lua:test_addon_party). The server side (mod-playerbots answering these commands) is dng.party_preset's.

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

-- Run /mg party and answer the way the server does: an addclass brings a character of that class into the party
-- (SMSG_GROUP_LIST -> PARTY_MEMBERS_CHANGED), and a companion whispers back "AI was reset to defaults" to `reset botAI`
-- and "Picking <spec>" to `talents spec <spec>` (ResetAiAction.cpp:64, ChangeTalentsAction.cpp:152). opts.silent:
-- companions never answer. Returns the names that joined, in order.
local TOKEN = { warrior = "WARRIOR", priest = "PRIEST", mage = "MAGE", rogue = "ROGUE", hunter = "HUNTER" }
local function call_party(w, opts)
	opts = opts or {}
	w.env.SlashCmdList.MOONGLADE("party")
	local names, done, n = {}, 0, 0
	while done < #w.called("SendChatMessage") do
		done = done + 1
		local c = w.called("SendChatMessage")[done]
		local msg, kind, to = c.args[1], c.args[2], c.args[4]
		local cls = kind == "SAY" and msg:match("^%.playerbots bot addclass (%a+)$")
		if cls then
			n = n + 1
			table.insert(w.party, { name = "Bot" .. n, token = TOKEN[cls] })
			table.insert(names, "Bot" .. n)
			w.fire("PARTY_MEMBERS_CHANGED")
		elseif kind == "WHISPER" and not opts.silent then
			if msg == "reset botAI" then w.fire("CHAT_MSG_WHISPER", "AI was reset to defaults", to) end
			local spec = msg:match("^talents spec (.+)$")
			if spec then w.fire("CHAT_MSG_WHISPER", "Picking " .. spec, to) end
			local secs = msg:match("^wait for attack time (%d+)$")
			if secs then w.fire("CHAT_MSG_WHISPER", "Wait for attack time set to " .. secs .. " seconds", to) end
		end
	end
	return names
end

-- The expected commands, expanded from the plan table independently of the add-on's own code.
local function expected(plan, role, names)
	local out = {}
	for i, key in ipairs(plan.party[role]) do
		local c = plan.companions[key]
		table.insert(out, "say .playerbots bot addclass " .. c.class)
		for _, step in ipairs(c.steps) do
			for _, wh in ipairs(step.send) do table.insert(out, "w " .. names[i] .. ": " .. wh) end
		end
	end
	return out
end

local function same(T, got, want, what)
	T.eq(#got, #want, what .. ": number of commands")
	for i = 1, #want do T.eq(got[i], want[i], what .. ": command " .. i) end
end

local function messages(w)
	local out = {}
	for _, m in ipairs(w.env.DEFAULT_CHAT_FRAME.messages) do table.insert(out, m[1]) end
	return out
end

return {
	damage_dealer_gets_tank_healer_mage_rogue = function(T)
		local w = boot(T, { class = "WARRIOR", talents = { 11, 0, 0 } })      -- arms
		local names = call_party(w)
		local got = sent(w)
		same(T, got, expected(w.env.MG_PARTY_PLAN, "damage", names), "damage dealer")
		T.eq(got[1], "say .playerbots bot addclass warrior", "a tank first")
		T.eq(got[2], "w Bot1: reset botAI", "old orders forgotten first")
		T.eq(got[3], "w Bot1: talents spec prot pve", "the tank's spec")
		T.eq(got[4], "w Bot1: autogear", "then its gear, after the spec")
		T.eq(got[5], "say .playerbots bot addclass priest", "then the healer")
		local classes = {}
		for _, line in ipairs(got) do
			local cls = line:match("addclass (%a+)$")
			if cls then table.insert(classes, cls) end
		end
		T.eq(table.concat(classes, ","), "warrior,priest,mage,rogue", "the party")
		T.eq(messages(w)[#messages(w)], w.env.MOONGLADE_PARTY_TEXT.done, "says the party is ready")
	end,

	damage_dealers_wait_for_the_tank_and_tanks_dont = function(T)
		local w = boot(T, { class = "MAGE", talents = { 0, 0, 0 } })            -- no points yet: a damage dealer
		call_party(w)
		local got = table.concat(sent(w), "\n")
		T.truthy(got:find("w Bot3: co +threat,+wait for attack\nw Bot3: wait for attack time 3", 1, true), "mage waits")
		T.truthy(got:find("w Bot4: co +threat,+wait for attack\nw Bot4: wait for attack time 3", 1, true), "rogue waits")
		T.truthy(not got:find("w Bot1: co", 1, true), "the tank gets no waiting strategy")
		T.truthy(not got:find("w Bot2: co", 1, true), "nor the healer")
	end,

	tank_player_gets_a_hunter_instead_of_a_tank = function(T)
		local w = boot(T, { class = "WARRIOR", talents = { 0, 5, 31 } })       -- protection
		local names = call_party(w)
		local got = sent(w)
		same(T, got, expected(w.env.MG_PARTY_PLAN, "tank", names), "tank player")
		T.eq(got[1], "say .playerbots bot addclass priest", "a healer first, no tank")
		for _, line in ipairs(got) do T.truthy(not line:find("addclass warrior", 1, true), "no warrior tank: " .. line) end
		T.eq(got[#got], "w Bot4: pet passive", "the hunter's pet never starts a fight")
	end,

	healer_player_gets_a_tank_and_a_hunter = function(T)
		local w = boot(T, { class = "PRIEST", talents = { 3, 40, 0 } })        -- holy
		local names = call_party(w)
		local got = sent(w)
		same(T, got, expected(w.env.MG_PARTY_PLAN, "healer", names), "healer player")
		local classes = {}
		for _, line in ipairs(got) do
			local cls = line:match("addclass (%a+)$")
			if cls then table.insert(classes, cls) end
		end
		T.eq(table.concat(classes, ","), "warrior,mage,rogue,hunter", "no second healer")
	end,

	roles_follow_the_talent_tree_with_most_points = function(T)
		local cases = {
			{ "PALADIN", { 31, 5, 0 }, "healer" }, { "PALADIN", { 0, 31, 5 }, "tank" }, { "PALADIN", { 0, 5, 31 }, "damage" },
			{ "PRIEST", { 31, 5, 0 }, "healer" }, { "PRIEST", { 0, 5, 31 }, "damage" },
			{ "DRUID", { 0, 0, 31 }, "healer" }, { "DRUID", { 0, 31, 0 }, "damage" },
			{ "SHAMAN", { 0, 0, 31 }, "healer" }, { "DEATHKNIGHT", { 31, 0, 0 }, "tank" },
			{ "WARRIOR", { 0, 31, 5 }, "damage" }, { "HUNTER", { 0, 0, 31 }, "damage" },
		}
		for _, c in ipairs(cases) do
			local w = boot(T, { class = c[1], talents = c[2] })
			T.eq(w.env.MoongladeParty_Role(), c[3], c[1] .. " " .. table.concat(c[2], "/"))
		end
	end,

	clear_sends_the_stock_removal = function(T)
		local w = boot(T, { class = "WARRIOR", talents = { 11, 0, 0 } })
		call_party(w)
		local n = #w.called("SendChatMessage")
		w.env.SlashCmdList.MOONGLADE("party clear")
		local got = sent(w)
		T.eq(#got, n + 1, "one command")
		T.eq(got[#got], "say .playerbots bot remove *", "every companion in the party logs out")
	end,

	unknown_role_sends_nothing_and_says_why = function(T)
		local w = boot(T, { class = "WARRIOR" })                              -- talents not loaded yet
		w.env.SlashCmdList.MOONGLADE("party")
		T.eq(#w.called("SendChatMessage"), 0, "nothing sent")
		T.eq(messages(w)[1], w.env.MOONGLADE_PARTY_TEXT.no_talents, "says why")
		local w2 = boot(T, { talents = { 0, 0, 0 } })                         -- class unknown
		w2.env.SlashCmdList.MOONGLADE("party")
		T.eq(#w2.called("SendChatMessage"), 0, "nothing sent without a class")
		T.eq(messages(w2)[1], w2.env.MOONGLADE_PARTY_TEXT.no_class, "says why")
	end,

	refuses_when_the_party_has_members = function(T)
		local w = boot(T, { class = "WARRIOR", talents = { 11, 0, 0 }, party = { { name = "Friend", token = "MAGE" } } })
		w.env.SlashCmdList.MOONGLADE("party")
		T.eq(#w.called("SendChatMessage"), 0, "nothing sent")
		T.eq(messages(w)[1], w.env.MOONGLADE_PARTY_TEXT.in_group, "says why")
	end,

	steps_wait_for_the_companions_answer = function(T)
		local w = boot(T, { class = "WARRIOR", talents = { 11, 0, 0 } })
		call_party(w, { silent = true })
		T.eq(table.concat(sent(w), ";"), "say .playerbots bot addclass warrior;w Bot1: reset botAI",
			"nothing more until the companion answers the reset")
		w.fire("CHAT_MSG_WHISPER", "AI was reset to defaults", "Someone else")
		T.eq(#w.called("SendChatMessage"), 2, "an answer from someone else doesn't count")
		w.fire("CHAT_MSG_WHISPER", "AI was reset to defaults", "Bot1")
		T.eq(sent(w)[3], "w Bot1: talents spec prot pve", "then the spec")
		w.fire("CHAT_MSG_WHISPER", "Spec prot pve not found", "Bot1")
		T.eq(#w.called("SendChatMessage"), 3, "a failed spec pick doesn't go on")
		w.advance(1.0, 31)
		T.eq(messages(w)[#messages(w)], string.format(w.env.MOONGLADE_PARTY_TEXT.no_answer, "Bot1", 30), "says so")
	end,

	a_companion_that_never_joins_stops_the_call = function(T)
		local w = boot(T, { class = "WARRIOR", talents = { 11, 0, 0 } })
		w.env.SlashCmdList.MOONGLADE("party")
		T.eq(#w.called("SendChatMessage"), 1, "the first addclass")
		w.advance(1.0, 31)
		T.eq(messages(w)[#messages(w)], string.format(w.env.MOONGLADE_PARTY_TEXT.timeout, "warrior", 30), "says so")
		table.insert(w.party, { name = "Late", token = "WARRIOR" })
		w.fire("PARTY_MEMBERS_CHANGED")
		T.eq(#w.called("SendChatMessage"), 1, "a late joiner gets nothing")
		w.env.SlashCmdList.MOONGLADE("")
		T.eq(messages(w)[#messages(w)], w.env.MOONGLADE_PARTY_TEXT.usage, "usage")
	end,

	a_joiner_of_another_class_is_not_taken_for_the_companion = function(T)
		local w = boot(T, { class = "WARRIOR", talents = { 11, 0, 0 } })
		w.env.SlashCmdList.MOONGLADE("party")
		table.insert(w.party, { name = "Stranger", token = "MAGE" })
		w.fire("PARTY_MEMBERS_CHANGED")
		T.eq(#w.called("SendChatMessage"), 1, "no whispers to a mage while a warrior is awaited")
	end,
}
