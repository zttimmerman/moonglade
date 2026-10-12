-- Moonglade dungeon party (openspec/changes/m4-party-preset, dungeon-companions "A dungeon party in one command").
--
-- /mg party        fills the party to five with companions for the roles the player's own talents don't cover
-- /mg party clear  dismisses them
-- /mg bring <name>    one of the player's own characters joins the party as a companion (m4-alt-companions)
-- /mg dismiss <name>  sends it home
--
-- It only sends stock mod-playerbots commands, all of them taken from MG_PARTY_PLAN below. The harness reads the same
-- table (harness/hslib/partyplan.py) for dng.party_preset, so the check and the add-on can't drift: keep the table a
-- plain literal (strings, numbers, nested tables; no expressions).
--
-- Commands, as mod-playerbots handles them at our pin (vendor/mod-playerbots, design.md §1-§2 of
-- openspec/changes/m4-dungeon-companions):
-- - `.playerbots bot addclass <class>` (PlayerbotMgr.cpp:1083-1180; a non-GM may use it while
--   AiPlayerbot.AddClassCommand = 1, conf:140): an idle character of that class joins the party at the player's level;
-- - whispers to the companion, in steps (see `steps` below): `reset botAI` (ResetAiAction: forget orders from an
--   earlier session), `talents spec <premade spec>` (ChangeTalentsAction::SpecPick; names from
--   AiPlayerbot.PremadeSpecName.<class>.<n>, conf:1786-2132), then `autogear` and the role's extras: damage dealers
--   `co +threat,+wait for attack` (ThreatStrategy: ease off near the tank's threat; WaitForAttackStrategy: hold back
--   after a fight starts, tanks excluded on purpose, design.md §1.3) and `wait for attack time 3` (seconds, 0-99,
--   SetWaitForAttackTimeAction; a tuning knob, Zach's answer to question 2: damage dealers wait for the tank; the
--   dungeon runs of m4-dungeon-companions tune it), the hunter `pet passive` (PetsAction: its pet never starts a fight);
-- - `.playerbots bot remove *` (PlayerbotMgr.cpp:711 and the "*" group form): every companion in the party logs out.
--
-- The party (Zach, 2026-10-10, m4-dungeon-companions proposal question 1): a warrior tank, a priest healer, a mage and
-- a rogue; a player who tanks or heals gets a hunter instead of a second of their own role.
-- The player's role comes from the talent tree holding the most points (the same rule as mod-playerbots'
-- AiFactory::GetPlayerSpecTab), mapped like PlayerbotAI::IsTank / IsHeal (PlayerbotAI.cpp:2287-2360; tab order from
-- the *_TABS enums in PlayerbotAI.h, which is the talent frame's order): protection warrior and paladin and blood
-- death knight tank; holy paladin, discipline and holy priest, restoration shaman and druid heal; everything else,
-- and a character with no talent points yet, deals damage. (A feral druid counts as a damage dealer: mod-playerbots
-- only treats one as a tank in bear form.)

MG_PARTY_PLAN = {
	addclass = ".playerbots bot addclass ",
	clear = ".playerbots bot remove *",
	-- Each companion's whispers go in steps: a step's whispers are sent together, and the next step waits until the
	-- companion whispers back a line containing the step's `reply`. A companion runs the commands it receives in one
	-- update in no fixed order (PlayerbotAI::HandleCommands sets every command's trigger at once), and `reset botAI`
	-- and `talents spec` each reset its strategies (ResetAiAction.cpp:62-64 "AI was reset to defaults";
	-- ChangeTalentsAction.cpp:70-72 and SpecPick "Picking <spec>"), so gear and strategies come after both answers.
	companions = {
		tank = { class = "warrior", token = "WARRIOR", steps = {
			{ send = { "reset botAI" }, reply = "AI was reset to defaults" },
			{ send = { "talents spec prot pve" }, reply = "Picking prot pve" },
			{ send = { "autogear" } },
		} },
		healer = { class = "priest", token = "PRIEST", steps = {
			{ send = { "reset botAI" }, reply = "AI was reset to defaults" },
			{ send = { "talents spec holy pve" }, reply = "Picking holy pve" },
			{ send = { "autogear" } },
		} },
		mage = { class = "mage", token = "MAGE", steps = {
			{ send = { "reset botAI" }, reply = "AI was reset to defaults" },
			{ send = { "talents spec frost pve" }, reply = "Picking frost pve" },
			{ send = { "autogear", "co +threat,+wait for attack", "wait for attack time 3" }, reply = "Wait for attack time set to 3" },
		} },
		rogue = { class = "rogue", token = "ROGUE", steps = {
			{ send = { "reset botAI" }, reply = "AI was reset to defaults" },
			{ send = { "talents spec combat pve" }, reply = "Picking combat pve" },
			{ send = { "autogear", "co +threat,+wait for attack", "wait for attack time 3" }, reply = "Wait for attack time set to 3" },
		} },
		hunter = { class = "hunter", token = "HUNTER", steps = {
			{ send = { "reset botAI" }, reply = "AI was reset to defaults" },
			{ send = { "talents spec mm pve" }, reply = "Picking mm pve" },
			{ send = { "autogear", "co +threat,+wait for attack", "wait for attack time 3", "pet passive" }, reply = "Wait for attack time set to 3" },
		} },
	},
	-- the player's role -> the companions it gets, in the order they're added
	party = {
		damage = { "tank", "healer", "mage", "rogue" },
		tank = { "healer", "mage", "rogue", "hunter" },
		healer = { "tank", "mage", "rogue", "hunter" },
	},
	-- talent tree (1-3, the talent frame's order) -> role, by class; trees not listed deal damage
	roles = {
		WARRIOR = { [3] = "tank" },
		PALADIN = { [1] = "healer", [2] = "tank" },
		DEATHKNIGHT = { [1] = "tank" },
		PRIEST = { [1] = "healer", [2] = "healer" },
		SHAMAN = { [3] = "healer" },
		DRUID = { [3] = "healer" },
	},
};

local PLAN = MG_PARTY_PLAN;
local JOIN_TIMEOUT_S = 30;          -- per join and per answer: addclass takes 1-5 s when a character is free (comp.add_remove)
local TEXT = {
	usage = "Moonglade: /mg party fills your party with companions; /mg party clear dismisses them; "
		.. "/mg bring <name> brings one of your own characters along; /mg dismiss <name> sends it home.",
	no_talents = "Moonglade: your talents haven't loaded yet. Try /mg party again in a moment.",
	no_class = "Moonglade: can't tell your class yet. Try /mg party again in a moment.",
	in_group = "Moonglade: your party already has members. Use /mg party clear first.",
	in_raid = "Moonglade: /mg party works in a party, not a raid.",
	busy = "Moonglade: companions are still joining.",
	start = "Moonglade: you play the %s role; calling %d companions.",
	joined = "Moonglade: %s joined as your %s.",
	timeout = "Moonglade: no %s joined within %d seconds; stopped.",
	no_answer = "Moonglade: %s didn't answer within %d seconds; stopped.",
	done = "Moonglade: your party is ready.",
	clear = "Moonglade: dismissing your companions.",
};
MOONGLADE_PARTY_TEXT = TEXT;

local function say(msg)
	DEFAULT_CHAT_FRAME:AddMessage(msg, 1.0, 0.82, 0.0);
end

-- The player's role: "damage", "tank" or "healer"; nil and the reason when it can't be told.
function MoongladeParty_Role()
	local _, token = UnitClass("player");
	if ( not token ) then return nil, TEXT.no_class; end
	local best, bestPoints, any = nil, 0, false;
	for tab = 1, 3 do
		local name, _, points = GetTalentTabInfo(tab);
		if ( name ) then any = true; end
		if ( (points or 0) > bestPoints ) then best, bestPoints = tab, points; end
	end
	if ( not any ) then return nil, TEXT.no_talents; end
	local byTab = PLAN.roles[token];
	return (best and byTab and byTab[best]) or "damage";
end

-- The commands for a role, in order: { {kind = "command"|"whisper", text = ..., companion = ..., step = n}, ... }.
-- Whispers go to the companion that just joined (its name is known only then); a step's whispers wait for the previous
-- step's reply.
function MoongladeParty_Commands(role)
	local out = {};
	for _, key in ipairs(PLAN.party[role] or {}) do
		local c = PLAN.companions[key];
		table.insert(out, { kind = "command", text = PLAN.addclass .. c.class, companion = key });
		for n, step in ipairs(c.steps) do
			for _, w in ipairs(step.send) do
				table.insert(out, { kind = "whisper", text = w, companion = key, step = n });
			end
		end
	end
	return out;
end

-- The party being called: { role, queue = {keys}, i, waiting = key (until it joins), name, step (its step being
-- answered), since, known = {names} }
local run = nil;
local frame = CreateFrame("Frame", "MoongladePartyFrame");

local function partyNames()
	local names = {};
	for i = 1, GetNumPartyMembers() do
		local n = UnitName("party" .. i);
		if ( n ) then names[n] = "party" .. i; end
	end
	return names;
end

local addNext;

-- Send the companion's step `n`; the companion is finished after its last step (and that step's reply, if it has one).
local function sendStep(n)
	local c = PLAN.companions[run.queue[run.i]];
	local step = c.steps[n];
	run.step, run.since = n, 0;
	for _, w in ipairs(step.send) do
		SendChatMessage(w, "WHISPER", nil, run.name);
	end
	if ( not step.reply ) then
		addNext();
	end
end

addNext = function()
	run.i = run.i + 1;
	local key = run.queue[run.i];
	if ( not key ) then
		run = nil;
		frame:Hide();
		say(TEXT.done);
		return;
	end
	run.waiting, run.name, run.step, run.since = key, nil, nil, 0;
	SendChatMessage(PLAN.addclass .. PLAN.companions[key].class, "SAY");
end

local function onJoin()
	if ( not run or not run.waiting ) then return; end
	local key = run.waiting;
	local c = PLAN.companions[key];
	for name, unit in pairs(partyNames()) do
		if ( not run.known[name] and not MoongladeBring_IsAlt(name) ) then
			local _, token = UnitClass(unit);
			if ( token == c.token ) then
				run.known[name] = true;
				run.waiting, run.name = nil, name;
				say(format(TEXT.joined, name, key));
				sendStep(1);
				return;
			end
		end
	end
end

local function onWhisper(message, sender)
	if ( not run or not run.step or sender ~= run.name ) then return; end
	local step = PLAN.companions[run.queue[run.i]].steps[run.step];
	if ( step.reply and strfind(message, step.reply, 1, true) ) then
		if ( PLAN.companions[run.queue[run.i]].steps[run.step + 1] ) then
			sendStep(run.step + 1);
		else
			run.step = nil;
			addNext();
		end
	end
end

frame:RegisterEvent("PARTY_MEMBERS_CHANGED");
frame:RegisterEvent("CHAT_MSG_WHISPER");
frame:SetScript("OnEvent", function(self, event, message, sender)
	if ( event == "CHAT_MSG_WHISPER" ) then
		onWhisper(message, sender);
	else
		onJoin();
	end
end);
frame:SetScript("OnUpdate", function(self, elapsed)
	if ( not run ) then self:Hide(); return; end
	run.since = run.since + elapsed;
	if ( run.since >= JOIN_TIMEOUT_S ) then
		local c = PLAN.companions[run.queue[run.i]];
		if ( run.waiting ) then
			say(format(TEXT.timeout, c.class, JOIN_TIMEOUT_S));
		else
			say(format(TEXT.no_answer, run.name, JOIN_TIMEOUT_S));
		end
		run = nil;
		self:Hide();
	end
end);
frame:Hide();

function MoongladeParty_Start()
	if ( run ) then say(TEXT.busy); return; end
	if ( GetNumRaidMembers() > 0 ) then say(TEXT.in_raid); return; end
	if ( GetNumPartyMembers() > 0 ) then say(TEXT.in_group); return; end
	local role, why = MoongladeParty_Role();
	if ( not role ) then say(why); return; end
	run = { role = role, queue = PLAN.party[role], i = 0, known = {} };
	say(format(TEXT.start, role, #run.queue));
	frame:Show();
	addNext();
end

function MoongladeParty_Clear()
	run = nil;
	frame:Hide();
	say(TEXT.clear);
	SendChatMessage(PLAN.clear, "SAY");
end

-- ----------------------------------------------------------------------------- /mg bring, /mg dismiss
-- (openspec/changes/m4-alt-companions design.md §2, companion-bots "Your own characters as companions".) Stock
-- mod-playerbots commands, all taken from MG_BRING_PLAN (a plain literal: alt.bring_dismiss reads it through
-- harness/hslib/partyplan.py):
-- - `.playerbots bot add <name>` (PlayerbotMgr.cpp ProcessBotCommand :687-708, AddPlayerBot :85-147): a character of
--   the player's own account logs in as a companion (AiPlayerbot.AllowAccountBots = 1) and joins the party
--   (OnBotLogin's GroupInviteOperation); another account's character is refused ("You are not allowed to control
--   bot"), and the add-on sends nothing else then;
-- - after it joins, the whisper `reset botAI` (ResetAiAction.cpp:62-64, "AI was reset to defaults"): an alt's
--   strategies persist in playerbots_db_store from earlier trips (F16), so it starts from its defaults. A companion
--   that has just logged in can miss the first whisper (task 1.1 spike), so it is sent again every `resend` seconds
--   until the answer comes, up to the join timeout;
-- - `.playerbots bot remove <name>` (ProcessBotCommand :710-720): it leaves the party and logs out, saving what it
--   earned like a played character.
-- Never sent to an alt (its real gear and talents are the player's; proposal question 1): autogear, talents, init,
-- maintenance. /mg party whispers only the companions it called itself, never a brought alt (onJoin above), and
-- AiPlayerbot.AutoGearCommandAltBots = 0 makes the server refuse `autogear` to an alt in any case.
MG_BRING_PLAN = {
	add = ".playerbots bot add ",
	remove = ".playerbots bot remove ",
	steps = {
		{ send = { "reset botAI" }, reply = "AI was reset to defaults" },
	},
	resend = 5,
};

local BRING = MG_BRING_PLAN;
local BRING_TEXT = {
	usage = "Moonglade: /mg bring <name> brings one of your characters along; /mg dismiss <name> sends it home.",
	busy = "Moonglade: %s is still on the way.",
	joined = "Moonglade: %s joined your party.",
	ready = "Moonglade: %s is ready.",
	timeout = "Moonglade: %s didn't join within %d seconds. Is it one of your characters, and offline?",
	no_answer = "Moonglade: %s didn't answer within %d seconds.",
	dismiss = "Moonglade: sending %s home.",
};
MOONGLADE_BRING_TEXT = BRING_TEXT;

-- the alts brought this session, by lower-case name, and the one on its way; /mg party leaves them alone
local alts = {};
local bring = nil;      -- the alt on its way: { name (as typed), unitName once joined, step, since, sent }
function MoongladeBring_IsAlt(name)
	if ( not name ) then return false; end
	local n = strlower(name);
	return alts[n] ~= nil or (bring ~= nil and strlower(bring.name) == n);
end

-- The commands, in order: { {kind = "command"|"whisper", text = ..., step = n}, ... } (whispers go to the alt once it
-- has joined, a step after the previous step's answer).
function MoongladeBring_Commands(name)
	local out = { { kind = "command", text = BRING.add .. name } };
	for n, step in ipairs(BRING.steps) do
		for _, w in ipairs(step.send) do table.insert(out, { kind = "whisper", text = w, step = n }); end
	end
	return out;
end

local bringFrame = CreateFrame("Frame", "MoongladeBringFrame");

local function bringSend(n)
	bring.step, bring.sent = n, 0;
	for _, w in ipairs(BRING.steps[n].send) do
		SendChatMessage(w, "WHISPER", nil, bring.unitName);
	end
end

local function bringDone(msg)
	say(msg);
	bring = nil;
	bringFrame:Hide();
end

local function bringJoin()
	if ( not bring or bring.unitName ) then return; end
	for name in pairs(partyNames()) do
		if ( strlower(name) == strlower(bring.name) ) then
			bring.unitName = name;
			alts[strlower(name)] = true;
			say(format(BRING_TEXT.joined, name));
			bringSend(1);
			return;
		end
	end
end

local function bringWhisper(message, sender)
	if ( not bring or not bring.step or sender ~= bring.unitName ) then return; end
	local step = BRING.steps[bring.step];
	if ( step.reply and strfind(message, step.reply, 1, true) ) then
		if ( BRING.steps[bring.step + 1] ) then
			bringSend(bring.step + 1);
		else
			bringDone(format(BRING_TEXT.ready, bring.unitName));
		end
	end
end

bringFrame:RegisterEvent("PARTY_MEMBERS_CHANGED");
bringFrame:RegisterEvent("CHAT_MSG_WHISPER");
bringFrame:SetScript("OnEvent", function(self, event, message, sender)
	if ( event == "CHAT_MSG_WHISPER" ) then
		bringWhisper(message, sender);
	else
		bringJoin();
	end
end);
bringFrame:SetScript("OnUpdate", function(self, elapsed)
	if ( not bring ) then self:Hide(); return; end
	bring.since = bring.since + elapsed;
	if ( bring.step ) then
		bring.sent = bring.sent + elapsed;
		if ( bring.since >= JOIN_TIMEOUT_S ) then
			bringDone(format(BRING_TEXT.no_answer, bring.unitName, JOIN_TIMEOUT_S));
		elseif ( bring.sent >= BRING.resend ) then
			bringSend(bring.step);
		end
	elseif ( bring.since >= JOIN_TIMEOUT_S ) then
		bringDone(format(BRING_TEXT.timeout, bring.name, JOIN_TIMEOUT_S));
	end
end);
bringFrame:Hide();

function MoongladeBring_Start(name)
	if ( not name or name == "" ) then say(BRING_TEXT.usage); return; end
	if ( bring ) then say(format(BRING_TEXT.busy, bring.name)); return; end
	bring = { name = name, since = 0 };
	bringFrame:Show();
	SendChatMessage(BRING.add .. name, "SAY");
end

function MoongladeBring_Dismiss(name)
	if ( not name or name == "" ) then say(BRING_TEXT.usage); return; end
	if ( bring and strlower(bring.name) == strlower(name) ) then
		bring = nil;
		bringFrame:Hide();
	end
	alts[strlower(name)] = nil;
	say(format(BRING_TEXT.dismiss, name));
	SendChatMessage(BRING.remove .. name, "SAY");
end

SLASH_MOONGLADE1 = "/mg";
SlashCmdList["MOONGLADE"] = function(msg)
	local text = strtrim(msg or "");
	local cmd = strlower(text);
	local verb, arg = strmatch(text, "^(%S+)%s*(.-)$");
	verb = verb and strlower(verb);
	if ( cmd == "party" ) then
		MoongladeParty_Start();
	elseif ( cmd == "party clear" ) then
		MoongladeParty_Clear();
	elseif ( verb == "bring" ) then
		MoongladeBring_Start(arg);
	elseif ( verb == "dismiss" ) then
		MoongladeBring_Dismiss(arg);
	else
		say(TEXT.usage);
	end
end
