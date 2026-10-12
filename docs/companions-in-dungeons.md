# Companions in dungeons: cheat sheet

## Get a party

| Type | What happens |
|---|---|
| `/mg party` | Your party fills up to five. You get the roles your talents don't cover: a warrior tank, a priest healer, a mage and a rogue. If you tank or heal yourself, a hunter comes instead of a second of your role. |
| `/mg party clear` | All your companions leave. |

- **Your role** comes from the talent tree with the most points. Protection warriors and paladins and blood death knights tank. Holy paladins, discipline and holy priests, and restoration shamans and druids heal. Everyone else, including a character with no talent points yet, deals damage. A feral druid counts as a damage dealer.
- **Each companion** joins at your level. It takes its role's talents and puts on gear for its level. You see a line in chat as each one joins, and "your party is ready" at the end.
- **Damage dealers wait for the tank.** They hold back for 3 seconds after a fight starts, and ease off before they pull a monster off the tank. The hunter's pet stays passive, so it never starts a fight.
- **`/mg party` needs an empty party.** Use `/mg party clear` first. If a companion doesn't join or answer within 30 seconds, the command stops and says so.

## Bring your own characters

| Type | What happens |
|---|---|
| `/mg bring <name>` | One of your other characters joins your party as a companion. It must be on your account and logged out. |
| `/mg dismiss <name>` | It leaves your party and logs out. |

- **It plays as itself.** It keeps its own level, gear, talents and spells. It isn't raised to your level, so a much lower character may struggle.
- **It keeps what it earns.** XP, levels, loot and gold from the trip are saved to that character when it leaves, as if you'd played it. Loading a save of your main character doesn't rewind it.
- **Its gear and talents are safe.** `/mg bring` and `/mg party` never send it gear or talent commands. If you whisper it `autogear`, it refuses: "You cannot use autogear on alt bots".
- **It starts fresh.** When it joins, it forgets orders you gave it on an earlier trip.
- **Where it is.** It joins from wherever it last logged out. Type `summon` to bring your companions to you.
- **Party first, then alts.** `/mg party` needs an empty party, so call your generated companions before you bring an alt.
- **Only your own characters.** A character from another account is refused ("You are not allowed to control bot").

## Give orders

- Say an order in party chat (`/p attack`) and every companion hears it.
- Whisper it (`/w Name stay`) and only that companion hears it.

| Order | What it does |
|---|---|
| `attack` | Attack your target. |
| `tank attack` | Only the tank answers: it attacks your target. |
| `pull` | The tank pulls your target to the party. |
| `pull rti` | The tank pulls the monster with its raid mark (skull unless you change it). |
| `rti <mark>` | Attack the monster with this raid mark first, e.g. `rti skull` (the default). |
| `rti cc <mark>` | Crowd-control the monster with this mark, e.g. a mage sheeps the `moon` (the default). In a 5-player dungeon, companions only crowd-control the marked monster. |
| `focus heal <name>` | The healer looks after this party member first. |
| `follow` / `stay` | Follow you, or stay where they are. |
| `flee` | Stop fighting and follow you. |
| `summon` | Companions come to you. |
| `ready` | Each companion answers a ready check. |
| `wait for attack time <seconds>` | How long damage dealers hold back at the start of a fight (0-99). |
| `max dps` | Damage dealers go all out. This also turns off easing off near the tank's threat. |
| `save mana` | Conserve mana. |
| `reset botAI` | Forget every order and go back to the defaults for its talents. |

## What companions do on their own

- **The tank** charges in and keeps the monsters on itself. It faces them away from the party.
- **Melee damage dealers** stand behind the monster.
- **The healer** heals and saves mana between fights. Inside dungeons it doesn't stop healing to deal damage.
- **Everyone** steps out of fire, poison clouds and other ground effects.
- **Boss tactics:** in some Outland and Northrend dungeons, companions also use boss-specific tactics. Hellfire Ramparts and Utgarde Keep are among them. Classic dungeons have none.
- **When you die**, the companions keep fighting if they can. If everyone dies, they release and run back with you.

These are stock mod-playerbots commands, as of our pinned version. Where each one comes from: `client-mods/addons/Moonglade/MoongladeParty.lua`, `openspec/changes/m4-dungeon-companions/design.md` §1 and `openspec/changes/m4-alt-companions/design.md`.
