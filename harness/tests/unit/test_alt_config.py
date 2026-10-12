"""m4-alt-companions (companion-bots "The setting that protects alts is on"; check id
unit:py:test_alt_config.alt_gear_protected_in_every_profile): every shipped profile's generated playerbots.conf sets
AiPlayerbot.AutoGearCommandAltBots = 0, even over a .conf.dist whose default is 1 (the stock default, conf:591-593),
lane B's AC_* environment carries the same value, and no command list the add-on sends to an alt (MG_BRING_PLAN)
contains autogear, init or talents. (/mg party never whispers a brought alt: unit:lua:test_addon_bring.)"""
import re

from hslib import config, partyplan as PP

from .base import UnitCase

KEY = "AiPlayerbot.AutoGearCommandAltBots"
DIST_ON = f"{KEY} = 1\nAiPlayerbot.MinRandomBots = 50\nPlayerbotsDatabaseInfo = \"x\"\n"


def _value(text: str, key: str) -> str | None:
    m = re.findall(rf"^{re.escape(key)}\s*=\s*(.*?)\s*$", text, re.M)
    return m[-1] if m else None


def problems(rendered: dict[str, str], lane_b: dict[str, str], bring: dict) -> list[str]:
    """What breaks the scenario: a profile whose playerbots.conf doesn't set the key to 0, lane B's environment not
    carrying 0, or an alt command list with a gear or talent command."""
    out = [f"{name}: {KEY} = {_value(text, KEY)}" for name, text in sorted(rendered.items()) if _value(text, KEY) != "0"]
    env = config.env_var_name(KEY)
    out += [f"lane B {name}: {env} = {e.get(env)}" for name, e in sorted(lane_b.items()) if e.get(env) != "0"]
    lists = PP.bring_commands(bring, "Hsalt")
    out += [f"/mg bring sends {x!r} to the alt" for x in PP.alt_forbidden(lists["whispers"])]
    out += [f"/mg bring/dismiss command {c!r}" for c in (lists["add"], lists["remove"])
            if PP.alt_forbidden([c.split(" ", 3)[-1]])]
    return out


class AltConfig(UnitCase):
    def _render(self, profile: str, dist_text: str = DIST_ON, drop_key: bool = False) -> str:
        dist = self.tmp / "playerbots.conf.dist"
        dist.write_text(dist_text)
        out = self.tmp / "playerbots.conf"
        ov = config.module_overrides(config.overrides(profile))["playerbots.conf.dist"]
        if drop_key:
            ov = {k: v for k, v in ov.items() if k != KEY}
        config.render(dist, out, ov)
        return out.read_text()

    def _lane_b(self) -> dict[str, dict[str, str]]:
        mods = self.tmp / "mods"
        mods.mkdir(exist_ok=True)
        union: dict[str, set[str]] = {}
        for name in config.PROFILES:
            for d, v in config.module_overrides(config.overrides(name)).items():
                union.setdefault(d, set()).update(v)
        for d, keys in union.items():
            (mods / d).write_text("".join(f"{k} = 1\n" for k in sorted(keys)))
        return {name: config.module_env(config.overrides(name), mods) for name in config.PROFILES}

    def test_alt_gear_protected_in_every_profile(self):
        rendered = {name: self._render(name) for name in config.PROFILES}
        self.assertGreaterEqual(len(rendered), 2, "the shipped profiles")
        self.assertEqual(problems(rendered, self._lane_b(), PP.bring_plan()), [])

    def test_counterexample_without_the_setting_the_dist_default_wins(self):
        rendered = {"m0": self._render("m0", drop_key=True)}
        self.assertEqual(problems(rendered, {}, PP.bring_plan()), [f"m0: {KEY} = 1"])

    def test_counterexample_a_gear_command_in_the_bring_list(self):
        bad = {"add": ".playerbots bot add ", "remove": ".playerbots bot remove ",
               "steps": [{"send": ["reset botAI"], "reply": "x"}, {"send": ["autogear", "talents spec arms pve"]},
                         {"send": ["init=auto"]}]}
        got = problems({"m0": self._render("m0")}, {}, bad)
        self.assertEqual(got, ["/mg bring sends 'autogear' to the alt", "/mg bring sends 'talents spec arms pve' to "
                               "the alt", "/mg bring sends 'init=auto' to the alt"])

    def test_the_bring_plan_reader_fails_closed(self):
        f = self.tmp / "MoongladeParty.lua"
        f.write_text("MG_BRING_PLAN = { add = \".playerbots bot add \", remove = x }\n")
        with self.assertRaises(PP.PlanError):
            PP.bring_plan(f)
        f.write_text("MG_BRING_PLAN = { add = \"a\", remove = \"r\" }\n")
        with self.assertRaises(PP.PlanError):
            PP.bring_plan(f)
