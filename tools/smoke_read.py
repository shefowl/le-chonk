"""Smoke A1: read-only pass over the ported bridge against a running game.

g.read_only = True makes every injected call raise on our side, so nothing here can
stop the game's threads or post a command. Each probe runs on its own; a failure is
reported and the rest continue.

usage: smoke_read.py <bridge_dir> [enemy_tag]
"""
import json
import os
import sys
import time
import traceback


def main():
    bridge = sys.argv[1]
    enemy = sys.argv[2] if len(sys.argv) > 2 else "POL"
    sys.path[:0] = [os.path.join(bridge, "calling"), os.path.join(bridge, "research")]
    from game import Game
    import armyops, combat, session

    g = Game()
    g.read_only = True
    print("attached pid=%d base=%#x, build-id check passed" % (g.p.pid, g.base))

    def probe(name, fn):
        t = time.time()
        try:
            r = fn()
            s = json.dumps(r, default=str, ensure_ascii=False)
            print("[ok]   %-22s %6.0f ms  %s" % (name, 1000 * (time.time() - t), s[:400]))
            return r
        except Exception as e:
            print("[FAIL] %-22s %s: %s" % (name, type(e).__name__, e))
            traceback.print_exc(limit=2, file=sys.stdout)

    probe("date", g.date)
    probe("player_tag", g.player_tag)
    probe("player.summary", lambda: g.player().summary())
    probe("paused", lambda: session.is_paused(g))
    probe("at_war", lambda: combat.at_war(g))
    probe("enemy_tags", lambda: combat.enemy_tags(g))
    probe("combats", lambda: combat.combats(g))
    ogs = probe("orders_groups", lambda: [hex(x) for x in armyops.orders_groups(g)])
    if ogs:
        for og in ogs[:6]:
            probe("og_info " + og, lambda og=og: armyops.og_info(g, int(og, 16)))
            probe("orders_of " + og, lambda og=og: armyops.orders_of(g, int(og, 16)))
    probe("fronts_towards " + enemy, lambda: armyops.fronts_towards(g, enemy))
    probe("armies", lambda: g.armies())
    probe("divisions", lambda: g.divisions(limit=5))
    probe("overview", lambda: g.overview())


if __name__ == "__main__":
    main()
