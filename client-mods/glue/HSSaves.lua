-- Default (empty) save list, shipped in the front-menu patch MPQ (openspec/changes/front-menu-saves). While the game
-- runs, the launcher keeps the real list in a fixed-size, uncompressed slot of the same name in the higher-priority
-- archive patch-<locale>-5.MPQ inside the play client (harness/hslib/frontmenu.py), rewritten in place when the save
-- store changes (harness/hslib/charsaves.py); the glue screens read it again each time they load (start, and back from
-- the world). HS_SAVES_END repeats the generation last, so a torn read (a rewrite racing a glue load) is detectable.
HS_SAVES = { gen = "0", chars = {} };
HS_SAVES_END = "0";
