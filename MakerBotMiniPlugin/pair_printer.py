#!/usr/bin/env python3
"""Command-line fallback for pairing a Replicator Mini without Cura's connect dialog.

    python3 pair_printer.py 192.168.1.50
    python3 pair_printer.py 192.168.1.50 --cura-cfg "<Cura config folder>/cura.cfg"

Accept the connection on the printer when asked.  With --cura-cfg the result is
written into Cura's preferences file (close Cura first; a .bak copy is made).
Otherwise the values are printed so you can enter them yourself.
"""

import argparse
import configparser
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import makerbot_rpc  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", help="printer IP address or host name")
    parser.add_argument("--cura-cfg", help="path to Cura's cura.cfg to update (close Cura first)")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for acceptance")
    args = parser.parse_args()

    try:
        host = makerbot_rpc.validate_host(args.host)
        code = makerbot_rpc.pair(
            host, "Cura", timeout_s=args.timeout,
            on_waiting=lambda: print("Accept the connection on the printer now (waiting up to %d s)..." %
                                     args.timeout))
    except makerbot_rpc.MakerBotError as err:
        print("Pairing failed: %s" % err, file=sys.stderr)
        return 1

    if not args.cura_cfg:
        print("Paired.  Values for Cura's [makerbot_mini] preferences section:")
        print("  ip = %s" % host)
        print("  auth_code = %s" % code)
        return 0

    parser_cfg = configparser.ConfigParser(interpolation=None)
    parser_cfg.optionxform = str  # keep key case
    if os.path.exists(args.cura_cfg):
        shutil.copyfile(args.cura_cfg, args.cura_cfg + ".bak")
        parser_cfg.read(args.cura_cfg, encoding="utf-8")
    if not parser_cfg.has_section("makerbot_mini"):
        parser_cfg.add_section("makerbot_mini")
    parser_cfg.set("makerbot_mini", "ip", host)
    parser_cfg.set("makerbot_mini", "auth_code", code)
    with open(args.cura_cfg, "w", encoding="utf-8") as handle:
        parser_cfg.write(handle)
    print("Paired.  Saved to %s (backup: %s.bak).  Start Cura." % (args.cura_cfg, args.cura_cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
