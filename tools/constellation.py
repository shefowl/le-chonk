"""Resolve old function addresses that sit in code regions without anchors.

Within one translation unit the distances between functions survive a patch unless that
unit was edited. So a group of old addresses that lie close together ("constellation")
is moved by one delta, and the right delta is the one that puts every member on a
function start in the new build. Big groups pin their delta uniquely; singletons do not,
and are reported with caller counts so a semantic check can decide.

usage: constellation.py <bridge_dir> <port.json> [--window 0x8000] [--gap 0x40000]
"""
import json
import os
import pickle
import sys


def main():
    bridge, port_json = sys.argv[1:3]
    window = int(sys.argv[sys.argv.index("--window") + 1], 16) if "--window" in sys.argv else 0x8000
    gap = int(sys.argv[sys.argv.index("--gap") + 1], 16) if "--gap" in sys.argv else 0x40000
    sys.path[:0] = [os.path.join(bridge, "calling"), os.path.join(bridge, "research")]
    import config
    dumps = config.DUMP_DIR
    starts = set(pickle.load(open(os.path.join(dumps, "funcs.pkl"), "rb")))
    callers = pickle.load(open(os.path.join(dumps, "callmap.pkl"), "rb"))

    rows = json.load(open(port_json))
    todo = sorted((r for r in rows if not r["ok"] and r["section"] == ".text"),
                  key=lambda r: r["old"])
    groups, cur = [], []
    for r in todo:
        if cur and r["old"] - cur[-1]["old"] > gap:
            groups.append(cur)
            cur = []
        cur.append(r)
    if cur:
        groups.append(cur)

    for g in groups:
        olds = [r["old"] for r in g]
        base = int(g[0]["how"].split("lo=")[1].split()[0], 16) if "lo=0x" in g[0]["how"] or "lo=-" in g[0]["how"] else 0
        scored = []
        for d in range(base - window, base + window + 1, 0x10):
            hit = sum((o + d) in starts for o in olds)
            if hit:
                scored.append((hit, -abs(d - base), d))
        scored.sort(reverse=True)
        print("group of %d around %s (anchor delta %s):" % (len(olds), hex(olds[0]), hex(base)))
        for hit, _, d in scored[:4]:
            print("   delta %-9s %d/%d on function starts" % (hex(d), hit, len(olds)))
        if scored:
            d = scored[0][2]
            for o in olds:
                n = o + d
                print("      %s -> %s  start=%s callers=%d" % (hex(o), hex(n), n in starts,
                                                              len(callers.get(n, ()))))


if __name__ == "__main__":
    main()
