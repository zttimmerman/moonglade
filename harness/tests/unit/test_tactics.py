"""m4-instance-tactics task 1.2: hslib/tactics.py reads ApplyInstanceStrategies's map table from source (fail-closed)
and judges the companions' `co ?` answers per dungeon. The source here is a stand-in with made-up map ids and names
(vendor/ isn't in CI); dng.instance_strategy parses the pinned file."""
from hslib import tactics as T

from .base import UnitCase

STUB = '''
void PlayerbotAI::Other(uint32 mapId) { switch (mapId) { case 9: strategyName = "decoy"; break; } }

void PlayerbotAI::ApplyInstanceStrategies(uint32 mapId, bool tellMaster)
{
    static const std::vector<std::string> allInstanceStrategies =
    {
        "x-one", "x-two",
        "x-three"
    };

    std::string strategyName;
    switch (mapId)
    {
        case 1001:
            strategyName = "x-one";  // first
            break;
        case 1002:
            strategyName = "x-two";
            break;
        case 1003: strategyName = "x-three"; break;
        default:
            break;
    }
    if (tellMaster && !strategyName.empty())
    {
        out << "Added " << strategyName << " instance strategy";
    }
}
'''


class Table(UnitCase):
    def test_parses_the_function_only(self):
        t = T.instance_table(STUB)
        self.assertEqual(t["table"], {1001: "x-one", 1002: "x-two", 1003: "x-three"})
        self.assertEqual(t["all"], ["x-one", "x-two", "x-three"])
        self.assertEqual(t["func_line"], 4)
        self.assertEqual(STUB.splitlines()[t["lines"][1001] - 1].strip(), "case 1001:")

    def test_fail_closed(self):
        with self.assertRaises(T.TacticsError):
            T.instance_table(STUB.replace("ApplyInstanceStrategies", "Renamed"))
        with self.assertRaises(T.TacticsError):     # the switch's entries changed shape
            T.instance_table(STUB.replace("strategyName = ", "name = "))
        with self.assertRaises(T.TacticsError):     # added but never removed: would survive leaving
            T.instance_table(STUB.replace('"x-three"\n', '"x-thr"\n'))
        with self.assertRaises(T.TacticsError):
            T.instance_table(STUB.replace("case 1002", "case 1001"))


class Judge(UnitCase):
    def setUp(self):
        super().setUp()
        self.expect = T.expectations(T.instance_table(STUB)["table"], [1001, 1002, 77])

    def test_expectations(self):
        self.assertEqual(self.expect, [{"map": 1001, "want": "x-one", "not": ["x-two"]},
                                       {"map": 1002, "want": "x-two", "not": ["x-one"]},
                                       {"map": 77, "want": None, "not": ["x-one", "x-two"]}])

    def test_pass(self):
        reads = [{"a": ["tank", "x-one"], "b": ["x-one"]}, {"a": ["x-two"], "b": ["heal", "x-two"]},
                 {"a": ["tank"], "b": ["heal"]}]
        self.assertEqual(T.judge(self.expect, reads), [])

    def test_counterexamples(self):
        ok = [{"a": ["x-one"]}, {"a": ["x-two"]}, {"a": []}]
        cases = {
            "missing": ([{"a": ["tank"]}] + ok[1:], "map 1001: a doesn't list x-one"),
            "kept": ([ok[0], {"a": ["x-one", "x-two"]}, ok[2]], "map 1002: a still lists x-one"),
            "kept_outside": (ok[:2] + [{"a": ["x-two"]}], "map 77: a still lists x-two"),
            "unanswered": ([ok[0], {"a": None}, ok[2]], "map 1002: a never answered `co ?`"),
            "empty_party": ([{}] + ok[1:], "map 1001: no companion answers"),
            "short": (ok[:2], "2 dungeon reads for 3 dungeons"),
        }
        for name, (reads, msg) in cases.items():
            self.assertIn(msg, T.judge(self.expect, reads), name)
