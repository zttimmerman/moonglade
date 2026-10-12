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
        # every module key any profile sets, with a stand-in default of 0: the 1s must come from the profile itself
        mods = self.tmp / "mods"
        mods.mkdir()
        union: dict[str, set[str]] = {}
        for name in config.PROFILES:
            for d, v in config.module_overrides(config.overrides(name)).items():
                union.setdefault(d, set()).update(v)
        for d, keys in union.items():
            (mods / d).write_text("".join(f"{k} = 0\n" for k in sorted(keys)))
        env = config.module_env(config.overrides("m0"), mods)
        for k in KEYS:
            self.assertEqual(env[config.env_var_name(k)], "1", k)
