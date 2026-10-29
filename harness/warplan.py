"""The war plans: one per war to come, written by the planner, read in parts by those who
carry them out, and checked against the game before a war is declared.

The user, 23.10: the China war of the last run stood a year before Beiping because nobody
owned the operational plan. The planner wrote one line of war_plan; the field marshals
never saw it and chose their own axes; the admiralty and the air staff knew nothing of
landings or of the sky over the main blow; nothing checked that a war was prepared before
it was declared.

A war: {enemy, starts, aim, phases: [{name, when, main_effort: {theatre, objectives,
how}, hold, landings: [{from_state, targets, divisions, force, sea}], air, special}],
stalemate, ready_when: {main_axis_divisions, marine_battalions, para_battalions,
free_convoys, attacks_drawn, other}}.
"""
import collections
import re

from . import bridge as B

STATE_LIST = {"type": "array", "items": {"type": "integer"}}

SCHEMA = {
    "type": "array",
    "description": "THE WAR PLANS, one per war to come (the field marshals, the admiralty, "
                   "the air and army staffs each read their part; a war is declared only "
                   "when its ready_when is met). Phases in order; a state is named by its "
                   "id (#NNN).",
    "items": {
        "type": "object",
        "properties": {
            "enemy": {"type": "string", "description": "tags joined by +, e.g. CHI+HBC+SHX+SND"},
            "starts": {"type": "string", "description": "how and when it begins: a game "
                       "event (and about when), or we declare it when ready"},
            "aim": {"type": "string", "description": "what victory is: capitulation, the "
                    "states to take, the peace wanted"},
            "phases": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "when": {"type": "string", "description": "the day or the trigger"},
                "main_effort": {"type": "object", "properties": {
                    "theatre": {"type": "string", "description": "the theatre (name or "
                                "id) that strikes or holds; its land must join the "
                                "objectives': land across the sea or behind another country "
                                "(Malaya from India) needs a theatre of its own, write 'new: "
                                "<name>' and Le Chonk opens it"},
                    "objectives": STATE_LIST,
                    "how": {"type": "string", "description": "the blow: the breakthrough, "
                            "the pockets, the cities and hubs to take first"}}},
                "hold": {"type": "string", "description": "theatres and stretches that "
                         "only hold, and with how little"},
                "landings": {"type": "array", "items": {"type": "object", "properties": {
                    "from_state": {"type": "integer", "description": "our port's state"},
                    "targets": STATE_LIST,
                    "divisions": {"type": "integer"},
                    "force": {"type": "string", "description": "which divisions: marines, "
                              "infantry, the army that lands"},
                    "sea": {"type": "string", "description": "the sea regions the navy "
                            "must hold for it"}}}},
                "air": {"type": "string", "description": "where air superiority, where "
                        "close air support, what bombing"},
                "special": {"type": "string", "description": "paratroopers, mountaineers, "
                            "marines: on what"}}}},
            "stalemate": {"type": "string", "description": "what is done when the front "
                          "stands: landings, paradrops on cities and supply hubs, a new "
                          "axis"},
            "ready_when": {"type": "object", "properties": {
                "main_axis_divisions": {"type": "integer", "description": "divisions in "
                                        "the first phase's main-effort theatre"},
                "marine_battalions": {"type": "integer"},
                "para_battalions": {"type": "integer"},
                "free_convoys": {"type": "integer"},
                "attacks_drawn": {"type": "boolean", "description": "the main effort's "
                                  "attacks drawn before the war"},
                "other": {"type": "string", "description": "what else must stand, checked "
                          "by Le Chonk: naval supremacy at sea X, the air wings at Y"}}},
            "go_by": {"type": "string", "description": "YYYY.MM.DD: the war starts by "
                      "this date on what stands, READY WHEN met or not, when waiting costs "
                      "more than readiness gains (the enemy grows stronger, a greater power "
                      "wakes or joins him). Leave out only when waiting costs nothing."},
        },
        "required": ["enemy", "aim", "phases", "ready_when"],
    },
}


