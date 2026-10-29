"""Smoke A2: battle plans posted through the game's command queue, no GUI.

One subcommand per step, state kept in cache-1.19.3/a2_state.json between steps, so
every step is a separate process and a crash shows exactly which call did it.

  scout              read-only: divisions nearest the Polish border, generals, wargoals
  army   [n]         create one army from the n divisions nearest the border (default 24)
  general [key]      give it a corps commander (best available if no key)
  front              front line along the whole POL border
  war                declare war on POL (existing war goal, game's CanExecute decides)
  offensive [depth]  offensive line into Poland (default: full depth)
  execute            execute the whole plan
  orders             read back the army's orders and divisions
  watch [days]       unpause, let N in-game days run, sample positions and combats

usage: a2.py <bridge_dir> <subcommand> [arg]
"""
import collections
import json
import os
import sys
import time

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache-1.19.3", "a2_state.json")
ENEMY = "POL"


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save_state(s):
    json.dump(s, open(STATE, "w"), indent=1)


def show(x):
    print(json.dumps(x, default=str, ensure_ascii=False, indent=1)[:6000])


def nearest_divisions(g, enemy):
    """Our divisions sorted by province-graph distance to the enemy border."""
    import armyops, mapdata, unitorders
    from game import _arr
    border = set()
    for sec in armyops.fronts_towards(g, enemy):
        border.update(sec["border_provinces"])
    adj = mapdata.build_adjacency()
    dist = {p: 0 for p in border}
    q = collections.deque(border)
    while q:
        a = q.popleft()
        for b in adj.get(a, ()):
            if b not in dist:
                dist[b] = dist[a] + 1
                q.append(b)
    out = []
    for d in _arr(g.p, g.player().ptr, 0x290):
        prov = unitorders.province_of(g, d)
        out.append((dist.get(prov, 999), prov, d))
    out.sort()
    return out, border


def main():
    bridge, sub = sys.argv[1], sys.argv[2]
    arg = sys.argv[3] if len(sys.argv) > 3 else None
    sys.path[:0] = [os.path.join(bridge, "calling"), os.path.join(bridge, "research")]
    from game import Game
    import armyops, combat, session, diplomacy
    g = Game()
    st = load_state()
    og = int(st["og"], 16) if st.get("og") else None
    t0 = time.time()

    if sub == "scout":
        g.read_only = True
        divs, border = nearest_divisions(g, ENEMY)
        print("border provinces facing %s: %d" % (ENEMY, len(border)))
        print("divisions by distance:", collections.Counter(min(d, 9) for d, _, _ in divs))
        print("nearest 30:", [(d, p) for d, p, _ in divs[:30]])
        print("at_war:", combat.at_war(g), "unassigned:", len(divs))
        try:
            show(g.available_generals(rank="corps_commander")[:8])
        except Exception as e:
            print("generals (needs a call?):", type(e).__name__, e)
        print("CWarGoal objects:", len(diplomacy.wargoals(g)))

    elif sub == "army":
        n = int(arg or 24)
        divs, _ = nearest_divisions(g, ENEMY)
        pick = [d for _, _, d in divs[:n]]
        r = g.create_army(pick)
        print("create_army ->", r)
        time.sleep(0.5)
        groups = armyops.orders_groups(g)
        print("orders groups now:", [hex(x) for x in groups])
        if groups:
            st["og"] = hex(groups[0]); save_state(st)
            show(armyops.og_info(g, groups[0]))

    elif sub == "general":
        if arg is None:
            cands = g.available_generals(rank="corps_commander")["generals"]
            show(cands[:5])
            arg = cands[0].get("key") or cands[0].get("name")
        show(g.assign_commander(og, arg))
        time.sleep(0.5)          # commands run on the game's next frame, even paused
        show(armyops.og_info(g, og))

    elif sub == "front":
        show(g.draw_front_line(og, ENEMY, None, None, 0, 100000, "proportional"))
        time.sleep(0.5)
        show(armyops.orders_of(g, og))

    elif sub == "war":
        show(g.declare_war(ENEMY))
        time.sleep(0.5)
        print("at_war:", combat.at_war(g), combat.enemy_tags(g))

    elif sub == "offensive":
        orders = armyops.orders_of(g, og)
        root = sorted((o for o in orders if o.get("type") == 2),
                      key=lambda o: -o.get("assigned_divisions", 0))
        parent = root[0]["id"] if root else None
        depth = arg or "full"
        provs = armyops.target_line(g, ENEMY, depth if depth == "full" else int(depth))
        print("parent order:", parent, "target provinces:", len(provs))
        show(armyops.offensive_line(g, og, parent, provs))
        time.sleep(0.5)
        show(armyops.orders_of(g, og))

    elif sub == "execute":
        show(g.execute_plan(og, 0))
        time.sleep(0.5)
        show(armyops.orders_of(g, og))

    elif sub == "orders":
        g.read_only = True
        show(armyops.og_info(g, og))
        show(armyops.orders_of(g, og))

    elif sub == "watch":
        import unitorders
        days = int(arg or 5)
        divs = armyops.group_divisions(g, og) if og else []
        before = {d: unitorders.province_of(g, d) for d in divs}
        d0 = g.date()["raw"]
        g.set_game_speed(2)          # UI speed 3 of 5 (0-based index)
        session.pause(g, False)
        g.read_only = True
        w0, last = time.time(), d0
        while g.date()["raw"] - d0 < days * 24:
            time.sleep(0.5)
            d = g.date()["raw"]
            if d // 24 != last // 24:
                print("%s  +%.1fs wall  combats: %d" % (g.date()["text"], time.time() - w0,
                                                        len(combat.combats(g))), flush=True)
            last = d
        print("%.2f s of wall time per game day at speed 3" % ((time.time() - w0) / days))
        g.read_only = False
        session.pause(g, True)
        g.read_only = True
        after = {d: unitorders.province_of(g, d) for d in divs}
        moved = sum(before[d] != after.get(d) for d in before)
        marching = sum(bool(unitorders.destination(g, d)) for d in divs)
        print("divisions %d, moved province %d, still marching %d" % (len(divs), moved, marching))
        show(combat.combats(g)[:5])

    print("[%s done in %.1fs]" % (sub, time.time() - t0))


if __name__ == "__main__":
    main()
