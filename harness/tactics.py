"""The tactical staff of a theatre: every few days, by code, the blows a corps commander
would deal on his own (25.10, the user's rule).

- The thinnest point: for every enemy province of the line, the attack we could put into
  it with every free division in the provinces beside it (one division stays in each, the
  best defender; a province in danger gives none), weighed by the ground for each kind of
  division, the combat width, rivers and forts, against what defends it. The best point
  at MIN_RATIO or more is struck from all sides; then the next, with what is left.
- An empty enemy province of the line gets the nearest free division, the fast first, and
  armour only where the ground does not cripple it.

Only armies whose POSTURE is attack strike, and not those whose battle plan the game
rates red (the balance per direction, not over the theatre). The orders are direct ones (a right click),
and only attacks: the division goes and the battle plan takes it back after. Our own line
is never mended by hand (the user, 25.10: a division's place changes only by the front,
a transfer between armies, or hold_line; the game puts every division back on its line).
The field marshal sets the postures and the axis; this staff only executes.
"""
import collections

from . import bridge as B
from . import front, ops

MIN_RATIO = 1.5          # a blow at this attack:defence or better
KEEP = 1                 # divisions that stay in each of our provinces
DANGER = 2.0             # a province of ours this threatened gives no division
MAX_BLOWS = 2
MAX_GRABS = 6
POSTURES = ("attack", "hold", "reserve")


def _fast(u):
    return "fast" in (u[5] if len(u) > 5 else {})


def tick(g, mv, theatre_id, enemies, postures, dry=False, min_ratio=MIN_RATIO):
    """One round of the staff in a theatre at war. postures: {army id: posture}, missing =
    attack. Returns [(kind, text)] of what was ordered (or would be, dry)."""
    fr = front.Front(g, mv, enemies)
    zone = front.group_zone(g, fr, theatre_id)
    if not zone:
        return []
    mine = {ops._hid(g, m) for m in ops.theatre_armies(g, theatre_id)}
    post = lambda aid: postures.get(str(aid)) or postures.get(aid) or "attack"
    out, used = [], set()
    uo = B.module("unitorders")

    def order(u, province, why):
        d = u[3]
        used.add(d)
        if dry:
            out.append((why, "a%s division p%s -> p%d" % (fr.div_army.get(d), _at(d), province)))
            return
        ok, err = uo.move(g, d, province)
        out.append((why, "a%s division p%s -> p%d%s" % (fr.div_army.get(d), _at(d), province,
                                                      "" if ok else " REFUSED: %s" % err)))

    where = {}
    for pr, us in fr.units.items():
        for u in us:
            if u[0] == "us":
                where[u[3]] = pr
    _at = lambda d: where.get(d, "?")

    # what each province of ours can give: all but its best defender; nothing when in danger
    danger = set()
    for a in zone:
        att, dfn, _ = fr.threat_ratio(a)
        if att > 0 and (dfn == 0 or att / dfn >= DANGER):
            danger.add(a)

    def free(pr, need_attack=True):
        us = [u for u in fr.units.get(pr, ()) if u[0] == "us" and u[3] not in used
              and fr.div_army.get(u[3]) in mine
              and post(fr.div_army.get(u[3])) != "reserve"
              and (not need_attack or post(fr.div_army.get(u[3])) == "attack")]
        if pr in danger or len([u for u in fr.units.get(pr, ()) if u[0] == "us"]) <= KEEP:
            return []
        keep = sorted((u for u in fr.units.get(pr, ()) if u[0] == "us"),
                      key=lambda u: -u[2])[:KEEP]
        return [u for u in us if u not in keep]

    # 0. armies whose battle plan the game rates red deal no blows: the balance is counted
    # per direction (each army's arrows), not over the whole theatre (the user, 25.10)
    red = set()
    for m in ops.theatre_armies(g, theatre_id):
        e = ops.plan_eval(g, m)
        if e["colour"] == "red" and post(ops._hid(g, m)) == "attack":
            red.add(ops._hid(g, m))
            out.append(("RED PLAN", "a%s: the game rates its plan %s, no blows from it" % (
                ops._hid(g, m), ops.plan_eval_text(e))))

    # 1. the thinnest points, struck from every side
    targets = {b for a in zone for b in mv.adj.get(a, ()) if b in fr.their_line}
    for _ in range(MAX_BLOWS):
        best = None
        for b in targets:
            if not fr.n_div[("them", b)]:
                continue                       # empty: a grab, below
            src = [a for a in mv.adj.get(b, ()) if a in zone]
            pool = [u for a in src for u in free(a) if fr.div_army.get(u[3]) not in red]
            if not pool:
                continue
            ter = mv.terrain.get(b, "plains")
            D = front._doctrine()
            dirs = sorted({where[u[3]] for u in pool})
            cap = D.width_limit(ter, len(dirs))
            att, width, chosen = 0.0, 0.0, []
            for u in sorted(pool, key=lambda u: -fr.att_on(u, ter)):
                if width and width + u[4] > cap:
                    continue
                att, width = att + fr.att_on(u, ter), width + u[4]
                chosen.append(u)
            att *= fr.attack_mult(b, dirs)
            dfn = fr.hold("them", b, dirs)
            r = att / dfn if dfn else 99.0
            city = (mv.city_at.get(b) or (None, 0))[1]
            # among the blows that win, the strongest first (no railway weighing: the
            # railway feeds how many divisions stand here, not whether those win; the
            # user, 25.10)
            score = (min(r, 9), city)
            if r >= min_ratio and (best is None or score > best[0]):
                best = (score, b, chosen, r)
        if not best:
            break
        _, b, chosen, r = best
        targets.discard(b)
        for u in chosen:
            order(u, b, "BLOW %s %.1f:1" % (front._p(mv, b), r))

    # 3. empty enemy provinces of the line: the nearest free division, the fast first
    grabs = 0
    for b in sorted(targets, key=lambda q: -(mv.city_at.get(q) or (None, 0))[1]):
        if fr.n_div[("them", b)] or grabs >= MAX_GRABS:
            continue
        ter = mv.terrain.get(b, "plains")
        cand = [u for a in mv.adj.get(b, ()) if a in zone for u in free(a)
                if (u[5].get(ter, (0.0, 0.0))[0] if len(u) > 5 else 0.0) > -0.15]
        if not cand:
            continue
        cand.sort(key=lambda u: (not _fast(u), -fr.att_on(u, ter)))
        order(cand[0], b, "GRAB %s" % front._p(mv, b))
        grabs += 1
    return out
