"""Valuable coastal states of ours that no division holds (26.10, the user: Malaya,
Singapore and Hong Kong stood empty while three armies faced Italy in Africa; Malaya's oil
and rubber are what Japan comes for, and a naval power lands where nobody stands)."""
import glob
import os
import re

from . import bridge as B

_RES = None
REACH = 8        # divisions on one land mass of ours that reach its states by land


def resources():
    """{state id: {resource: amount}} from history/states."""
    global _RES
    if _RES is None:
        _RES = {}
        for path in glob.glob(os.path.join(B.game_dir(), "history", "states", "*.txt")):
            txt = re.sub(r"#[^\n]*", "", open(path, encoding="utf-8-sig", errors="replace").read())
            m = re.search(r"\bid\s*=\s*(\d+)", txt)
            rm = re.search(r"\bresources\s*=\s*\{([^}]*)\}", txt)
            if m and rm:
                _RES[int(m.group(1))] = {k: float(v) for k, v in
                                         re.findall(r"(\w+)\s*=\s*([\d.]+)", rm.group(1))}
    return _RES


_KEYS = None


def strait_keys(mv):
    """{state id: [strait or canal]}: the states holding the provinces an adjacency rule
    requires (map/adjacency_rules.txt; who holds them decides who passes)."""
    global _KEYS
    if _KEYS is None:
        _KEYS = {}
        txt = re.sub(r"#[^\n]*", "", open(os.path.join(B.game_dir(), "map", "adjacency_rules.txt"),
                                          encoding="utf-8-sig", errors="replace").read())
        for blk in re.split(r"\badjacency_rule\s*=", txt)[1:]:
            n = re.search(r'\bname\s*=\s*"([^"]+)"', blk)
            r = re.search(r"required_provinces\s*=\s*\{([^}]*)\}", blk)
            if not n or not r:
                continue
            name = n.group(1).replace("_", " ").title().replace("Strait", "strait").replace(
                "Canal", "canal")
            for pv in re.findall(r"\d+", r.group(1)):
                s = mv.state_of.get(int(pv))
                if s is not None and name not in _KEYS.setdefault(s, []):
                    _KEYS[s].append(name)
    return _KEYS


def masses(mv, ours):
    """[set of state ids]: our land in pieces joined by our own land only."""
    out, seen = [], set()
    for start in ours:
        if start in seen:
            continue
        comp, todo = set(), [start]
        while todo:
            x = todo.pop()
            if x in comp:
                continue
            comp.add(x)
            todo.extend(n for n in mv.neighbours.get(x, ()) if n in ours and n not in comp)
        seen |= comp
        out.append(comp)
    return out


def split_theatres(mv, ours, theatres):
    """Lines for theatres whose states lie on land masses that our land does not join (26.10,
    the user: Malaya sat in the India theatre, cut off from it by Siam and the sea)."""
    where = {}
    for i, comp in enumerate(masses(mv, ours)):
        for x in comp:
            where[x] = i
    out = []
    for th in theatres:
        parts = {}
        for sid in th.get("states") or ():
            if sid in where:
                parts.setdefault(where[sid], []).append(sid)
        if len(parts) < 2:
            continue
        rows = sorted(parts.values(), key=len, reverse=True)
        if all(len(r) < 2 for r in rows[1:]):
            continue          # an island or two off the main land (Shetland, Kuwait) is fine
        out.append("THEATRE %s %s SPANS %d LAND MASSES our land does not join (its armies on "
                   "the main one cannot march to the rest): %s. Land across the sea or behind "
                   "another country is its own theatre: define_theatre for it with an army "
                   "there (or a garrison under guard_points), e.g. South-East Asia and Oceania "
                   "apart from India." % (
                       th.get("id"), th.get("name"), len(rows), "; ".join(
                           "%s%d states (%s)" % ("main: " if i == 0 else "", len(r),
                                                 ", ".join(mv.label(x) for x in r[:5])
                                                 + (", ..." if len(r) > 5 else ""))
                           for i, r in enumerate(rows[:7]))))
    return out


SEA_HOPS = 3     # sea provinces between our coast and an enemy's that make a state front-line
FRONT_LINE = 40  # its weight: an empty island in the enemy's sea is taken first (26.10, the
                 # user: Malta empty, Italian fleets all round it; it ranked 19th of 26)


def enemy_coasts(mv, provs, holders, enemies, hops=SEA_HOPS):
    """The enemies whose coast lies within `hops` sea provinces of these provinces."""
    seen, todo, out = set(), [], set()
    for a in provs:
        todo += [(b, 1) for b in mv.adj.get(a, ()) if b not in mv.land]
    while todo:
        q, d = todo.pop()
        if q in seen:
            continue
        seen.add(q)
        for b in mv.adj.get(q, ()):
            if b in mv.land:
                c = holders.get(mv.state_of.get(b), (None, None))[1]
                if c in enemies:
                    out.add(c)
            elif d < hops and b not in seen:
                todo.append((b, d + 1))
    return out


def held_states(g, mv, subs=None):
    """{state id: divisions of ours and our subjects' in it}."""
    from .navy import _prov_id
    if subs is None:
        try:
            subs = {t for t, _ in B.module("diplomacy").subjects(g)}
        except Exception:
            subs = set()
    held = {}
    divs = list(g.divisions(limit=5000)["divisions"])
    for t in subs:            # a subject's own divisions hold its land too
        try:
            divs += g.divisions(t, limit=2000)["divisions"]
        except Exception:
            pass
    for d in divs:
        try:
            pid = _prov_id(g, int(d["province_ptr"], 16))
        except Exception:
            continue
        s = mv.state_of.get(pid)
        if s is not None:
            held[s] = held.get(s, 0) + 1
    return held


