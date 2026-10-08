## 1. Power guard
- [x] 1.1 `power.py`: job default, battery guard with pause/resume/stop, test switches and their refusal
- [x] 1.2 Wire `--jobs` and `--ignore-power` into `hs build`, `hs bootstrap`/`./bootstrap`, `baseline.build_install`; log the job count and pauses
- [x] 1.3 Guard between checks in full/slow/gui runs; record pauses in results.json; timing guard subtracts paused time
- [x] 1.4 Checks `power.jobs_default`, `power.jobs_override`, `power.pause_resume`, `power.run_pause`, `power.stop_low`, `power.bootstrap_resume`, `power.switch_refusal`, each with a counterexample
- [x] 1.5 README/docs: one line on chargers and the pause
