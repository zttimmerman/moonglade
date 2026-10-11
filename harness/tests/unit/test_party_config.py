"""m4-party-preset task 1.2: every profile's generated playerbots.conf sets AiPlayerbot.AutoAvoidAoe = 1 and
AiPlayerbot.ApplyInstanceStrategies = 1 explicitly, even when the .conf.dist's default says otherwise (an upstream
default change), and lane B's AC_* environment carries the same values."""
import re

from hslib import config, paths as P

from .base import UnitCase

KEYS = ("AiPlayerbot.AutoAvoidAoe", "AiPlayerbot.ApplyInstanceStrategies")
# a .conf.dist whose defaults turned both off (the change this pins against), plus a key the profiles also set
DIST_OFF = ("AiPlayerbot.AutoAvoidAoe = 0\nAiPlayerbot.ApplyInstanceStrategies = 0\n"
            "AiPlayerbot.MinRandomBots = 50\nPlayerbotsDatabaseInfo = \"x\"\n")


def _value(text: str, key: str) -> str | None:
    m = re.findall(rf"^{re.escape(key)}\s*=\s*(.*?)\s*$", text, re.M)
    return m[-1] if m else None


class PartyConfig(UnitCase):
    def _render(self, profile: str) -> str:
        dist = self.tmp / "playerbots.conf.dist"
        dist.write_text(DIST_OFF)
        out = self.tmp / "playerbots.conf"
        ov = config.module_overrides(config.overrides(profile))["playerbots.conf.dist"]
        config.render(dist, out, ov)
        return out.read_text()

    def test_every_profile_sets_both_on(self):
        for name in config.PROFILES:
            text = self._render(name)
            for k in KEYS:
                self.assertEqual(_value(text, k), "1", f"{name}: {k}")

    def test_profile_cannot_turn_them_off(self):
        # the explicit values come after the profile's own section, so no profile can drop them by accident
        prof = {"playerbots": {k: "0" for k in KEYS}}
        ov = config.module_overrides(prof)["playerbots.conf.dist"]
        self.assertEqual([ov[k] for k in KEYS], ["1", "1"])

    def test_counterexample_without_the_pin_the_dist_default_wins(self):
        dist = self.tmp / "playerbots.conf.dist"
        dist.write_text(DIST_OFF)
        out = self.tmp / "playerbots.conf"
        ov = {k: v for k, v in config.module_overrides(config.overrides("m0"))["playerbots.conf.dist"].items()
              if k not in KEYS}
        config.render(dist, out, ov)
        self.assertEqual([_value(out.read_text(), k) for k in KEYS], ["0", "0"])

    def test_lane_b_env_carries_them(self):
        mods = self.tmp / "mods"
        mods.mkdir()
        src = P.BOTS_SRC / "conf" / "playerbots.conf.dist"
        if src.is_file():
            (mods / "playerbots.conf.dist").write_text(src.read_text())
        else:
            self.skipTest("vendor/mod-playerbots not checked out")
        for d in ("mod_era_profile.conf.dist", "mod_moonglade.conf.dist", "AutoBalance.conf.dist"):
            for base in (P.MODULES / "mod-era-profile" / "conf", P.MODULES / "mod-moonglade" / "conf",
                         P.AB_SRC / "conf"):
                if (base / d).is_file():
                    (mods / d).write_text((base / d).read_text())
        try:
            env = config.module_env(config.overrides("m0"), mods)
        except config.ConfigError as e:
            self.skipTest(f"module .conf.dist files not all present: {e}")
        for k in KEYS:
            self.assertEqual(env[config.env_var_name(k)], "1", k)
