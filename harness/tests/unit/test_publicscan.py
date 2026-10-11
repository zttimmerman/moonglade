"""publicscan's email rule (harness/hslib/publicscan.py): an image's scale suffix isn't an address, a real one is.
The sample addresses are assembled at run time, so this file itself holds none (public.tree_scrub and the history
scan read the source)."""
from hslib import publicscan as S

from .base import UnitCase


class EmailRule(UnitCase):
    def hits(self, data: bytes):
        return [h for h in S.find_hits(data, []) if h[0] == "email address"]

    def test_iconset_scale_suffix_isnt_an_email(self):
        self.assertEqual(self.hits(b'I.write_png(iconset / "icon_512x512@2x.png", 1)\n'), [])

    def test_counterexample_a_real_address_is_found(self):
        at = b"@"
        self.assertEqual(self.hits(b"mail me at someone.real" + at + b"gmail.com\n"), [("email address", 1)])
        self.assertEqual(self.hits(b"x" + at + b"2x.com\n"), [("email address", 1)])