def normalize(wars):
    """The planner's list, cleaned for keeping."""
    if isinstance(wars, str):
        import json
        try:
            wars = json.loads(wars)
        except ValueError:
            return []
    out = []
    for w in wars or []:
        if not isinstance(w, dict) or not w.get("enemy"):
            continue
        w = dict(w)
        w["enemy"] = "+".join(t.strip().upper() for t in str(w["enemy"]).replace(",", "+").split("+")
                              if t.strip())
        w["phases"] = [p for p in (w.get("phases") or []) if isinstance(p, dict)]
        w["ready_when"] = w.get("ready_when") if isinstance(w.get("ready_when"), dict) else {}
        out.append(w)
    return out


def tags(war):
    return set(war["enemy"].split("+"))


def find(plan, tag):
    for w in (plan or {}).get("wars") or []:
        if tag in tags(w):
            return w
    return None


def _st(mv, ids):
    out = []
    for s in ids or []:
        try:
            out.append(mv.label(int(s)) if mv else "#%s" % s)
        except (TypeError, ValueError):
            continue
    return ", ".join(out)


def _phase(mv, p, i):
    me = p.get("main_effort") or {}
    bits = ["PHASE %d %s (%s):" % (i + 1, p.get("name") or "", p.get("when") or "?")]
    if me:
        bits.append("main effort %s on %s%s;" % (me.get("theatre") or "?",
                                                _st(mv, me.get("objectives")) or "?",
                                                " (%s)" % me["how"] if me.get("how") else ""))
    if p.get("hold"):
        bits.append("hold: %s;" % p["hold"])
    for ld in p.get("landings") or []:
        bits.append("landing from %s on %s, %s divisions (%s), sea: %s;" % (
            _st(mv, [ld.get("from_state")]) or "?", _st(mv, ld.get("targets")) or "?",
            ld.get("divisions") or "?", ld.get("force") or "?", ld.get("sea") or "?"))
    if p.get("air"):
        bits.append("air: %s;" % p["air"])
    if p.get("special"):
        bits.append("special forces: %s;" % p["special"])
    return " ".join(bits)


def render(mv, war, part=None, theatre=None, ctx=None):
    """The war plan as one reader reads it. part: None (all), 'army', 'navy', 'air',
    'theatre' (with theatre = (id, name): the phases where it strikes or holds)."""
    rw = war.get("ready_when") or {}
    head = "WAR PLAN AGAINST %s (%s): %s" % (war["enemy"], war.get("starts") or "?",
                                            war.get("aim") or "")
    phases = war.get("phases") or []
    # a war already on is not waiting for READY WHEN (25.10, n3c: "from 1938.04 when READY
    # WHEN stands" kept Le Chonk and Kan'in on hold through a running war with China)
    on = bool(ctx) and bool(set(str(war["enemy"]).upper().split("+"))
                            & set(ctx.get("enemies") or ()))
    if on:
        head += ("\nTHE WAR IS ON: phase 1 is NOW, whatever its dates say. READY WHEN does "
                 "not stop an attack: strike wherever we win today (1.5:1 or better), and "
                 "let the forces READY WHEN names flow in for the later blows.")
    if part is None:
        lines = [head] + [_phase(mv, p, i) for i, p in enumerate(phases)]
        if war.get("stalemate"):
            lines.append("IF THE FRONT STANDS: %s" % war["stalemate"])
        lines.append("READY WHEN: %s" % ", ".join("%s %s" % kv for kv in rw.items()))
        return "\n".join(lines)
    if part == "theatre":
        tid, name = theatre
        mine = []
        # the phase now and the next only (25.10, n3: Kan'in read four phases, the first
        # "to about 1938.04.30" still telling him to strike Beiping in late May, beside Le
        # Chonk's "no attack")
        first = 0
        held = (ctx or {}).get("holders") or {}
        own = (ctx or {}).get("own")
        for i, p in enumerate(phases):
            objs = [int(s) for s in (p.get("main_effort") or {}).get("objectives") or []
                    if str(s).isdigit()]
            if objs and own and all(held.get(s, (None, None))[1] == own for s in objs):
                first = i + 1
            else:
                break
        for i, p in enumerate(phases):
            if i < first or len(mine) >= 2:
                continue
            me = p.get("main_effort") or {}
            t = str(me.get("theatre") or "")
            role = None
            if (t and (t == str(tid) or t.lower() in name.lower() or name.lower() in t.lower())) \
                    or (ctx and _theatre_id(ctx, t, me.get("objectives") or ()) == tid):
                role = "YOU ARE THE MAIN EFFORT"
            elif name.lower() in str(p.get("hold") or "").lower():
                role = "you hold"
            if role:
                mine.append("%s. %s" % (role, _phase(mv, p, i)))
        if not mine:
            return ""
        out = [head] + mine
        if war.get("stalemate"):
            out.append("IF THE FRONT STANDS: %s" % war["stalemate"])
        return "\n".join(out)
    if part == "navy":
        ls = ["phase %d (%s, %s): from %s on %s, %s divisions (%s), sea: %s;" % (
                  i + 1, p.get("name") or "", p.get("when") or "?",
                  _st(mv, [ld.get("from_state")]) or "?", _st(mv, ld.get("targets")) or "?",
                  ld.get("divisions") or "?", ld.get("force") or "?", ld.get("sea") or "?")
              for i, p in enumerate(phases) for ld in p.get("landings") or []]
        return (head + "\nTHE LANDINGS THE NAVY CARRIES AND COVERS (hold the sea before, "
                "escort the convoys): " + " ".join(ls) + (" Convoys needed: %s." %
                rw["free_convoys"] if rw.get("free_convoys") else "")) if ls else ""
    if part == "air":
        ls = ["phase %d (%s): %s" % (i + 1, p.get("name") or "", p["air"])
              for i, p in enumerate(phases) if p.get("air")]
        return (head + "\nTHE AIR WAR OF THE PLAN: " + "; ".join(ls)) if ls else ""
    if part == "army":
        need = ["%s %s" % kv for kv in rw.items() if kv[0] != "attacks_drawn"]
        land = sum(int(ld.get("divisions") or 0) for p in phases for ld in p.get("landings") or [])
        spec = "; ".join(p["special"] for p in phases if p.get("special"))
        return (head + "\nFORCES THE PLAN NEEDS: %s%s%s" % (
            ", ".join(need) or "?", "; landing forces %d divisions" % land if land else "",
            "; special forces: %s" % spec if spec else ""))
    return head


