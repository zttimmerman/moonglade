"""./play's signal handler (play-launcher "Interrupt shuts everything down"; test-pyramid design 9.3): SIGTERM and SIGHUP
take the same path as SIGINT, the live check (play.cold_sessions, session I) sends SIGINT. Here: every handled signal
raises Interrupted once with its number and records its name; a repeated signal, or any signal once cleanup has
started, is ignored (the cleanup is never aborted or run twice); and main() maps an interrupt to 128 + the signal."""
import signal

from hslib import play

from .base import UnitCase


class _Loop:
    """The handler's state, as play.Loop holds it (cleaning, signum, rec)."""

    def __init__(self):
        self.cleaning = False
        self.signum = None
        self.rec = {}


class SignalHandler(UnitCase):
    def test_sigterm_interrupts_like_sigint(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop = _Loop()
            with self.assertRaises(play.Interrupted) as cm:
                play.Loop.on_signal(loop, sig, None)
            self.assertEqual(cm.exception.signum, sig)
            self.assertEqual(loop.rec["signal"]["name"], signal.Signals(sig).name)

    def test_handled_signals_include_sigterm(self):
        self.assertIn(signal.SIGTERM, play.HANDLED)
        self.assertIn(signal.SIGINT, play.HANDLED)

    def test_repeated_signal_is_ignored(self):
        loop = _Loop()
        with self.assertRaises(play.Interrupted):
            play.Loop.on_signal(loop, signal.SIGTERM, None)
        play.Loop.on_signal(loop, signal.SIGTERM, None)          # no second raise
        play.Loop.on_signal(loop, signal.SIGINT, None)
        self.assertEqual(loop.rec["signal"]["name"], "SIGTERM")

    def test_signal_during_cleanup_is_ignored(self):
        loop = _Loop()
        loop.cleaning = True
        play.Loop.on_signal(loop, signal.SIGTERM, None)
        self.assertNotIn("signal", loop.rec)

    def test_exit_code_is_128_plus_signal(self):
        # main(): `rc = 128 + e.signum` for an Interrupted (read from the source, so a changed mapping fails here)
        import inspect
        src = inspect.getsource(play.main)
        self.assertIn("rc = 128 + e.signum", src)
        self.assertEqual(play.Interrupted(signal.SIGTERM).signum + 128, 143)
