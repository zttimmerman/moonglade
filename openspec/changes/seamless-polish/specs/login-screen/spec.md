## ADDED Requirements

### Requirement: The loading overlay shows real, forward-only progress
While the "Entering Azeroth…" overlay is up, its bar SHALL reflect the login's real progress:
- it advances when the client reports the next login step;
- it never decreases during one load;
- it is full when the character list appears;
- it starts from empty at the beginning of every load, including a load right after returning to the main menu.

#### Scenario: Progress only moves forward and ends full
- GIVEN a scratch patched play client at the front menu
- WHEN the player presses Continue and the login reaches character select
- THEN every recorded bar value during that load is greater than or equal to the one before it
- AND the bar advanced at least once in response to a client-reported login step (not only with time)
- AND the last value before the overlay hides is full
- Check: `login.overlay_progress`

#### Scenario: A quick second load starts from zero and never goes backwards
- GIVEN the player has just reached character select through the overlay
- WHEN the player clicks Main Menu and presses Continue again within a few seconds
- THEN the second load's first recorded bar value is empty, at or below the start value
- AND the second load's values never decrease and end full
- Check: `login.overlay_progress_reload`

### Requirement: The character list starts at the top of its panel
With the realm name and Change Realm hidden, the first character in the character select list SHALL sit at the top of the list's panel. The gap between the panel's top edge and the first character is no larger than stock WoW's gap when the realm controls are shown, minus their height.

#### Scenario: No empty gap above the first character
- GIVEN character select on a patched play client with at least one character
- WHEN the screen is shown
- THEN the vertical gap between the character list panel's top edge and the first character button's top edge is within a small tolerance of the stock layout's gap below the realm controls
- AND the realm name and Change Realm stay hidden, and every character button stays inside the panel
- Check: `login.charselect_layout`