# ------------------------------------------------------------------ readiness
def _battalions(g):
    """Counter of battalion types in our divisions."""
    from . import army
    tm = {}
    for t in g.division_templates():
        tm[t["name"]] = collections.Counter(
            b for b in army._battalions(g, int(t["ptr"], 16), army.TMPL_BATTALIONS) if b)
    out = collections.Counter()
    for d in g.divisions(limit=3000)["divisions"]:
        out.update(tm.get(d["template"], {}))
    return out


def _theatre_id(ctx, name, objectives=()):
    """The theatre a phase names; failing the name, the one whose land touches the most of
    its objective states (the plan is written before the theatres are opened)."""
    t = str(name or "")
    for tid, m in ctx["marshals"].items():
        if t and (t == str(tid) or t.lower() in m.theatre["name"].lower()
                  or m.theatre["name"].lower() in t.lower()):
            return tid
    if not objectives or not ctx.get("marshals"):
        return None
    try:
        from . import agents
        reg = agents.regions(ctx)
    except Exception:
        return None
    mv = ctx["mv"]
    best = None
    for tid, states in reg.items():
        mine = {q for s in states for q in mv.land_provinces(s)}
        touch = sum(1 for o in objectives
                    if any(b in mine for q in mv.land_provinces(int(o)) for b in mv.adj.get(q, ())))
        if touch and (best is None or touch > best[0]):
            best = (touch, tid)
    return best[1] if best else None


