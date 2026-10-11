-- Moonglade dungeon party (openspec/changes/m4-party-preset, dungeon-companions "A dungeon party in one command").
--
-- /mg party        fills the party to five with companions for the roles the player's own talents don't cover
-- /mg party clear  dismisses them
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
			{ send = { "autogear", "co +threat,+wait for attack", "wait for attack time 3" } },
		} },
		rogue = { class = "rogue", token = "ROGUE", steps = {
			{ send = { "reset botAI" }, reply = "AI was reset to defaults" },
			{ send = { "talents spec combat pve" }, reply = "Picking combat pve" },
			{ send = { "autogear", "co +threat,+wait for attack", "wait for attack time 3" } },
		} },
		hunter = { class = "hunter", token = "HUNTER", steps = {
			{ send = { "reset botAI" }, reply = "AI was reset to defaults" },
			{ send = { "talents spec mm pve" }, reply = "Picking mm pve" },
			{ send = { "autogear", "co +threat,+wait for attack", "wait for attack time 3", "pet passive" } },
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
	usage = "Moonglade: /mg party fills your party with companions; /mg party clear dismisses them.",
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

-- Send the companion's step `n`; a step without a reply finishes the companion.
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
		if ( not run.known[name] ) then
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
		sendStep(run.step + 1);
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

SLASH_MOONGLADE1 = "/mg";
SlashCmdList["MOONGLADE"] = function(msg)
	local cmd = strlower(strtrim(msg or ""));
	if ( cmd == "party" ) then
		MoongladeParty_Start();
	elseif ( cmd == "party clear" ) then
		MoongladeParty_Clear();
	else
		say(TEXT.usage);
	end
end
