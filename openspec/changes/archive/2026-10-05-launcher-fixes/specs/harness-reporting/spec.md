# Spec Delta: harness-reporting

## ADDED Requirements

### Requirement: GUI path rule covers shared helpers
The GUI path rule SHALL include the shared harness modules the GUI checks depend on, so changing one of them runs the GUI checks in a full run.

#### Scenario: Changing a shared helper triggers the GUI checks
- **GIVEN** a change that touches only a shared helper used by the GUI session (e.g. `harness/hslib/procs.py`)
- **WHEN** the GUI path rule is evaluated
- **THEN** it reports a GUI path change, and a full run would run the GUI checks
- Check: `report.gui_tier_rule`