def readiness(ctx, war):
    """[(item, have, want, ok)] for what the game can count; 'other' is Le Chonk's."""
    from . import navy, ops
    gw = ctx["gw"]
    rw = war.get("ready_when") or {}
    phases = war.get("phases") or []
    me = (phases[0].get("main_effort") or {}) if phases else {}
    tid = _theatre_id(ctx, me.get("theatre"), me.get("objectives") or ())
    out = []

    def look(g):
        r = {}
        bats = _battalions(g)
        r["marine"] = sum(n for k, n in bats.items() if k.startswith("marine")
                          or k.startswith("amphibious"))
        r["para"] = sum(n for k, n in bats.items() if "paratrooper" in k)
        r["convoys"] = navy.convoys(g)["free"]
        if tid is not None:
            ms = ops.theatre_armies(g, tid)
            r["axis"] = sum(len(ops.division_bases(g, m)) for m in ms)
            r["arrows"] = any(B.arr(g.p, o, ops.OI_CHILDREN) for m in ms for o in ops._fronts(g, m))
        return r
    have = gw.run(look)
    if rw.get("main_axis_divisions"):
        want = int(rw["main_axis_divisions"])
        h = have.get("axis")
        out.append(("divisions in the main-effort theatre %s" % (me.get("theatre") or "?"),
                    h if h is not None else "no such theatre", want, h is not None and h >= want))
    objs = [int(x) for x in me.get("objectives") or [] if str(x).lstrip("-").isdigit()]
    if tid is not None and objs:
        from .garrison import masses
        own = ctx.get("own")
        mine = {sid for sid, (o, c) in ctx["holders"].items() if c == own}
        th = next((m.theatre for t, m in ctx["marshals"].items() if t == tid), {})
        reach = set()
        for comp in masses(ctx["mv"], mine):
            if comp & set(th.get("states") or ()):
                reach |= comp
        cut = [x for x in objs if x in mine and x not in reach]
        if cut:
            out.append(("our objectives the main-effort theatre %s cannot reach by land "
                        "(across the sea or another country: they need a theatre of their "
                        "own, define_theatre)" % (me.get("theatre") or tid),
                        ", ".join(map(ctx["mv"].label, cut[:6])), "none", False))

        want = int(rw["marine_battalions"])
        out.append(("marine battalions", have["marine"], want, have["marine"] >= want))
    if rw.get("para_battalions"):
        want = int(rw["para_battalions"])
        out.append(("paratrooper battalions", have["para"], want, have["para"] >= want))
    if rw.get("free_convoys"):
        want = int(rw["free_convoys"])
        out.append(("free convoys", have["convoys"], want, have["convoys"] >= want))
    wg = war_goal(ctx, war)
    if wg:
        out.append(("war goal", wg[0], "held, justified in time or brought by an event",
                    wg[1]))
    if rw.get("attacks_drawn"):
        a = have.get("arrows")
        out.append(("attacks drawn in the main-effort theatre", "yes" if a else "no", "yes",
                    bool(a)))
    return out


_GOAL_CACHE = {}
_HELD_CACHE = {}       # foe tags -> (day, tags a war goal is held against)


def war_goal(ctx, war):
    """(have, ok) for the war goal a declaration needs, or None when the war is on.
    24.10: on 1938.02.08 the game refused Le Chonk's declaration ("no valid war goal") after
    he had moved GO BY three times; he never justified one. A war the game's own event
    brings (the Kwantung Army's Marco Polo) needs none: the user, 24.10, political power is
    not spent on a justification the event makes needless."""
    foe = tags(war)
    if foe & set(ctx.get("enemies") or ()):
        return None
    key = (str(ctx.get("date", ""))[:10], tuple(sorted(foe)))
    if key in _GOAL_CACHE:
        return _GOAL_CACHE[key]
    from . import politics
    dip = B.module("diplomacy")
    # the dry declaration scans every war goal and holds the game ~2 s: once a week per
    # war (25.10, n3d: two coming wars evicted each other from a one-entry cache, and Le
    # Chonk's turn froze the game 2 s of every 2.5 for half a minute)
    day = ctx.get("day") or 0
    hk = tuple(sorted(foe))
    old = _HELD_CACHE.get(hk)
    fresh = old is not None and 0 <= day - old[0] < 7

    def _held(g):
        # the war goals are scanned once (~2 s), not once per country (25.10, n3d: eight
        # countries of the Pacific war held the game 2 s each, every week)
        out = []
        own = g._inj is None
        if own:
            g.attach()
        try:
            cands = [int(w["ptr"], 16) for w in dip.wargoals(g)]
            for t in sorted(foe):
                for wg in cands:
                    try:
                        if dip.declare_war(g, t, wargoal_ptr=wg, dry_run=True)[0]:
                            out.append(t)
                            break
                    except Exception:
                        pass    # a failed check is cached too, or every step pays again
        finally:
            if own:
                g.detach()
        return out

    def look(g):
        held = old[1] if fresh else _held(g)
        _HELD_CACHE[hk] = (day if not fresh else old[0], held)
        own = g.player_tag()
        just = [j for j in dip.justifying(g) if j["from"] == own and j["target"] in foe]
        event = [w for w in politics.coming_wars(g) if foe & set(w["targets"])]
        return held, just, event
    try:
        held, just, event = ctx["gw"].run(look)
    except Exception as e:
        print("    .. war goal check failed: %r" % e, flush=True)
        return None
    if held:
        r = ("held against %s" % ", ".join(held), True)
    elif just:
        r = ("being justified against %s, %s days left" % (
            just[0]["target"], just[0]["days_left"]), True)
    elif event:
        w = event[0]
        r = ("none needed: the game's event %s brings the war in %d days (when %s runs out); "
             "a declaration BEFORE that needs a justified war goal" % (
                 w["event"], w["days_left"], w["mission"]), True)
    else:
        r = ("NONE: the game refuses a declaration without one; justify_war_goal (the game "
             "sets weeks to months and the political power)", False)
    for k in [k for k in _GOAL_CACHE if k[0] != key[0]]:
        del _GOAL_CACHE[k]
    _GOAL_CACHE[key] = r
    return r


