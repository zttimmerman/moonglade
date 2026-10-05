# Proposal: Make comp.assists reliable (F23)

## Why
`comp.assists` ("Companion fights the master's target") fails about 1 run in 3–4 with "precondition: no creature attacked the master within 15 s". The round-3 verifier reproduced it on main's code (29594d0), so it predates in-client-login. In the failing run's combat events, the chosen creature was already fighting another unit (likely a bot) and killed it, so it never attacked the master. Flakiness is P0 (harness rules); it also muddies every future verification.

## What Changes
- The check's GIVEN ("a hostile creature the master is fighting") becomes deterministic: the creature must not already be engaged and must have no other players or bots near it, or be freshly spawned for the check, so it reliably engages the master.
- No change to companion behaviour, the server, modules or patches. The scenario text in `companion-bots` is unchanged. A new scenario requires the check to be stable.

## Capabilities
### Modified Capabilities
- `companion-bots`: adds a reliability requirement for the assist check.

## Spec-change log
(none yet)
