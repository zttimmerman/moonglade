# Spec Delta: client-connectivity

## ADDED Requirements

### Requirement: Resolution matches the display unless the player chose one
Before each launch, the client's resolution SHALL be set to match the display's aspect ratio when the player hasn't chosen one, and a player's own choice SHALL never be overwritten.

#### Scenario: Auto-chosen, re-chosen, never overriding the player
- **GIVEN** a play client with no resolution set, and a (real or injected) display of a known size
- **WHEN** the launcher prepares a launch, then the display size changes and it prepares again, then the player sets a different resolution and it prepares again
- **THEN** the first launch writes a resolution whose aspect ratio matches the display within 2% and that the client accepts; the second re-picks it to match the new display; the third leaves the player's value untouched; the native-vs-half-size default follows the decision recorded in design.md with its measurements
- Check: `client.resolution_default`

#### Scenario: Zach checks full screen
- **WHEN** Zach plays in full screen
- **THEN** characters aren't stretched
- [human-verify] Proportions on screen are judged by a person.