def readiness_line(ctx, war):
    rows = readiness(ctx, war)
    miss = [r for r in rows if not r[3]]
    other = (war.get("ready_when") or {}).get("other")
    go = war.get("go_by")
    return "%s: %s%s%s" % (
        "READY" if not miss else "NOT READY",
        "; ".join("%s %s of %s%s" % (r[0], r[1], r[2], "" if r[3] else " (SHORT)") for r in rows)
        or "nothing the game can count",
        "; and, by your own judgement: %s" % other if other else "",
        "; GO BY %s: from then the war starts on what stands" % go if go else "")


def soft_ok(ctx, war, miss, despite):
    """Whether a declaration may go ahead short of READY WHEN (the user, 23.10: be
    reasonable: when time presses, as for Japan against China before America wakes, or
    the enemy is weak, earlier is better). The attacks drawn stay a must: that is the
    operational preparation. Returns (ok, why)."""
    hard = [r for r in miss if r[0].startswith("attacks drawn")]
    if hard:
        return False, "the attacks are not drawn"
    go = war.get("go_by")
    if go and str(ctx.get("date", "")) >= go:
        return True, "the plan's GO BY %s has come" % go
    if despite and len(str(despite).strip()) >= 20:
        return True, "declared short of READY WHEN because: %s" % despite
    return False, ""


def gaps(ctx, wars):
    """What a war plan lacks for its executors, with the means to fill it: [text]."""
    mv = ctx["mv"]
    names = {tid: m.theatre["name"] for tid, m in (ctx.get("marshals") or {}).items()}
    out = []
    for w in wars:
        foe = tags(w)
        for i, p in enumerate(w.get("phases") or []):
            me = p.get("main_effort") or {}
            if not me.get("objectives"):
                states = sorted((sid for sid, (o, c) in ctx["holders"].items() if o in foe),
                                key=lambda s: mv.label(s))
                out.append("%s, phase %d: the main effort has no objectives: name the states by "
                           "id (the marshal draws his attacks on them). %s's states: %s" % (
                               w["enemy"], i + 1, w["enemy"],
                               ", ".join(mv.label(s) for s in states[:90])))
            t = str(me.get("theatre") or "")
            if names and t and _theatre_id(ctx, t, me.get("objectives") or ()) is None:
                out.append("%s, phase %d: no theatre is named %r; the theatres now: %s (name "
                           "one of them, or Le Chonk opens it as a new theatre)" % (
                               w["enemy"], i + 1, t, ", ".join(
                                   "%s (%d)" % (n, k) for k, n in names.items())))
            for ld in p.get("landings") or []:
                if not ld.get("targets"):
                    out.append("%s, phase %d: a landing has no target states (by id)"
                               % (w["enemy"], i + 1))
        st = _stalled(ctx, w)
        if st:
            out.append(st)
    return out


