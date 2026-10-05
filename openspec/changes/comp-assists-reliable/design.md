# Design: comp.assists reliability
- Lead (verifier round 3, main-comp1): the picked creature was in combat with guid 41 (another unit) and killed it; the master was never attacked.
- Options for the builder to evaluate:
  1. choose a creature that isn't in combat and has no other player/bot within N yd, re-checked just before engaging;
  2. spawn a temporary creature via GM at the staging point (despawn after), so nothing else has a claim on it;
  3. make the master initiate the fight in a way that doesn't count as the master attacking (if the spec allows). The spec says the master must not damage the creature, so be careful.
- Keep all existing counterexamples (passive companion, stay/not near, master attacks) failing.
