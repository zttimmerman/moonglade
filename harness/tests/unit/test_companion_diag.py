"""fix/quest-shared-flake: the evidence a failed `.playerbots bot addclass` keeps (hslib/botlog.excerpt and
hslib/companion.addclass_verdict), with mocked logs and DB rows."""
from hslib import botlog, companion as C
from hslib.probe import proto as W

from .base import UnitCase

PRIEST = {"guid": 801, "name": "Priesta", "level": 8, "online": 0, "account": 12, "group": None}


class LogExcerpt(UnitCase):
    def test_only_lines_since_the_mark(self):
        log = self.tmp / "Playerbots.log"
        log.write_text("old 1\nold 2\n")
        at = botlog.marks({"playerbots": log})
        with open(log, "a") as fh:
            fh.write("new GroupInvite failed\nnew noise\n")
        self.assertEqual(botlog.excerpt(at), {"playerbots": ["new GroupInvite failed", "new noise"]})
        self.assertEqual(botlog.excerpt(at, pattern="groupinvite"), {"playerbots": ["new GroupInvite failed"]})
        self.assertEqual(botlog.excerpt(at, max_lines=1), {"playerbots": ["new noise"]})

    def test_restart_truncation_reads_from_start_and_missing_log_is_empty(self):
        log = self.tmp / "worldserver.out"
        log.write_text("x" * 100 + "\n")
        at = botlog.marks({"worldserver": log, "gone": self.tmp / "missing.log"})
        log.write_text("after restart\n")          # mode w: the worldserver truncated it
        self.assertEqual(botlog.excerpt(at), {"worldserver": ["after restart"], "gone": []})


class AddclassVerdict(UnitCase):
    def test_refusal_message_wins(self):
        chat = [{"type": W.CHAT_MSG_SYSTEM, "sender": 0, "msg": "Failure: You have added too many bots (more than 40)"}]
        self.assertIn("refused by AddPlayerBot", C.addclass_verdict([PRIEST], set(), chat, 1))

    def test_no_login(self):
        self.assertIn("never completed", C.addclass_verdict([PRIEST], set(), [], 1))
        # a priest already in the party before the command doesn't count as the new one
        self.assertIn("never completed", C.addclass_verdict([PRIEST | {"online": 1}], {801}, [], 1))

    def test_online_in_stale_party(self):
        v = C.addclass_verdict([PRIEST | {"online": 1, "group": 7}], set(), [], 1)
        self.assertIn("another saved party", v)
        self.assertIn("Priesta", v)

    def test_online_greeted_not_invited(self):
        chat = [{"type": W.CHAT_MSG_WHISPER, "sender": 801, "msg": "Hello!"}]
        v = C.addclass_verdict([PRIEST | {"online": 1}], set(), chat, 1)
        self.assertIn("greeted the master", v)
        self.assertNotIn("greeted", C.addclass_verdict([PRIEST | {"online": 1}], set(), [], 1))

    def test_in_masters_party_but_unseen(self):
        # live counterexample 2026-10-12 (0.2 s timeout): the DB already had the bot in the master's party
        self.assertIn("master's saved party", C.addclass_verdict([PRIEST | {"online": 1, "group": 1}], set(), [], 1))
        self.assertIn("another saved party", C.addclass_verdict([PRIEST | {"online": 1, "group": 7}], set(), [], 1))
        self.assertIn("another saved party", C.addclass_verdict([PRIEST | {"online": 1, "group": 7}], set(), [], None))