def _stalled(ctx, w):
    """A war on whose front cannot be broken head on, and a plan that does not turn it.
    25.10, n3d: the planner rewrote phase 1 as "hold until 1.5:1 (1939.06)" and dropped
    the Tianjin landing, while the front stood. The user: if the front cannot be broken,
    the supreme looks for a way round: landings, paratroopers (Japan holds the sea and
    the air)."""
    foe = tags(w)
    if not foe & set(ctx.get("enemies") or ()):
        return None
    gw, held, own = ctx.get("gw"), ctx.get("holders") or {}, ctx.get("own")
    if gw is None:
        return None
    from . import ops

    def best(g):
        vals = []
        for og in ops.armies(g).values():
            if g.p.u8(og + 0x39) or not ops._fronts(g, og):
                continue
            # only armies with an attack drawn: no arrow is an idle army, not a front
            # that cannot be broken (the IDLE alert speaks for it)
            if not any(B.arr(g.p, o, ops.OI_CHILDREN) for o in ops._fronts(g, og)):
                continue
            e = ops.plan_eval(g, og)
            if e["his"] > 0:                      # it faces the enemy
                vals.append(e["value"])
        return max(vals) if vals else None
    try:
        b = gw.run(best)
    except Exception:
        return None
    if b is None or b > 0.0:
        return None
    phases = w.get("phases") or []
    cur = next((i for i, p in enumerate(phases)
                if not all(held.get(int(x), (None, None))[1] == own
                           for x in ((p.get("main_effort") or {}).get("objectives") or [])
                           if str(x).isdigit())), 0)
    near = phases[cur:cur + 1]
    if any(p.get("landings") for p in near) or "para" in str(
            [p.get("special") for p in near]).lower():
        return None
    return ("%s, phase %d: the front stands (the best plan of our armies facing him is "
            "rated %+.2f by the game, nothing green) and this phase does not turn it. A war "
            "on whose front cannot be broken head on is turned NOW: a landing behind his "
            "line (landings: an enemy port or coast with its hub, near the main effort's "
            "objectives), paratroopers on his hubs and cities (special), or another axis. "
            "Holding until the odds improve is not a plan." % (w["enemy"], cur + 1, b))


def landings(ctx, war):
    """Each landing of the war plan with what the executors need to make it: the port
    province, the coast provinces, the landing armies and their theatres, the invasion
    drawn or not. [(theatre id or None, text, planned?)]"""
    from . import ops
    mv, gw = ctx["mv"], ctx["gw"]

    def look(g):
        L = ops.landing_divisions(g)
        th = {}
        for tid in ctx["marshals"]:
            for m in ops.theatre_armies(g, tid):
                th[ops._hid(g, m)] = tid
        out = []
        for aid, og in ops.armies(g).items():
            ds = ops.division_bases(g, og)
            n = sum(d in L for d in ds)
            if n and n * 2 >= len(ds):
                out.append((aid, n, th.get(aid), bool(ops._fronts(g, og))))
        return out
    force = gw.run(look)
    res = []
    for i, p in enumerate(war.get("phases") or []):
        me = p.get("main_effort") or {}
        tid = _theatre_id(ctx, me.get("theatre"), me.get("objectives") or ())
        for ld in p.get("landings") or []:
            fs = ld.get("from_state")
            ports = [q for q in (mv.land_provinces(int(fs)) if fs else []) if q in mv.ports]
            coast = [q for s in ld.get("targets") or [] for q in mv.land_provinces(int(s))
                     if q in mv.coast]
            planned = [a for a in force if ops.INVASIONS.get(a[0])]
            txt = ("LANDING (phase %d %s) from %s%s on %s%s, %s divisions; landing armies: %s; "
                   "invasion planned: %s" % (
                       i + 1, p.get("name") or "", mv.label(int(fs)) if fs else "?",
                       " (port p%s)" % ports[0] if ports else " (NO PORT OF OURS THERE)",
                       ", ".join(mv.label(int(s)) for s in ld.get("targets") or []) or "?",
                       " (coast %s)" % ", ".join("p%d" % q for q in coast[:6]) if coast else "",
                       ld.get("divisions") or "?",
                       "; ".join("army %d (%d marine divisions, %s%s)" % (
                           a, n, "theatre %s" % t if t is not None else "in no theatre",
                           ", ON A SECTOR" if f else "") for a, n, t, f in force) or "NONE",
                       ", ".join("army %d op %s" % (a[0], ops.INVASIONS[a[0]]) for a in planned)
                       or "no"))
            res.append((tid, txt, bool(planned)))
    return res


