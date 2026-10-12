"""Process lookups the release gate checks rely on, under a release HOME with a space in its path (the real one is
~/Library/Application Support/Moonglade): the Wow.exe client matcher (launch.is_wow_client) and app_play's
server-of-this-HOME test (firstlaunch_checks.server_of_home)."""
from pathlib import Path

from hslib import launch
from hslib.checks.firstlaunch_checks import server_of_home

from .base import UnitCase

HOME = Path("/tmp/u/Library/Application Support/Moonglade")


class WowClientMatch(UnitCase):
    def test_client_paths(self):
        for cmd in ("Wow.exe",
                    r"Z:\Users\x\repo\env\client-play\Wow.exe",
                    r"Z:\Users\x\Library\Application Support\Moonglade\client-play\Wow.exe",
                    r"Z:\Users\x\out\fl-1\h s\client-play\Wow.exe -console",
                    r"/opt/w/bin/wine64-preloader Z:\a b\c\Wow.exe",
                    "/opt/w/bin/wine64-preloader Wow.exe"):
            self.assertTrue(launch.is_wow_client(cmd), cmd)

    def test_not_the_client(self):
        # counterexamples: launchers and other programs naming Wow.exe, with or without spaces in the paths
        for cmd in (r"C:\windows\system32\start.exe /exec Z:\h s\client-play\Wow.exe",
                    r"C:\windows\system32\start.exe /exec Wow.exe",
                    r"Z:\h s\client-play\WowError.exe",
                    r"Z:\h s\client-play\Wow.exe.bak",
                    r"C:\windows\system32\cmd.exe /c Z:\h s\Wow.exe",
                    "/opt/w/bin/wine Wow.exe",
                    "/opt/w/bin/wine64-preloader /opt/w/bin/wine64 Wow.exe",
                    r"Z:\h s\client-play\notwow.exe"):
            self.assertFalse(launch.is_wow_client(cmd), cmd)

    def test_wow_processes_filters(self):
        procs = [{"pid": 1, "cmd": r"Z:\h s\client-play\Wow.exe"},
                 {"pid": 2, "cmd": r"C:\windows\system32\start.exe /exec Z:\h s\client-play\Wow.exe"}]
        self.assertEqual([p["pid"] for p in launch.wow_processes(procs)], [1])


class ServerOfHome(UnitCase):
    def test_relative_conf_matched_by_env(self):
        # a spaced HOME: -c is relative to HOME/run (stack.conf_value); OPENSSL_CONF names HOME's etc/
        argv = ["/Apps/Moonglade.app/Contents/Resources/Moonglade/server/bin/worldserver", "-c",
                "../etc/worldserver.conf"]
        self.assertTrue(server_of_home(argv, {"OPENSSL_CONF": str(HOME / "etc/openssl.cnf")}, HOME))

    def test_absolute_conf_matched_by_argv(self):
        home = Path("/tmp/h")
        argv = ["/A/server/bin/authserver", "-c", "/tmp/h/etc/authserver.conf"]
        self.assertTrue(server_of_home(argv, {}, home))

    def test_other_home_not_matched(self):
        # counterexamples: another HOME (also a prefix-sharing name) and the dev lane (no OPENSSL_CONF)
        argv = ["/A/server/bin/worldserver", "-c", "../etc/worldserver.conf"]
        self.assertFalse(server_of_home(argv, {"OPENSSL_CONF": "/tmp/u/other/etc/openssl.cnf"}, HOME))
        self.assertFalse(server_of_home(argv, {"OPENSSL_CONF": str(HOME) + "2/etc/openssl.cnf"}, HOME))
        self.assertFalse(server_of_home(["/repo/env/server/bin/worldserver", "-c", "/repo/env/etc/worldserver.conf"],
                                        {}, HOME))
