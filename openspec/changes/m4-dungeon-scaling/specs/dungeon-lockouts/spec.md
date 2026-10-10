## ADDED Requirements

### Requirement: No hourly dungeon limit
The worldserver SHALL NOT refuse a player entry to a 5-player dungeon because of the number of instances entered in the past hour. `AccountInstancesPerHour` SHALL be set high enough that a solo player can't reach it in normal play, with the value and its reason recorded in design.md §3.

#### Scenario: A sixth fresh dungeon in one hour
- **GIVEN** the probe character has entered, cleared the save of and left five different fresh copies of The Deadmines within the last hour (the core's stock limit is 5, `worldserver.conf.dist` `AccountInstancesPerHour`)
- **WHEN** it enters The Deadmines a sixth time
- **THEN** it arrives in a new copy (`SMSG_NEW_WORLD` for map 36, a sixth distinct instance id), and no `SMSG_TRANSFER_ABORTED` with `TRANSFER_ABORT_TOO_MANY_INSTANCES` arrives. With the stock value (counterexample profile `lockouts-stock`) the same sixth entry is refused with that abort
- Check: `lock.no_hourly_cap`

### Requirement: A finished dungeon is fresh next time
When a player enters a 5-player dungeon (normal or heroic) while their saved copy has every final encounter done (each `instance_encounters` row with `lastEncounterDungeon` ≠ 0 for that map and difficulty), and no non-bot player is inside it, mod-moonglade SHALL clear that save for the player and for every bot in their group first, so the entry creates a fresh copy. A save whose final encounter isn't done SHALL be kept, so a player who leaves mid-way (for example after dying) returns to their progress. Logging in inside a dungeon SHALL NOT clear anything. `Moonglade.Dungeons.FreshAfterClear = 0` SHALL restore stock behaviour.

#### Scenario: Finished normal dungeon resets on re-entry
- **GIVEN** the probe character and two companions in The Deadmines, with every encounter done (bosses killed in a fight, or with the GM `.die` command, which goes through the same kill-credit path; design.md §3) and the completion saved in `characters.instance.completedEncounters`
- **WHEN** the party leaves the dungeon and the probe enters it again
- **THEN** the probe arrives in a copy with a different instance id and a completed-encounter mask of 0, the companions follow into that same new copy, and `character_instance` holds no row for the old copy for the probe or either companion
- Check: `lock.fresh_after_clear`

#### Scenario: Unfinished dungeon keeps its progress
- **GIVEN** the probe character in The Deadmines with its first encounter done and its final encounter not done
- **WHEN** the probe leaves (by dying and releasing to the graveyard outside, or by walking out) and enters again
- **THEN** it arrives in the same instance id, and the first encounter is still done
- Check: `lock.partial_kept`

#### Scenario: Dismissing companions keeps an unfinished dungeon
- **GIVEN** a probe in a listed normal dungeon with companions, with one boss killed and the final boss alive
- **WHEN** the probe leaves the dungeon, dismisses every companion (the group breaks up), and enters again alone
- **THEN** it is in the same instance (same instance id), and the killed boss is still dead
- Check: `lock.partial_kept_dismissed`

#### Scenario: Finished heroic dungeon resets on re-entry (no daily lock)
- **GIVEN** the probe character at level 80 with heroic difficulty selected, in Utgarde Keep (heroic), with every encounter done and the save marked by the core as a heroic bind
- **WHEN** it leaves and enters again on the same server day
- **THEN** it arrives in a fresh heroic copy (new instance id, mask 0); and with `Moonglade.Dungeons.FreshAfterClear = 0` (profile `lockouts-stock`) the same re-entry returns to the old, cleared copy
- Check: `lock.heroic_fresh_after_clear`

#### Scenario: Unfinished heroic keeps its progress
- **GIVEN** the probe character in Utgarde Keep (heroic) with the first encounter done and the final one not done
- **WHEN** it leaves and enters again
- **THEN** it arrives in the same instance id, with the first encounter still done
- Check: `lock.heroic_partial_kept`

#### Scenario: Logging in inside a dungeon clears nothing
- **GIVEN** the probe character logged out inside a fully cleared Deadmines copy
- **WHEN** it logs in again
- **THEN** it is in the same instance id, and its bind is unchanged
- Check: `lock.login_inside_kept`

#### Scenario: Rerunning feels free
- **GIVEN** the relaxed lockouts
- **WHEN** Zach finishes a dungeon and goes back in
- **THEN** he gets a fresh dungeon without any lockout message, and nothing about it feels like an MMO restriction
- [human-verify] This is a feel check on the real client's messages and flow (for example, no "instance reset" spam). It is judged at the M4 human gate.

### Requirement: Companions can follow you into a fresh dungeon
A companion character that logged out inside an older copy of a dungeon SHALL be able to join the player's current copy, or the player outside, when summoned. It SHALL NOT be left stranded in the old copy.

#### Scenario: A companion saved in an old copy joins the new one
- **GIVEN** a pool companion that logged out inside an older instance of a listed dungeon, and a probe in a fresh instance of it
- **WHEN** the probe brings that companion into its party and summons it
- **THEN** within 30 s the companion is in the probe's instance (same instance id) and within 10 yd of the probe
- Check: `lock.companion_unstuck`