# ------------------------------------------------------------------ the theatre's drawn plan
def main_phase(ctx, theatre):
    """(war, phase number, phase) of the first phase whose main effort is this theatre and
    whose objectives are not all ours yet; None when the plan gives it no blow."""
    from .agents import theatre_enemies
    th = theatre
    mine = theatre_enemies(ctx, th)
    held = ctx["holders"]
    for w in (getattr(ctx.get("planner"), "plan", None) or {}).get("wars") or []:
        if not mine & set(tags(w)):
            continue
        for i, p in enumerate(w.get("phases") or []):
            me = p.get("main_effort") or {}
            objs = [int(s) for s in me.get("objectives") or [] if str(s).isdigit()]
            if not objs or _theatre_id(ctx, me.get("theatre"), objs) != th["id"]:
                continue
            if all(held.get(s, (None, None))[1] == ctx["own"] for s in objs):
                continue
            return w, i, p
    return None


def theatre_check(ctx, theatre, armies):
    """WAR PLAN CHECK: the attacks drawn in the theatre against the blow the war plan gives
    it (24.10, the user: the arrows are poor and borders are left bare; the war plans must
    be whole before the war. Kan'in drew two arrows into the same city, Beiping, and none
    to Tianjin, Jinan or Qingdao, and nobody told him)."""
    found = main_phase(ctx, theatre)
    if not found:
        return []
    war, i, p = found
    mv, held = ctx["mv"], ctx["holders"]
    objs = [int(s) for s in p["main_effort"]["objectives"] if str(s).isdigit()]
    out = []
    go = war.get("go_by")
    head = "WAR PLAN CHECK (%s%s): " % (
        p.get("name") or "phase %d" % (i + 1), ", go by %s" % go if go else "")
    reach = {}                       # state id -> armies whose arrows reach it
    firsts = collections.defaultdict(list)
    shallow, none = [], []
    on_line = [a for a in armies if a["sector"]]
    for a in armies:
        for x in a["attacks"]:
            ids = []
            for lab in list(x["route"]) + list(x["then"]):
                mt = re.search(r"#(\d+)\)", lab)
                if mt:
                    ids.append(int(mt.group(1)))
            for s in ids:
                reach.setdefault(s, set()).add(a["army"])
            ends = [s for s in ids if held.get(s, (None, None))[1] != ctx["own"]]
            for s in ends[:2]:           # where the arrow breaks in, not where it meets
                firsts[s].append(a["army"])
            if len(ends) <= 1:
                shallow.append("army %d (%s)" % (a["army"], " > ".join(x["route"][-1:])))
    for a in on_line:
        if not a["attacks"]:
            none.append(str(a["army"]))
    landed = {int(t) for ld in p.get("landings") or [] for t in ld.get("targets") or []
              if str(t).isdigit()}
    miss = [s for s in objs if s not in reach and s not in landed
            and held.get(s, (None, None))[1] != ctx["own"]]
    if miss:
        out.append("objectives of the phase no arrow reaches: %s" % ", ".join(
            mv.label(s) for s in miss))
    for s, ars in firsts.items():
        if len(set(ars)) > 1:
            out.append("armies %s break in at the same place, %s: one blow at one point "
                       "(right for a breakthrough; a pocket is two arrows breaking in either "
                       "side of his mass and meeting behind it)" % (" and ".join(map(str, sorted(set(ars)))),
                                              mv.label(s)))
    if none:
        out.append("armies on the line with no arrow: %s (on the first day they only "
                   "stand)" % ", ".join(none))
    if shallow:
        out.append("arrows one state deep: %s (a shallow arrow is fought lazily and stops "
                   "when it arrives; chain the next objectives with then_states)"
                   % ", ".join(shallow))
    if not out:
        return [head + "every objective of the phase is under an arrow"]
    return [head + "; ".join(out) + ". Redraw what is wrong in this turn: the war opens on "
            "what is drawn, and every redraw before it costs only the days of planning "
            "already built"]
