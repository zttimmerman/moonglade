# Proposal: Make comp.assists reliable (F23)

## Why
`comp.assists` ("Companion fights the master's target") fails about 1 run in 3–4 with "precondition: no creature attacked the master within 15 s". The round-3 verifier reproduced it on main's code (29594d0), so it predates in-client-login. In the failing run's combat events, the chosen creature was already fighting another unit (likely a bot) and killed it, so it never attacked the master. Flakiness is P0 (harness rules); it also muddies every future verification.

## What Changes
- The check's GIVEN becomes deterministic (as built, see design.md): a readiness gate waits until the creature is still, unhurt and out of combat, then the master lands on its *live* position (the root cause was teleporting to the wanderer's pick-time position); busy, waypoint-walking or crowded candidates are set aside before combat; new evidence requires the creature out of combat before the fight and the attacker to be the picked creature. Spawning a temporary creature was rejected (design.md).
- No change to companion behaviour, the server, modules or patches. The scenario text in `companion-bots` is unchanged. A new scenario requires the check to be stable.

## Capabilities
### Modified Capabilities
- `companion-bots`: adds a reliability requirement for the assist check.

## Spec-change log
(none yet)
