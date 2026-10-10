## ADDED Requirements

### Requirement: The world pauses
When the pause is on, the server SHALL stop the whole world: creatures, playerbots, combat, spell casts and channels, aura durations and ticks, player and creature cooldowns, respawns, corpse decay and leash timers. The player's session SHALL stay connected. On resume, every one of these SHALL continue from the value it had when the pause began, and the client SHALL be sent the corrected aura durations and cooldowns. A cast in progress when the pause begins SHALL be cancelled (Zach, 2026-10-09).

#### Scenario: Pause stops combat
- **GIVEN** a fixture character in melee with a creature (probe session)
- **WHEN** the probe sends the pause addon message and waits 20 s
- **THEN** the character's and the creature's health are unchanged, no `SMSG_ATTACKERSTATEUPDATE` arrives during the pause, and after resume combat continues (a swing arrives within 5 s)
- Check: `pause.combat_frozen`

#### Scenario: Auras freeze
- **GIVEN** a buff with at least 30 s left on the character and a DoT on the creature
- **WHEN** the world is paused for 20 s and resumed
- **THEN** the buff's remaining time differs from before the pause by less than 1 s, the DoT dealt no ticks during the pause, and on resume the client receives the buff's remaining duration
- Check: `pause.aura_frozen`

#### Scenario: Cooldowns freeze and the client is told
- **GIVEN** a player spell on a 30 s or longer cooldown, and a creature ability on cooldown
- **WHEN** the world is paused for 20 s and resumed
- **THEN** the server's remaining cooldowns are unchanged (within 1 s), and on resume the client receives a cooldown correction for the player spell
- Check: `pause.cooldown_frozen`

#### Scenario: A cast is cancelled by pausing
- **GIVEN** the character casting a spell with a cast time of at least 2 s
- **WHEN** the pause begins mid-cast
- **THEN** the cast is cancelled (the client gets the cast-failed or interrupted notice), no spell effect lands during or after the pause, and the character is rooted until resume
- Check: `pause.cast_cancelled`

#### Scenario: Respawn and leash freeze
- **GIVEN** a killed creature with respawn time R s, and a creature chasing the character
- **WHEN** the world is paused for longer than R s and resumed
- **THEN** the dead creature hasn't respawned until R s of unpaused time have passed, and the chasing creature hasn't evaded
- Check: `pause.respawn_frozen`

#### Scenario: Bots freeze
- **GIVEN** a playerbot in the character's party that is moving or casting
- **WHEN** the world is paused for 20 s
- **THEN** its position, health, mana and current spell are unchanged during the pause, and it acts again after resume
- Check: `pause.bots_frozen`

#### Scenario: The connection survives a long pause
- **GIVEN** a paused probe session that sends only keep-alives
- **WHEN** 5 minutes pass
- **THEN** the session is still in the world and resumes normally
- Check: `pause.session_alive`

### Requirement: When pausing is allowed
Pausing SHALL be refused on a flight path, boat or zeppelin, with a message to the player. Logging out SHALL unpause first. With `Moonglade.Pause.Enable = 0`, pause requests SHALL do nothing.

#### Scenario: No pause while travelling
- **GIVEN** a character on a taxi flight
- **WHEN** a pause is requested
- **THEN** the world keeps running and the client gets a refusal message
- Check: `pause.refused_on_taxi`

#### Scenario: Logout from a paused world works
- **GIVEN** a paused world
- **WHEN** the character logs out
- **THEN** the world unpauses and the logout completes within the normal countdown
- Check: `pause.logout_unpauses`

#### Scenario: A lost client doesn't leave the world paused
- **GIVEN** a world paused by a connected client
- **WHEN** that client disconnects or is killed without logging out
- **THEN** the world unpauses within a few seconds (it does not wait for the session timeout, which runs on the frozen clock)
- Check: `pause.disconnect_unpauses`

#### Scenario: Pause disabled means no change
- **GIVEN** `Moonglade.Pause.Enable = 0`
- **WHEN** the pause addon message arrives
- **THEN** nothing pauses (combat continues)
- Check: `pause.disabled_noop`

### Requirement: Esc pauses in the real client
In the play client, opening the game menu with Esc SHALL pause the world and closing it SHALL resume, with the world view dimmed behind the menu and a visible "Paused" banner while paused (Zach, 2026-10-09: the client keeps drawing idle animations and effects during a pause, so the dimming makes it read as paused). There is no separate pause key and no auto-pause on losing focus (Zach, 2026-10-09).

#### Scenario: Esc pauses and resumes
- **GIVEN** the play client in the world (test account)
- **WHEN** Esc opens the game menu, then closes it
- **THEN** the server reports paused while the menu is open (`.moonglade pause status`), a screenshot shows the banner and the world view dimmed (the mean brightness of a screen region outside the menu and banner is clearly lower than the same region in a screenshot taken just before Esc), and after the menu closes the server reports running and that region's brightness is back to within a small tolerance of the pre-pause shot
- Check: `client.pause_menu`

#### Scenario: Resume looks right
- **GIVEN** a paused fight with moving creatures
- **WHEN** play resumes
- **THEN** no unit visibly teleports for more than a moment, and the buff and cooldown displays match the server
- [human-verify] Visual smoothness can't be asserted headlessly. The packets themselves are covered by the checks above.
