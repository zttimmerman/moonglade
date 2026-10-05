-- Default (empty) profile list, shipped in the front-menu patch MPQ. At each start the launcher writes the real
-- list into a higher-priority archive inside the play client (harness/hslib/frontmenu.py); it holds logins, so it
-- is never committed and never written into client/.
HS_PROFILES = {};
HS_PROFILE_DEFAULT = nil;
HS_TEST = nil;
