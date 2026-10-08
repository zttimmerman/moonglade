## 1. Paths and app shape
- [ ] 1.1 `paths.py`: `MODE`, `APP`, `HOME`, the release layout table (design 1); record the dev path table from the parent commit as the `release.mode_resolution` golden; release refuses `HS_LANE=b`
- [ ] 1.2 Move every `P.ROOT`/`P.ENV`/`P.DATA`/`P.BREW` runtime path in hslib (stack, config, downloads, wineinst, clientinst, clientbuild, settings, backups, play, launch, bootstrap, app) onto the resolver; reviewed list of dev-only exceptions
- [ ] 1.3 `release.py`/`package.sh`: zip top level `Moonglade.app` with the payload at `Contents/Resources/Moonglade/`, bundle id `local.moonglade.release`; point `release-build`'s checks at the new root (list each edit and why it isn't a loosening)
- [ ] 1.4 Test switches (`HS_RELEASE_HOME`, `HS_RELEASE_PORTS`, `HS_RELEASE_DOWNLOAD_BASE`, `HS_FIRSTLAUNCH_TEST`, `HS_APP_TEST_CLIENT_PICK`) with refusal outside `--allow-test-release` + live harness run / the app test hook

## 2. Release stack
- [ ] 2.1 Payload `mysqld`/`mysqladmin`/`mysql`/`mysqldump` in stack and backups; release `my.cnf` (basedir, share dirs)
- [ ] 2.2 `config` release mode: `SourceDirectory` = `APP/sql` (verify against `DBUpdater.cpp`; fix packaging if the layout differs), `MySQLExecutable`, `OPENSSL_CONF`, module keys as `AC_*` via `module_env`, server cwd `HOME/run`
- [ ] 2.3 Launcher release mode: payload from the bundle, payload Python `-I -m hslib.play`, logs in HOME

## 3. First launch
- [ ] 3.1 Release step list in `bootstrap.py` + `hs first-launch` (preflight incl. disk need = client size + fixed part, client, data, wine, database, playclient, defaults); `home.json` step/attempt record
- [ ] 3.2 Client step: read-only source (pristine rules extended), clonefile on same volume else byte copy, manifest verify before "can delete"
- [ ] 3.3 Download progress events; per-step resume; plain errors for offline and damaged downloads
- [ ] 3.4 Database step: initialise, first start, safe reset rule (attempt record + no characters), phase timestamps
- [ ] 3.5 Launcher first-launch window: choose-folder, progress, error + choose again, "All set" + Play; second open goes to the play loop; closing mid-setup stops cleanly
- [ ] 3.6 Error catalog (design 5) wired to every failure path

## 4. Measure, check, document
- [ ] 4.1 Measure on a scratch HOME (×3): DB first start split (initialise / SQL import / bot setup), first-launch time per step, HOME size per folder (clone vs copy); put the fixed disk part into preflight with the citation; record all in design.md "Measurements"
- [ ] 4.2 Checks `release.mode_resolution`, `release.test_switches_refused`, `release.stack_from_payload`, `release.app_readonly`, `release.app_moved`, `release.play_standin`, `release.client_smoke`, `release.player_data_untouched` (each with a counterexample)
- [ ] 4.3 Checks `firstlaunch.flow`, `firstlaunch.second_open_plays`, `firstlaunch.source_untouched`, `firstlaunch.client_refusals`, `firstlaunch.preflight`, `firstlaunch.downloads`, `firstlaunch.kill_resume` (each with a counterexample; shared fixture per run)
- [ ] 4.4 `docs/setup.md`: "Using a release" (what goes where, how to remove everything); design.md "As built"
