# Harness requirements (original KICKOFF section 5)

Preserved verbatim from the kickoff so it survives the KICKOFF.md rewrite.

## 5. Milestone 0 comes first: the verification harness

You cannot play the game, so you need instruments. Before any feature work, build `harness/`, which must be able to do the following, headless, from one command:

1. Build the server and modules from clean.
2. Start authserver and worldserver, wait for readiness, and shut down cleanly.
3. Send GM commands through worldserver's SOAP or RA interface and capture the output.
4. Run SQL assertions against the world and characters DBs.
5. Create a test account and character, and drive state changes via GM commands (level, teleport, add item, start quest) to exercise scenarios.
6. Investigate whether mod-playerbots can be scripted to run a character through content (quest, dungeon) as a behavioral test. Report findings; do not block M0 on it.
7. Emit a machine-readable report (`harness/results.json`) plus a human summary.

The harness is the oracle for the whole project. Treat a flaky harness as a P0 bug.
