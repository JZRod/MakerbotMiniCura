#!/usr/bin/env python3
"""Inspect a .makerbot file, or compare a plugin-generated one with a MakerBot Desktop one.

    python3 inspect_makerbot.py reference.makerbot
    python3 inspect_makerbot.py reference.makerbot --compare generated.makerbot

Prints meta.json, command statistics, the first/last commands, and a feedrate-unit
check (is sum(distance / feedrate) close to meta 'commanded_duration_s'?).
"""

import argparse
import collections
import json
import math
import sys
import zipfile


def load(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        meta = json.loads(archive.read("meta.json"))
        toolpath = json.loads(archive.read("print.jsontoolpath"))
    return names, meta, toolpath


def stats(toolpath):
    counts = collections.Counter(c["command"]["function"] for c in toolpath)
    moves = [c["command"] for c in toolpath if c["command"]["function"] == "move"]
    position = None
    seconds = 0.0
    rel_seen = collections.Counter()
    for m in moves:
        p = m["parameters"]
        rel_seen[json.dumps(m.get("metadata", {}).get("relative"), sort_keys=True)] += 1
        here = (p.get("x", 0), p.get("y", 0), p.get("z", 0))
        if position is not None and p.get("feedrate"):
            dist = math.dist(here, position)
            seconds += dist / p["feedrate"]
        position = here
    return counts, moves, seconds, rel_seen


def describe(path):
    names, meta, toolpath = load(path)
    counts, moves, seconds, rel_seen = stats(toolpath)
    print("== %s" % path)
    print("files:", ", ".join(names))
    print("meta.json:", json.dumps(meta, indent=2, sort_keys=True))
    print("commands: %d  %s" % (len(toolpath), dict(counts)))
    print("relative-flag combinations:", dict(rel_seen))
    if moves:
        xs = [m["parameters"]["x"] for m in moves]
        ys = [m["parameters"]["y"] for m in moves]
        zs = [m["parameters"]["z"] for m in moves]
        print("x range %.2f..%.2f  y range %.2f..%.2f  z range %.2f..%.2f" %
              (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))
    commanded = meta.get("commanded_duration_s")
    print("sum(distance/feedrate) = %.1f s   (meta commanded_duration_s = %s)" % (seconds, commanded))
    if commanded:
        print("  -> ratio %.3f  (close to 1.0 means feedrate is mm/s; close to 60 means mm/min)" %
              (seconds / commanded))
    print("first 12 commands:")
    for c in toolpath[:12]:
        print("  ", json.dumps(c["command"], sort_keys=True))
    print("last 6 commands:")
    for c in toolpath[-6:]:
        print("  ", json.dumps(c["command"], sort_keys=True))
    return meta, counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file")
    parser.add_argument("--compare", help="a second .makerbot file to compare structurally")
    args = parser.parse_args()
    meta, counts = describe(args.file)
    if args.compare:
        print()
        other_meta, other_counts = describe(args.compare)
        print("\n== comparison")
        print("meta keys only in reference:", sorted(set(meta) - set(other_meta)))
        print("meta keys only in generated:", sorted(set(other_meta) - set(meta)))
        for key in sorted(set(meta) & set(other_meta)):
            if type(meta[key]) is not type(other_meta[key]):
                print("type differs for %s: %r vs %r" % (key, meta[key], other_meta[key]))
        print("command types only in reference:", sorted(set(counts) - set(other_counts)))
        print("command types only in generated:", sorted(set(other_counts) - set(counts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
