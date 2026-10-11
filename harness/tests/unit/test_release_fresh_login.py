"""release-fresh-login: the login evidence of release.stack_from_payload (fresh_login / login_ok) with a fake probe,
and the drop_openssl_modules test switch of stack.spawn_env."""
from unittest import mock

from hslib import paths as P, stack
from hslib.checks import release_runtime_checks as R

from .base import UnitCase

PORTS = {"auth": 13744, "world": 18105}


class FakeLogon:
    def __init__(self, ok=True, realms=None):
        self.ok = ok
        self.session_key = b"k" * 40
        self.realms = realms if realms is not None else [{"id": 1, "address": "127.0.0.1:18105"}]

    def evidence(self):
        return {"ok": self.ok}


class FakeWorld:
    connect_error = None

    def __init__(self, user, key, realm, timeout=20):
        self.log = [("C>S", "CMSG_AUTH_SESSION")]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def connect(self):
        if self.connect_error:
            raise self.connect_error
        return {"auth_response": "0x0C AUTH_OK"}

    def char_enum(self):
        return []


def fake_probe(logon=None, world=FakeWorld):
    p = mock.Mock()
    p.logon = mock.Mock(return_value=logon or FakeLogon())
    p.WorldSession = world
    return p


class FreshLogin(UnitCase):
    def test_all_steps_reached(self):
        p = fake_probe()
        ev = R.fresh_login("U", "pw", PORTS, probe=p)
        self.assertTrue(R.login_ok(ev), ev)
        self.assertEqual(ev["reached"], list(R.LOGIN_STEPS))
        self.assertEqual(p.logon.call_args.kwargs["port"], 13744)
        self.assertEqual(ev["char_enum"], 0)

    def test_world_connection_closed_fails_at_world_auth(self):
        class Dies(FakeWorld):
            connect_error = ConnectionError("connection closed by server after 0/1 bytes")
        ev = R.fresh_login("U", "pw", PORTS, probe=fake_probe(world=Dies))
        self.assertFalse(R.login_ok(ev))
        self.assertEqual(ev["failed_at"], "world_auth")
        self.assertEqual(ev["reached"], ["auth", "realm_list"])
        self.assertIn("connection closed", ev["error"])

    def test_bad_proof_fails_at_auth(self):
        ev = R.fresh_login("U", "pw", PORTS, probe=fake_probe(logon=FakeLogon(ok=False)))
        self.assertEqual((ev["failed_at"], ev["reached"]), ("auth", []))
        self.assertFalse(R.login_ok(ev))

    def test_realm_on_another_port_fails_at_realm_list(self):
        lr = FakeLogon(realms=[{"id": 1, "address": "127.0.0.1:8085"}])
        ev = R.fresh_login("U", "pw", PORTS, probe=fake_probe(logon=lr))
        self.assertEqual(ev["failed_at"], "realm_list")
        self.assertFalse(R.login_ok(ev))

    def test_transport_error_at_logon_is_evidence_not_a_raise(self):
        p = fake_probe()
        p.logon.side_effect = ConnectionRefusedError("refused")
        ev = R.fresh_login("U", "pw", PORTS, probe=p)
        self.assertEqual(ev["failed_at"], "auth")

    def test_login_ok_needs_every_step(self):
        self.assertFalse(R.login_ok({"reached": ["auth", "realm_list", "world_auth"], "failed_at": None}))
        self.assertFalse(R.login_ok({}))


class SpawnEnvSwitch(UnitCase):
    def env(self, switch: dict) -> dict:
        with mock.patch.object(P, "MODE", "release"), mock.patch.object(P, "LANE", "a"), \
                mock.patch.object(P, "firstlaunch_test", return_value=switch), \
                mock.patch.object(stack.config, "lane_env", return_value={}):
            return stack.spawn_env("worldserver")

    def test_release_servers_get_openssl_modules(self):
        self.assertIn("OPENSSL_MODULES", self.env({}))

    def test_drop_openssl_modules_switch_leaves_it_out(self):
        env = self.env({"drop_openssl_modules": True})
        self.assertNotIn("OPENSSL_MODULES", env)
        self.assertIn("OPENSSL_CONF", env)
