"""m4-party-preset: the harness reads /mg party's plan from the add-on (hslib/partyplan.py), fail-closed, and every
premade spec the plan picks exists for its class in the pinned playerbots.conf.dist (a renamed spec fails here first,
then in dng.party_preset)."""
from hslib import partyplan as PP, paths as P

from .base import UnitCase


class PartyPlan(UnitCase):
    def test_reads_the_addon_table(self):
        p = PP.plan()
        self.assertEqual([c["class"] for c in PP.commands(p, "damage")], ["warrior", "priest", "mage", "rogue"])
        self.assertEqual([c["class"] for c in PP.commands(p, "tank")], ["priest", "mage", "rogue", "hunter"])
        self.assertEqual([c["class"] for c in PP.commands(p, "healer")], ["warrior", "mage", "rogue", "hunter"])
        self.assertEqual(p["clear"], ".playerbots bot remove *")
        for role in PP.ROLES:
            for c in PP.commands(p, role):
                self.assertEqual(c["addclass"], ".playerbots bot addclass " + c["class"])
                self.assertEqual(c["whispers"][0], "reset botAI", c)
                spec = PP.spec_of(c["whispers"])
                self.assertIsNotNone(spec, c)
                # gear and strategies only after the spec pick has answered (a spec pick resets strategies)
                self.assertEqual(c["steps"][0], {"send": ["reset botAI"], "reply": "AI was reset to defaults"})
                self.assertEqual(c["steps"][1], {"send": [f"talents spec {spec}"], "reply": f"Picking {spec}"})
                self.assertIsNone(c["steps"][-1]["reply"])

    # Each plan spec existing in the pinned playerbots.conf is checked by dng.party_preset against the rendered
    # conf (vendor/ isn't in a unit run's checkout); the parser it uses is tested here.

    def test_counterexample_renamed_spec_is_missing(self):
        conf = "AiPlayerbot.PremadeSpecName.1.2 = protection pve\nAiPlayerbot.PremadeSpecName.5.1 = holy pve\n"
        self.assertNotIn("prot pve", PP.premade_specs(conf, "warrior"))
        self.assertEqual(PP.premade_specs(conf, "priest"), ["holy pve"])

    def test_parser_literals_and_comments(self):
        t = PP.parse_table('{ a = "x", -- note\n ["b c"] = { 1, 2.5, true }, [3] = "t"; d = {} }')
        self.assertEqual(t, {"a": "x", "b c": [1, 2.5, True], 3: "t", "d": []})   # an empty table reads as a list

    def test_parser_fails_closed(self):
        for bad in ('{ a = x }', '{ a = "x" .. "y" }', '{ a = "x" ', '{ 1, b = 2 }', '{ a = f() }'):
            with self.assertRaises(PP.PlanError, msg=bad):
                PP.parse_table(bad)
        f = self.tmp / "p.lua"
        f.write_text("MG_PARTY_PLAN = { addclass = 'x' }\n")
        with self.assertRaises(PP.PlanError):
            PP.plan(f)
        f.write_text("local nothing = 1\n")
        with self.assertRaises(PP.PlanError):
            PP.plan(f)
