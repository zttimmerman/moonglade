## Why
M4's exit criterion is a listed set of dungeons that can be finished by one player plus N companions (KICKOFF §8). Its human gate is Zach playing two dungeons. Zach also wants companions that play like a competent party: the tank holds the monsters' attention, the healer heals, and nobody stands in fire. STATUS ("Single-player differentiators" item 4) adds pull assignments, crowd control on command, focus targets and raid marks, and asks that M4 first check what mod-playerbots already supports before building anything.

This is the second of three M4 changes (see `m4-dungeon-scaling`'s proposal for the split and the listed dungeons). It depends on that change's prototype (task 1.1), its dungeon driver (`harness/hslib/dungeon.py`) and its scaling settings.

## What Zach gets (plain language)
- **A dungeon party in one command.** Type `/mg party` and the game fills the party with companions for the roles you don't cover:
  - by default a tank, a healer and two damage dealers;
  - if you're a tank or a healer yourself, you get one more damage dealer instead of a second of your role.
  - They join at your level, in the right talent spec, wearing suitable gear.
- **Companions that know their job in a dungeon:**
  - the tank charges in first and keeps the monsters on itself;
  - damage dealers hold back a moment and ease off if they're about to pull a monster off the tank;
  - the healer keeps everyone alive and drinks between fights;
  - everyone steps out of fire, poison clouds and other ground effects.
  - In the Outland and Northrend dungeons on the list, they also use mod-playerbots' boss-specific tactics.
- **Orders, if you want them.** These are mod-playerbots chat commands, collected on one cheat sheet. You can:
  - mark a monster with a raid icon and say "kill skull first";
  - tell the mage to sheep the moon target;
  - tell the tank to pull;
  - ask the party to wait, follow or stay.
- **The listed dungeons can be finished.** Each one has been beaten by an automated party of companions, and The Deadmines solo, before you play.

## What Changes
- **Moonglade addon:** a `/mg party` slash command. It sends the stock mod-playerbots commands from one table in `client-mods/addons/Moonglade/MoongladeParty.lua`: `.playerbots bot addclass`, then whispers `talents spec …`, `autogear`, and per-role strategy tweaks. The damage dealers get `co +threat,+wait for attack` and `wait for attack time <s>`, so they let the tank start the fight and back off near its threat. These go per role because the "wait for attack" strategy would delay a tank too (`WaitForAttackStrategy.cpp:29-56`). There's no server code. `/mg party clear` dismisses the companions.
- **mod-playerbots config** (`harness/hslib/config.py`, `playerbots` section, every profile): the existing defaults `AutoAvoidAoe = 1` and `ApplyInstanceStrategies = 1` are stated explicitly, so a future default change upstream can't silently turn them off (design.md §2).
- **Harness:**
  - `dungeon.py` gains a full-run driver (boss by boss in encounter order, with wipe recovery);
  - the probe learns `SMSG_SPELLHEALLOG` and raid-icon packets;
  - new `dng.*` checks.
- **Docs:** a player cheat sheet, `docs/companions-in-dungeons.md`, covering the slash command, the orders and what the companions do on their own.
- **If the prototype shows a tactic gap in classic dungeons** (task 1.1 here), the fix goes through the check-upstream-first procedure. That means a mod-playerbots pin bump, a cherry-pick or our own `patches/playerbots/0002`, with ADR-0008's queue, and it becomes its own task with an estimate.

## What mod-playerbots already has (checked in source at our pin; design.md §1)
- **Roles by talent spec:**
  - protection, blood and feral-bear specs get `tank`, `tank assist`, `pull`, `pull back` and `tank face`;
  - healing specs get `heal` / `holy heal` and `save mana`;
  - everyone else gets `dps assist`, plus `behind` for melee.
- **On top of the role kits:** `avoid aoe` for everyone when the master is a real player; `cc` for mages, hunters, warlocks and balance/cat druids.
- **Instance tactics for TBC and WotLK 5-player dungeons only:**
  - including `tbc-ramp` (Hellfire Ramparts) and `wotlk-uk` (Utgarde Keep);
  - there are **no classic dungeon tactics**;
  - they switch on automatically on entering the map.
- **Orders:** `attack`, `tank attack`, `pull`, `pull rti`, `rti <icon>`, `rti cc <icon>`, `focus heal <name>`, `follow`, `stay`, `flee`, `wait for attack time <s>`, `max dps`, `save mana`, `summon`, `ready`, `reset botAI`.

## Open questions for Zach (plain gameplay terms; the default ships if you don't answer)
1. **Which companions does `/mg party` bring?** *Default:* a warrior tank, a priest healer, a mage and a rogue (the mage can also sheep a monster on command). If you're a tank or healer yourself, you get a third damage dealer (a hunter, with its pet kept passive so it doesn't pull) instead. Tell me if you'd rather have other classes, or a paladin tank or a druid healer.
2. **Should companions wait for the tank before attacking?** *Default:* yes. Damage dealers wait a few seconds after the tank starts (the tuning runs set the exact delay), and back off if they're close to stealing a monster. The alternative is everyone attacking at once: faster, but riskier.
3. **When you die in a dungeon, what should companions do?** *Default:* stock mod-playerbots behaviour. They keep fighting if they can; if everyone dies, they release and run back with you. The alternative is an instant "party wipe → everyone back at the entrance" rule (a new feature).
4. **The two dungeons you'll play for the M4 sign-off.** *Default:* The Deadmines with companions, and one listed dungeon near your character's current level, solo or with companions as you like.

**Zach's answers (2026-10-10):**
- Q1: the default party is fine, as long as he can play the tank or healer himself and the companion in that role is replaced. That is the default: a hunter takes the free damage slot.
- Q2: damage dealers wait for the tank (yes).
- Q3: stock wipe behaviour (yes).
- Q4: gate dungeons are the default (Deadmines with companions plus one near his level).

## Sources
- mod-playerbots source at pin `7bae1b5c` (paths in design.md). Config defaults from `vendor/mod-playerbots/conf/playerbots.conf.dist`.
- Dungeon facts: `m4-dungeon-scaling` design.md §6 (DBC/SQL rules). No game data values are written by this change.
- Upstream search (2026-10-10): design.md §1.3.

## Risks
- **Bot competence in classic dungeons.** There are no dungeon tactics there, so the generic kits decide. Bosses with adds or fear (VanCleef's adds, Amnennar's summons) may wipe a party of bots. The completion checks measure this, and a gap becomes a playerbots patch task.
- **Headless runs differ from real play.** Checks teleport the party from boss to boss and skip the walk through trash, doors and events (design.md §4). The full walk-through is the human gate.
- **Run time.** A full four-dungeon companion run is roughly 1–1.5 h, so those checks are slow-tier. The verifier runs each once, and the nightly sweep covers the repeats (design.md §5).

## M4 gate review items (for the human gate, not decisions now)
- The two performance-budget loosenings recorded in the M2 proposal (STATUS: 4.0 → 6.0 GB plus the 7.0 GB stack limit; "largest N that fits" → "default fits + larger sizes measured").
- Any spec loosening logged in the three M4 changes' spec-change logs.

## Spec-change log
- (none yet)
