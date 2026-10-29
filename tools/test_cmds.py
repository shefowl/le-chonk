"""Live check of the new commands (18.10.2026): new production line, training, trade,
advisors, and the political checks. Run with the harness stopped and the game paused.

    python3 tools/test_cmds.py checks          # pure CanExecute checks, nothing posted
    python3 tools/test_cmds.py line VARIANT_ID # open a production line, then remove it
    python3 tools/test_cmds.py train TEMPLATE  # one division into training (name)
    python3 tools/test_cmds.py trade TAG RES N # buy N of a resource from TAG
    python3 tools/test_cmds.py hire CHAR SLOT  # hire an advisor
"""
import json
import struct
import sys
import time

sys.path.insert(0, "bridge/calling")
from game import Game, _arr          # noqa: E402
import cabinet                       # noqa: E402
import industry                      # noqa: E402


def show(x):
    print(json.dumps(x, ensure_ascii=False, default=str, indent=1)[:4000])


def deployment_arrays(g):
    p = g.p
    ds = industry.deployment(g)
    out = {}
    for off in range(0, 0x400, 8):
        try:
            hdr = p.read(ds + off, 16)
        except OSError:
            continue
        d, cap, n = struct.unpack("<Qii", hdr)
        if 0 < n <= cap < 4096 and 0x500000000000 < d < 0x7fffffffffff:
            els = [struct.unpack("<Q", p.read(d + 8 * i, 8))[0] for i in range(min(n, 4))]
            out[hex(off)] = (n, [(hex(e), g.oi.class_of(e)) for e in els])
    return out


def main():
    g = Game()
    what = sys.argv[1]
    if what == "checks":
        g.read_only = False
        print("== variants"); show([(v["id"], v["equipment"], v["design"]) for v in industry.variants(g)])
        print("== resources"); show(sorted(industry.strategic_resources(g)))
        print("== startable focuses"); show(cabinet.startable_focuses(g))
        print("== laws"); show(cabinet.laws(g))
        print("== hireable"); show([(r["character"], r["slot"], r["cost"]) for r in cabinet.hireable(g)])
        print("== decisions"); show(cabinet.takeable_decisions(g))
        print("== deployment arrays"); show(deployment_arrays(g))
    elif what == "line":
        vid = int(sys.argv[2])
        print(industry.add_line(g, vid, dry_run=True))
        r = industry.add_line(g, vid)
        print(r)
        if r.get("ok"):
            show(g.production_lines()[-2:])
            if "--keep" not in sys.argv:
                print(g.remove_production_line(r["lines_after"] - 1))
    elif what == "train":
        name = sys.argv[2]
        t = next(x for x in g.division_templates() if x["name"] == name)
        before = deployment_arrays(g)
        print(industry.train(g, int(t["ptr"], 16), dry_run=True))
        print(industry.train(g, int(t["ptr"], 16)))
        time.sleep(0.6)
        after = deployment_arrays(g)
        print("before"); show(before)
        print("after"); show(after)
    elif what == "trade":
        tag, res, n = sys.argv[2], sys.argv[3], int(sys.argv[4])
        print(industry.trade(g, tag, res, n, dry_run=True))
        if "--dry" not in sys.argv:
            print(industry.trade(g, tag, res, n))
            time.sleep(0.6)
            print(g.trade_routes())
    elif what == "hire":
        print(cabinet.hire(g, sys.argv[2], sys.argv[3], dry_run=True))
        if "--dry" not in sys.argv:
            print(cabinet.hire(g, sys.argv[2], sys.argv[3]))
            time.sleep(0.6)
            print(g.politics()["political_power"])


if __name__ == "__main__":
    main()
