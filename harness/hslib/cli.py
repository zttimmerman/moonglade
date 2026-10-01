import argparse
import json
import sys

from .util import log


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hs", description="Hearthstone harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="sync pinned vendor trees and build/install the server")
    b.add_argument("--clean", action="store_true")
    b.add_argument("-j", "--jobs", type=int, default=8)
    a = ap.parse_args(argv)

    if a.cmd == "build":
        from .build import build, BuildError
        try:
            info = build(clean=a.clean, jobs=a.jobs)
        except BuildError as e:
            log(f"BUILD FAILED: {e}")
            return 1
        print(json.dumps(info, indent=2))
        return 0
    return 2