def unguarded(g, mv, holders):
    """[(score, state id, what it holds)] of our coastal states with something worth taking
    and no division of ours in them, the most valuable first."""
    own = g.player_tag()
    try:
        subs = {t for t, _ in B.module("diplomacy").subjects(g)}
    except Exception:
        subs = set()
    held = held_states(g, mv, subs)
    res = resources()
    ours = {sid for sid, (o, c) in holders.items() if o in subs | {own} and c in subs | {own}}
    held_by_land = {}
    for comp in masses(mv, ours):  # land masses of ours, and the divisions in each
        n_div = sum(held.get(x, 0) for x in comp)
        for x in comp:
            held_by_land[x] = n_div
    keys = strait_keys(mv)
    try:
        enemies = set(B.module("combat").enemy_tags(g))
    except Exception:
        enemies = set()
    out = []
    for sid, (o, c) in holders.items():
        if o not in subs | {own} or c not in subs | {own} or held.get(sid):
            continue
        key = keys.get(sid)
        # land joined by our own states to a mass of divisions is reached by land (the
        # home islands' 22-division reserve stood in a few states only); a strait is not
        provs = mv.provinces.get(sid, set())
        if not key and (held_by_land.get(sid, 0) >= REACH or not provs & mv.coast):
            continue
        r = res.get(sid, {})
        port = max((mv.ports.get(p, 0) for p in provs), default=0)
        fac = sum(mv.buildings.get(sid, {}).get(k, 0) for k in ("arms_factory", "industrial_complex"))
        vp = mv.vp.get(sid, 0)
        score = sum(r.values()) + vp + 6 * port + 4 * fac
        near = enemy_coasts(mv, provs & mv.coast, holders, enemies) if enemies else set()
        if near:
            score += FRONT_LINE
        if key:
            score += 1000
        elif score < 12:
            continue
        what = ", ".join("%s %g" % (k, v) for k, v in sorted(r.items(), key=lambda kv: -kv[1]))
        bits = [x for x in (what, "victory points %d" % vp if vp else "",
                            "naval base %d" % port if port else "",
                            "%d factories" % fac if fac else "") if x]
        if o != own:
            bits.insert(0, "our subject %s" % o)
        if near:
            bits.insert(0, "ENEMY COAST WITHIN REACH (%s): a landing can come any day" %
                        ", ".join(sorted(near)))
        if key:
            bits.insert(0, "HOLDS THE %s" % " AND THE ".join(key).upper())
        out.append((score, sid, "; ".join(bits)))
    out.sort(reverse=True)
    return out


def lines(g, mv, holders, theatres=(), top=16):
    rows = unguarded(g, mv, holders)
    out = []
    if rows:
        out.append(
            "UNGUARDED COASTAL STATES (ours and our subjects', worth taking, NO division in them; "
            "a naval power lands where nobody stands, and the resources go with the state): "
            + "; ".join("%s: %s" % (mv.label(s), w) for _, s, w in rows[:top])
            + ". Give each that a naval power can reach a garrison: an army in its theatre on "
              "guard_points with kinds ports (and cities), before the war, not after the landing. "
              "A state that HOLDS a strait or a canal keeps a garrison of its own ALWAYS, "
              "whatever stands near: who holds it decides whose fleets and convoys pass.")
    try:
        subs = {t for t, _ in B.module("diplomacy").subjects(g)}
    except Exception:
        subs = set()
    own = g.player_tag()
    ours = {sid for sid, (o, c) in holders.items() if o in subs | {own} and c in subs | {own}}
    return out + split_theatres(mv, ours, theatres) + orphans(g, mv, ours, theatres)


def orphans(g, mv, ours, theatres, top=6):
    """Pieces of our land (2+ states, or a strait) in no theatre and with no division:
    nobody answers for them (26.10: Malaya, Borneo, Hong Kong; the war plan gave Malaya's
    defence to the India theatre, which holds none of it)."""
    inth = {sid for th in theatres for sid in th.get("states") or ()}
    keys = strait_keys(mv)
    res = resources()
    have = held_states(g, mv)
    rows = []
    for comp in masses(mv, ours):
        n = sum(have.get(x, 0) for x in comp)
        if comp & inth or n >= 3:      # a dominion's own army (Canada, Australia) holds it
            continue
        if len(comp) < 2 and not any(x in keys for x in comp):
            continue
        val = sum(sum(res.get(x, {}).values()) + mv.vp.get(x, 0) for x in comp)
        rows.append((val, sorted(comp, key=lambda x: -mv.vp.get(x, 0)), n))
    if not rows:
        return []
    rows.sort(key=lambda r: -r[0])
    return ["OUR LAND IN NO THEATRE, 0-2 DIVISIONS ON IT (no field marshal answers for it; "
            "a theatre's armies cannot march there across the sea or another country): " + "; ".join(
                "%d states, %d divisions (%s%s)" % (len(c), n, ", ".join(mv.label(x) for x in c[:5]),
                                                    ", ..." if len(c) > 5 else "")
                for _, c, n in rows[:top])
            + ". Where a naval power can land, open a theatre of its own (define_theatre with an "
              "army moved or raised there: South-East Asia and Oceania is not India)."]
