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

These are stock mod-playerbots commands, as of our pinned version. Where each one comes from: `client-mods/addons/Moonglade/MoongladeParty.lua` and `openspec/changes/m4-dungeon-companions/design.md` §1.
