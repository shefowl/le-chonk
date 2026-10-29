"""Briefings: what each commander is told at the start of a turn.

Facts only, gathered read-only and formatted for a model. No recommendations: a briefing
that said "attack Gdynia" would be the code deciding. Enemy forces are limited to the
states on our front line and our own states (a stand-in for fog of war, see README).
"""
import collections
import json
import re

from . import air, front, ops, threats, zone as zonepic
from .bridge import module as B_module
from .personas import persona


def _t():
    from . import agents
    return agents


def B_justifying(g):
    return ops.B.module("diplomacy").justifying(g)


def war_threat_lines(ctx, enemy=None):
    report = ctx.get("war_threats")
    if report is None:
        return ["WAR THREAT DATA: unavailable for this briefing"]
    return threats.lines(report, ctx["own"], enemy)


def _j(x):
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def _enemy_front(ctx, enemies):
    mv, h = ctx["mv"], ctx["holders"]
    ours, theirs = mv.front_states(h, ctx["own"], enemies)
    return ([mv.describe_state(s, h) for s in sorted(ours)],
            [mv.describe_state(s, h) for s in sorted(theirs)])


def lost_ground(ctx):
    """Our own states currently controlled by someone else."""
    mv, h, own = ctx["mv"], ctx["holders"], ctx["own"]
    return [mv.describe_state(s, h) for s, (o, c) in sorted(h.items()) if o == own and c != own]


def air_regions(mv):
    if not hasattr(mv, "air_regions"):
        mv.air_regions = air.Regions(mv.dir, mv.state_of)
    return mv.air_regions


def _mix(m):
    return ", ".join("%d %s" % (n, t) for t, n in m.items())


def self_plan_days(ctx):
    return ctx.get("plan_days") or 14


def _situation(ctx):
    gw, mv, h = ctx["gw"], ctx["mv"], ctx["holders"]
    A = _t()
    return gw.run(ops.situation, mv, h, A.regions(ctx), set(ctx["enemies"]), A.foes(ctx))


def _stakes(ctx, weigh):
    """{theatre: (factories in its states, on the homeland's soil)}. Homeland: our states
    joined to the capital's state over land through our own states."""
    gw, mv, h, own = ctx["gw"], ctx["mv"], ctx["holders"], ctx["own"]
    ours = {st for st, (_, c) in h.items() if c == own}

    def read(g):
        cap = g.p.i32(g.country(own).ptr + front.CO_CAPITAL_STATE)
        fac = {}
        for st in set().union(*(w[-1] for w in weigh)) & ours:
            try:
                b = g.state_detail(st)["buildings"]
            except Exception:
                continue
            fac[st] = sum(b.get(k, 0) for k in ("arms_factory", "industrial_complex", "dockyard"))
        return cap, fac
    cap, fac = gw.run(read)
    home, todo = {cap}, [cap]
    while todo:
        st = todo.pop()
        for q in mv.land_provinces(st):
            for b in mv.adj.get(q, ()):
                t = mv.state_of.get(b)
                if b in mv.land and t in ours and t not in home:
                    home.add(t)
                    todo.append(t)
    return {w[0]: (sum(fac.get(st, 0) for st in w[-1]), bool(set(w[-1]) & home)) for w in weigh}


def _thin(ctx, weigh, lines):
    """THIN BORDER: a theatre whose line has empty provinces and more enemy divisions
    next to it than ours on it, with its stakes and every other theatre's local counts.
    Headcount minus border length is arithmetic, not a claim that divisions are free.
    (i36, 12.1939: Savoy with 6 of 14 line provinces empty
    against 19 Italian divisions, refused three times "until the training divisions are
    full" while the North-East held 92 on its line.)"""
    try:
        stakes = _stakes(ctx, weigh)
    except Exception:
        stakes = {}
    hush = {tid for tid, m in ctx["marshals"].items() if _t().quiet(ctx, m)}
    rows = [w for w in weigh if w[6] and w[5] > w[4] and w[0] not in hush]
    rows.sort(key=lambda w: (not stakes.get(w[0], (0, False))[1], -stakes.get(w[0], (0,))[0]))
    for t, nm, n, ln, ours, his, e, _ in rows:
        f, home = stakes.get(t, (0, False))
        others = ["%d %s: %d assigned, %d line provinces, %d on line, %d enemy, "
                  "%d empty; headcount minus line %d" % (ot, name, assigned, length,
                                                         on, enemy, holes, assigned - length)
                  for ot, name, assigned, length, on, enemy, holes, _ in weigh if ot != t]
        lines.append(
            "THIN BORDER: theatre %d %s (%s, %d factories in its states), %d of its %d line "
            "provinces EMPTY, ours %d divisions on the line against his %d next to it. "
            "OTHER THEATRES (arithmetic only, not verified available divisions; positions, "
            "orders and travel still matter): %s"
            % (t, nm, "HOMELAND: a land road to the capital" if home else "overseas",
               f, e, ln, ours, his, "; ".join(others) or "none"))


def _theatre_overview(ctx, own_tid):
    """Compact facts on other theatres; each count is limited to its responsibility."""
    A, gw = _t(), ctx["gw"]
    lines = []
    for tid, m in ctx["marshals"].items():
        if tid == own_tid:
            continue
        try:
            faces = A.facing(ctx, m)
            war = faces & set(ctx["enemies"])
            view = gw.run(ops.command_view, ctx["mv"], ctx["holders"], tid,
                          faces or {m.theatre["enemy"]})
            fr = front.of(ctx, A.line_of(ctx, m))
            length, on, enemy, holes = front.counts(fr, front.responsibility_zone(fr, m.region(ctx)))
            lines.append("- %d %s: %s; %d assigned, %d line provinces, %d on line, "
                         "%d enemy next to line, %d empty" % (
                             tid, m.theatre["name"],
                             "WAR WITH " + "+".join(sorted(war)) if war else "PEACE on this border",
                             view["divisions"], length, on, enemy, holes))
        except (ops.OpError, OSError) as e:
            lines.append("- %d %s: data unavailable (%s)" % (tid, m.theatre["name"], e))
    return (["OTHER THEATRES (assigned headcount is not arrival; local border counts):"]
            + lines) if lines else []


def _groups(ctx, sit, oob, lines):
    """THEATRES, ARMY GROUPS and ARMIES, one line each."""
    gw, mv, h = ctx["gw"], ctx["mv"], ctx["holders"]
    A = _t()
    game = gw.run(ops.theatre_listing)
    try:
        from . import garrison
        lines.extend(gw.run(garrison.lines, mv, h, [m.theatre for m in ctx["marshals"].values()]))
    except Exception as e:
        lines.append("UNGUARDED COASTAL STATES: could not be read (%s)" % e)
    lines.append("THEATRES (each under its field marshal; its line is the whole border of "
                 "its states):")
    weigh = []
    for tid, m in ctx["marshals"].items():
        th = m.theatre
        try:
            v = gw.run(ops.command_view, mv, h, tid, A.facing(ctx, m) or {th["enemy"]})
            att = sum(1 for a in v["armies"] if a["attacks"])
            st = "%d of %d armies attacking" % (att, len(v["armies"]))
            secs = " | ".join("army %d: %s" % (a["army"], ", ".join(
                x.split(" (#")[0] for x in a["sector"]) or "reserve") for a in v["armies"])
            fr = front.of(ctx, A.line_of(ctx, m))
            zone = front.responsibility_zone(fr, m.region(ctx))
            secs += "; FRONT: " + gw.run(lambda g: front.summary(fr, zone))
            if A.at_war_in(ctx, m):
                # the plan against the theatre: his to settle (25.10, n3: the marshal got
                # "phase 1: strike Beiping" and Le Chonk's "no attack" side by side)
                from . import warplan
                chk = warplan.theatre_check(ctx, th, v["armies"])
                if chk and "every objective" not in chk[0]:
                    secs += ("; THE WAR PLAN AGAINST YOUR DIRECTIVE: " + chk[0].split(
                        ". Redraw")[0] + ". Either the directive or the plan changes: "
                        "say which (set_directive, or ask_for_new_plan)")
            n = v["divisions"]
            weigh.append((tid, th["name"], n) + front.counts(fr, zone) + (m.region(ctx),))
        except ops.OpError as e:
            st, secs, n = "error: %s" % e, "", 0
        kinds = collections.Counter(k for k, _, _ in sit.get(tid, []))
        trouble = ", ".join("%d %s" % (k_, k) for k, k_ in kinds.items() if k in ("HOLE", "POCKET"))
        lines.append("- %d %r under %s: states %s; borders %s (the blow expected from %s); "
                     "army groups %s; %d divisions; %s%s; sectors %s; directive: %s" % (
                         tid, th["name"], m.name,
                         ", ".join(mv.label(x) for x in sorted(m.region(ctx))),
                         ", ".join(sorted(A.facing(ctx, m))) or "none",
                         th["enemy"], game.get(tid, {}).get("army_groups") or "none", n, st,
                         "; " + trouble if trouble else "", secs, th["directive"]))
    _thin(ctx, weigh, lines)
    lines.extend(_main_effort_missing(ctx))
    for tid, t in game.items():
        if tid not in ctx["marshals"]:
            lines.append("- %d %r (THE GAME'S OWN GROUPING, NOT A THEATRE OF OURS: no field "
                         "marshal, no turns, no tactical staff, no orders; its armies stand "
                         "idle until define_theatre takes them in): army groups %s, armies %s"
                         % (tid, t["name"], t["army_groups"], t["armies"]))
    where = {x: tid for tid, t in game.items() for x in t["army_groups"] + t["armies"]}
    lines.append("ARMY GROUPS:")
    for row in oob["army_groups"]:
        lines.append("- %d under %s, armies %s, %d divisions, theatre %s" % (
            row["army_group"], row["field_marshal"], row["armies"], row["divisions"],
            where.get(row["army_group"])))
    lines.append("ARMIES:")
    for a in oob["armies"]:
        lines.append("- %d %s, %d div [%s]%s, theatre %s%s" % (
            a["army"], a["general"] or "no general", a["divisions"], _mix(a["composition"]),
            ", army group %d" % a["army_group"] if a["army_group"] else "",
            where.get(a["army"]), " OVER THE LIMIT" if a.get("OVER_LIMIT") else ""))
    un = gw.run(ops.unassigned_summary, mv)
    if un["total"]:
        lines.append("DIVISIONS IN NO ARMY: %d [%s]" % (un["total"], _mix(un["by_kind"])))


def _air(ctx, lw, lines, owner=None, kinds=None):
    """AIR FORCE and the regions over the fronts. owner/kinds: what one air staff sees
    as its own (ctx["air_owner"]: wing -> staff that last ordered it)."""
    mv = ctx["mv"]
    enemies = set(ctx["enemies"])
    planned = {m.theatre["enemy"] for m in ctx["marshals"].values()}
    owners = ctx.get("air_owner", {})
    tag = lambda ids: (" [ordered by the %s]" % ", ".join(sorted({owners[i] for i in ids
                                                                 if i in owners})))\
        if any(i in owners for i in ids) else ""
    fly = ["%s: %d %s wings on %s%s" % (f["region"], len(f["wings"]), f["kind"], f["mission"],
                                         tag(f["wings"]))
           for f in lw["flying"] if not kinds or f["kind"] in kinds]
    att = ["army %s: %d wings (%s)" % (aid, len(v["wings"]), _mix(v["kinds"]))
           for aid, v in lw["attached_to_armies (rebase and support them by themselves)"].items()]
    try:
        tags = [ctx["own"]] + sorted(enemies)
        st = ctx["gw"].run(air.strength, tags)
        lines.append("AIR BALANCE (planes in wings): " + "; ".join(
            "%s %d (%s)" % ("WE" if t == ctx["own"] else t, sum(st[t].values()),
                            ", ".join("%s %d" % kv for kv in sorted(st[t].items(),
                                                                    key=lambda x: -x[1])))
            for t in tags))
    except Exception:
        pass
    lines.append("AIR FORCE: flying: %s; attached: %s; idle: %s" % (
        "; ".join(fly) or "none", "; ".join(att) or "none",
        "; ".join("%d %s wings" % (len(v["wings"]), k) for k, v in lw["idle_by_kind"].items()
                  if not kinds or k in kinds) or "none"))
    regions = air_regions(mv)
    bs = ctx["gw"].run(air.bases, ctx["holders"], ctx["own"])
    # 24.10: "Kitakyushu: 0/600" was read as "no air base yet" and a fourth level was
    # ordered at the head of the queue; bases under 400 were not shown at all
    lines.append("OUR AIR BASES (every one; the level stands, the planes are what our wings "
                 "keep on it; %d planes a level; rebase_air moves wings between them; a wing "
                 "flies only over regions in range of its base): " % air.CAPACITY_PER_LEVEL
                 + "; ".join("%s: level %d, %d of %d planes" % (
                     mv.label(b["state"]), b["capacity"] // air.CAPACITY_PER_LEVEL,
                     b["planes"], b["capacity"]) for b in bs))
    for t in sorted(enemies | planned):       # before a war too: where the wings go
        ours, theirs = _enemy_front(ctx, {t})
        sts = [d["id"] for d in ours + theirs]
        if sts:
            lines.append("AIR REGIONS OVER THE FRONT WITH %s: %s" % (t, "; ".join(
                "%s: %s" % (regions.label(r), ", ".join(mv.name(x) for x in regions.states[r]
                                                        if x in sts))
                for r in regions.over(sts))))


def _main_effort_missing(ctx):
    """25.10, n3d: Le Chonk's define_theatre for China was refused twice (enemy states);
    he wrote "China theatre 122 opened" and the war stood under Kwantung North's hold."""
    from . import warplan
    pl = ctx.get("planner")
    wars = ((pl.plan or {}).get("wars") if pl else None) or []
    held, own = ctx.get("holders") or {}, ctx.get("own")
    mv, out = ctx["mv"], []
    for w in wars:
        if not warplan.tags(w) & set(ctx.get("enemies") or ()):
            continue
        for p in w.get("phases") or []:
            me = p.get("main_effort") or {}
            objs = [int(x) for x in me.get("objectives") or [] if str(x).isdigit()]
            if objs and all(held.get(x, (None, None))[1] == own for x in objs):
                continue
            t = str(me.get("theatre") or "")
            named = any(t and (t == str(tid) or t.lower() in m.theatre["name"].lower()
                               or m.theatre["name"].lower() in t.lower())
                        for tid, m in ctx["marshals"].items())
            if t and not named:
                try:
                    ours = {own} | set(ctx["gw"].run(
                        lambda g: B_module("combat").subject_tags(g)))
                except Exception:
                    ours = {own}
                near = sorted({n for o in objs for n in mv.neighbours.get(o, ())
                               if held.get(n, (None, None))[0] in ours})
                out.append("THE PLAN'S MAIN EFFORT HAS NO THEATRE: phase %r names %r, and no "
                           "theatre of ours is that one (ours: %s). Open it (define_theatre) "
                           "on OUR states on that line%s, with its armies and field marshal, "
                           "taking those states from the theatre that holds them; the enemy's "
                           "states are its objectives (the directive), never its states: we "
                           "attack FROM our land INTO his. Holding instead is a change of the "
                           "plan: say why to the planner." % (
                               p.get("name") or "", t, ", ".join(
                                   "%s %s" % (tid, m.theatre["name"])
                                   for tid, m in ctx["marshals"].items()) or "none",
                               ": " + ", ".join(mv.label(n) for n in near) if near else ""))
            idle = _main_effort_on_hold(ctx, [o for o in objs
                                              if held.get(o, (None, None))[1] != own])
            if idle:
                out.append(idle)
            break
        st = warplan._stalled(ctx, w)
        if st:
            out.append("THE FRONT STANDS: " + st + " Order the landing (the admiralty and "
                       "the theatre that lands) or call the planning session for it (ask_for_new_plan).")
    return out


_HOLD_WORDS = re.compile(r"hold only|only hold|no offensive|no attack|defen[cs]e only|"
                         r"defensive|stay on (the )?defen|hold the line", re.I)


def _main_effort_on_hold(ctx, objs):
    """The theatres of ours that border the main effort's objectives, when none of them
    attacks: its directive holds, or every army there stands on hold or in reserve.
    25.10, n3f: the plan moved the blow to Suiyuan; Le Chonk tried to redraw a theatre
    over it (refused: an enemy state), and the theatre on its border, Kwantung North,
    kept "hold only, no offensive"; nobody struck the main effort for a week."""
    if not objs:
        return None
    from . import agents
    mv = ctx["mv"]
    regs = agents.regions(ctx)
    objs = set(objs)
    touching = []
    for tid, m in ctx["marshals"].items():
        region = set(regs.get(tid, m.theatre["states"]))
        if not any(set(mv.neighbours.get(s, ())) & objs for s in region):
            continue
        pos = m.theatre.get("posture") or {}
        directive = str(m.theatre.get("directive") or "")
        held = bool(_HOLD_WORDS.search(directive)) or (
            pos and all(v != "attack" for v in pos.values()))
        touching.append((tid, m, held, directive))
    if not touching or not all(h for _, _, h, _ in touching):
        return None
    return ("THE MAIN EFFORT IS NOT STRUCK: the plan's objectives (%s) border %s, and %s. "
            "A theatre is never redrawn over the enemy's states: give the theatre on that "
            "border the attack (set_directive: attack <objective> with what it has, and "
            "Le Chonk's armies moved there if it needs more)." % (
                ", ".join(mv.label(o) for o in sorted(objs)),
                ", ".join("theatre %s %s (%s)" % (tid, m.theatre["name"], m.name)
                          for tid, m, _, _ in touching),
                "; ".join("%s holds (directive: %s%s)" % (
                    m.theatre["name"], d[:80] or "none",
                    "; postures " + ", ".join("%s %s" % kv for kv in sorted(
                        (m.theatre.get("posture") or {}).items())) if m.theatre.get("posture")
                    else "") for _, m, _, d in touching)))


def _commanders(ctx, oob, lines):
    gw = ctx["gw"]
    caps = ops.caps()
    lines.append("COMMAND POWER: %s (promotion to field marshal costs %d). LIMITS: an army "
                 "holds at most %d divisions; a field marshal leads at most %d armies"
                 % (gw.run(ops.command_power), caps["promotion_command_power"],
                    caps["army_divisions"], caps["field_marshal_armies"]))
    leads = {a["general"]: "army %d" % a["army"] for a in oob["armies"] if a["general"]}
    leads.update({r["field_marshal"]: "army group %d" % r["army_group"]
                  for r in oob["army_groups"] if r["field_marshal"]})
    for m in ctx["marshals"].values():
        leads[m.name] = ", ".join(x for x in (leads.get(m.name), "the theatre %s"
                                              % m.theatre["name"]) if x)
    busy = set()
    for rank, title in (("field_marshal", "FIELD MARSHALS available (lead army groups)"),
                        ("corps_commander", "GENERALS available (lead armies; can be "
                                            "promoted to field marshal)")):
        lines.append(title + " — key, attack/defense/planning/logistics, style:")
        for g_ in gw.run(ops.generals, rank)[:10]:
            if g_["key"] in busy:
                continue
            _, prof = persona(g_["key"], g_["name"])
            now = leads.get(g_["name"])
            lines.append("- %s %s a%d d%d p%d l%d%s: %s" % (
                g_["key"], g_["name"], g_["attack"], g_["defense"], g_["planning"],
                g_["logistics"], " [now leads %s]" % now if now else "",
                prof.split(".")[0] + "."))
    from . import leaders
    lines.extend(gw.run(leaders.lines))


def supreme(ctx):
    """Le Chonk's picture: fronts, alerts, the armies and groups one line each. (17.10:
    15.6k characters, a third of it JSON of armies and of borders with countries we do
    not touch.) The generals, the air regions and command power went to the army staff's
    briefing (18.10): the staff organises, Le Chonk decides."""
    gw, mv, h = ctx["gw"], ctx["mv"], ctx["holders"]
    enemies = set(ctx["enemies"])
    planned = {m.theatre["enemy"] for m in ctx["marshals"].values()}
    from . import economy
    wt = gw.run(lambda g: B_module("diplomacy").world_tension(g))
    from .planner import course_line
    lines = ["DATE: %s" % ctx["date"],
             "AT WAR WITH: %s" % (", ".join(sorted(enemies)) or "nobody"),
             course_line(),
             gw.run(economy.fuel_line)]
    lines += war_threat_lines(ctx)
    foes_all = sorted(set(enemies) | {m.theatre["enemy"] for m in ctx["marshals"].values()})
    if foes_all:
        lines.append("INTELLIGENCE ON THE ENEMY (army intel; the game's range for his "
                     "divisions, wide when intel is low): " + "; ".join(
                         gw.run(_intel_line, t) for t in foes_all[:6]))
    from . import politics as pol
    try:
        lines += gw.run(pol.coming_war_lines)
    except Exception:
        pass
    if wt is not None:
        lines.append("WORLD TENSION: %.1f%% (guarantees from 25%%); OUR GUARANTEES: %s"
                     % (wt * 100, ", ".join(sorted(ctx.get("guarantees") or ())) or "none"))
    try:
        fac = gw.run(lambda g: [t for t in g.faction_members(g.player_tag())["members"]
                                if isinstance(t, str)])
    except OSError:                  # in no faction: the pointer is not a faction
        fac = []
    lines.append("OUR FACTION: %s" % (", ".join(fac) if fac else
                 "none: we fight alone; join_faction asks a faction's leader to take us in"))
    # what the factories make against what the army uses (20.10: "rifles at the absolute
    # head until the stockpile turns positive" held all winter; the army in full needs
    # 68,000 rifles, and no line made a truck, a tank or a gun)
    from . import army_needs
    e = gw.run(economy.picture, mv)
    lines.append("MILITARY FACTORIES BY LINE: " + "; ".join(
        "%s %s" % (eq.split(" ")[0], f) for _, eq, f, _, naval in e["lines"]
        if not naval and f not in (0, "0")) + " (of %d)" % e["military_factories"])
    lines += gw.run(army_needs.picture, e["lines"], e["stockpile"])
    idle = gw.run(base_idle)
    if idle:
        lines.append("THE BASE STANDS: no factory makes %s (rifles, artillery, support "
                     "equipment and trucks always run)" % ", ".join(idle))
    bal = gw.run(ops.balance, mv, h, enemies | planned) if enemies | planned else {}
    neighbours = {h.get(n, (None, None))[1] for sid, (_, c) in h.items() if c == ctx["own"]
                  for n in mv.neighbours.get(sid, ())} - {ctx["own"], None}
    for tag in sorted(enemies | planned | neighbours | set(ctx.get("neighbours_of_interest", []))):
        try:
            line = gw.run(ops.border_line, mv, h, tag)
        except Exception:
            line = None
        if not line:
            continue
        head = ("FRONT WITH %s (at war)" % tag if tag in enemies else
                "FRONT WITH %s (planned, not at war)" % tag if tag in planned else
                "BORDER WITH %s (not at war)" % tag)
        b = bal.get(tag)
        if b:
            head += ": our %d divisions near it, theirs seen %d" % (
                b["our_divisions_in_the_border_zone"], b["enemy_divisions_seen_there"])
        lines.append("%s; our states along it: %s" % (head, " | ".join(", ".join(st) for st in line)))
    for j in [j for j in gw.run(B_justifying) if j["from"] == ctx["own"]]:
        lines.append("JUSTIFYING A WAR GOAL against %s: %s days left" % (j["target"], j["days_left"]))
    for tag in sorted(planned - enemies):
        todo = ctx["planning"](tag)
        qt = [m for m in ctx["marshals"].values() if m.theatre["enemy"] == tag]
        if qt and all(_t().quiet(ctx, m) for m in qt):
            lines.append("QUIET BORDER WITH %s (at peace; no war with it is planned or set off "
                         "by the game, our war is with %s): a thin screen is enough here; "
                         "the divisions belong to the theatres of that war first, and this "
                         "border gets more only if %s masses against it" % (
                             tag, ", ".join(sorted(set(enemies) | set(ctx.get("coming_wars")
                                                                      or ()) | set(ctx["war_wanted"]))),
                             tag))
            continue
        if not enemies and tag in (ctx.get("coming_wars") or ()):
            # 23.10: the China war comes by event; "at peace" stripped the attacks from the
            # list and the China Front was told to dig in, no arrow drawn
            try:
                days = min(w["days_left"] for w in gw.run(pol.coming_wars)
                           if tag in w["targets"])
            except Exception:
                days = None
            lines.append("WAR AGAINST %s COMES BY THE GAME'S EVENT%s (we do not declare it): "
                         "have the theatres deployed and their attacks drawn before it, so the "
                         "planning bonus is full on the first day (it builds for %d days); tell the "
                         "marshals 'draw, do not execute', never 'no offensive', which they "
                         "read as no attacks drawn; %s"
                         % (tag, " in about %d days" % days if days is not None else "",
                            self_plan_days(ctx), "deployed and planned" if todo == "ready"
                            else "not ready yet: " + "; ".join(todo)))
            continue
        if not enemies and tag not in ctx["war_wanted"]:
            # (19.10: "READY ... declare_war opens it" read as an invitation, and the
            # supreme of a France of 1936 twice tried to declare war on Germany)
            todo = [] if todo == "ready" else [t for t in todo if "attack" not in t]
            lines.append("OUR THEATRES FACING %s (we are at peace with it): %s"
                         % (tag, "deployed on the line" if not todo else "; ".join(todo)))
            continue
        lines.append("WAR AGAINST %s: %s" % (tag, "READY: the theatres' armies are deployed and "
                     "their attacks planned; declare_war opens it" if todo == "ready" else
                     "not ready yet: " + "; ".join(todo)))
    sit = _situation(ctx)
    hush = {str(tid) for tid, m in ctx["marshals"].items() if _t().quiet(ctx, m)}
    al = [t for _, _, t in sit.get(None, [])
          if not (t.startswith("HOLE") and re.search(r"\(theatre (\d+)\)", t)
                  and re.search(r"\(theatre (\d+)\)", t).group(1) in hush)]
    lw = gw.run(air.summary, air_regions(mv))
    if enemies & planned:          # the wars our theatres fight, not the colonies
        for a in gw.run(ops.allies_out_of_war, mv, h, enemies & planned):
            al.append("ALLY OUT OF OUR WAR (call_ally): %s, %d divisions, not at war with %s%s"
                      % (a["ally"], a["divisions"], ", ".join(a["not_at_war_with"]),
                         "; it BORDERS %s: its border is closed to our attack and its "
                         "divisions stand idle" % ", ".join(a["borders"]) if a["borders"] else ""))
    covered = set().union(*_t().regions(ctx).values()) if ctx["marshals"] else set()
    for t, foe, n, empty in gw.run(ops.buffer_borders, mv, h, planned | enemies, covered):
        if empty:
            # 19.10: the whole Belgian border stood empty in 1939, 48 divisions in Lorraine
            al.append("BUFFER BORDER: our border with %s (neutral, but it borders %s): %d of "
                      "its %d provinces have no division of ours, and its states are in no "
                      "theatre. A blow round a fortified line comes through such a country; "
                      "give these states to the theatre against %s (redraw_theatre; no war "
                      "with %s is implied)" % (t, foe, empty, n, foe, t))
    try:
        from . import intel
        br = gw.run(intel.broken)
        if br:
            al.append("BROKEN CIPHERS: %s. reveal_intel gives 30 days of +15%% breakthrough "
                      "and defence against that country; keep it for the opening of an "
                      "offensive or a landing" % ", ".join(br))
    except Exception:
        pass
    if al:
        lines.append("ALERTS:\n- " + "\n- ".join(al))
    lost = lost_ground(ctx)
    if lost:
        lines.append("OUR STATES HELD BY THE ENEMY: " + ", ".join(
            "%s (%s)" % (d["name"], d.get("controller")) for d in lost))
    ev = gw.run(ops.pending_events)
    if ev:
        lines.append("EVENTS WAITING FOR YOUR DECISION (answer_event): " + _j(ev))
    req = gw.run(ops.requests)
    if req:
        lines.append("REQUESTS FROM OTHER COUNTRIES, WAITING FOR YOUR ANSWER (answer_request; "
                     "unanswered they lapse): " + _j([{k: r[k] for k in ("request", "what")}
                                                      for r in req]))
    oob = gw.run(ops.order_of_battle)
    _groups(ctx, sit, oob, lines)
    fly = ["%s: %d %s wings on %s" % (f["region"], len(f["wings"]), f["kind"], f["mission"])
           for f in lw["flying"]]
    lines.append("AIR FORCE (the air staff commands it; order_minister it): flying: %s; "
                 "idle: %s" % ("; ".join(fly) or "none", "; ".join(
                     "%d %s wings" % (len(v["wings"]), k) for k, v in lw["idle_by_kind"].items())
                     or "none"))
    staff = ctx.get("staff")
    if staff and staff.memory:
        lines.append("ARMY STAFF'S LATEST NOTE: " + staff.memory[-1])
    from . import exile
    try:
        lines += gw.run(exile.lines)
    except Exception:
        pass
    return "\n".join(lines)


def _army_lines(a):
    """One army of a group, as a few short lines."""
    mix = ", ".join("%d %s" % (n, t) for t, n in a["mix"].items())
    out = ["army %d (%s%s): %d divisions [%s], org %s, strength %s" % (
        a["army"], a["general"] or "no general",
        ", army group %d" % a["army_group"] if a.get("army_group") else "",
        a["divisions"], mix, a["org"], a["hp"])]
    if a["sector"]:
        dep = "%d on the line" % a.get("on_the_line", 0)
        if a.get("marching_to_it"):
            dep += ", %d marching (up to %d provinces away)" % (
                a["marching_to_it"]["divisions"], a["marching_to_it"]["farthest_provinces_away"])
        if a.get("no_land_route_to_it"):
            dep += ", %d with no land route to it" % a["no_land_route_to_it"]
        off = a["divisions"] - a["on_sector"]
        if off:
            dep += ", %d on no sector" % off
            sea = a.get("off_sector_overseas")
            if sea:
                dep += (" (%d of them across the sea: %s; they cannot walk to the line: "
                        "ship_by_sea brings them over, or the staff swaps them for local "
                        "divisions)" % (
                            sum(n for _, n in sea), ", ".join("%s %d" % kv for kv in sea)))
        out.append("  sector: %s; %s" % (", ".join(a["sector"]), dep))
        if a.get("plan"):
            # the game's own colour of the army's orders (the user, 25.10: a red plan is
            # never launched, a yellow one may be)
            out.append("  the game rates its plan %s" % a["plan"])
        if a.get("hq"):
            out.append("  " + a["hq"])
    else:
        out.append("  sector: none (in reserve)")
    for x in a["attacks"]:
        out.append("  attack: %s%s (%d divisions, %s)" % (
            " > ".join(x["route"]), "".join(" >> " + t for t in x["then"]), x["divisions"],
            "executing" if x["executing"] else "not executing"))
    if a["sector"] and not a["attacks"]:
        out.append("  attack: none")
    if a.get("targets"):
        out.append("  targets: " + "; ".join(a["targets"]))
    return out


def _enemy_line(ctx, m):
    """Whom the theatre expects: his whole army and what of it faces the theatre."""
    gw, tag = ctx["gw"], m.theatre["enemy"]
    total = gw.run(lambda g: len(ops.B.arr(g.p, g.country(tag).ptr, 0x290)))
    fr = front.of(ctx, tag)
    zone = gw.run(front.group_zone, fr, m.theatre["id"])
    facing = {b for a in zone for b in ctx["mv"].adj.get(a, ()) if b in fr.their_line}
    allies = sorted(t for t in ctx["enemies"] if t != tag) if tag in ctx["enemies"] else []
    return ("THE ENEMY EXPECTED: %s%s, %d divisions in all, %d of them facing your theatre "
            "(%s)%s"
            % (tag, " (AT WAR WITH US)" if tag in ctx["enemies"] else " (at peace)",
               total, fr.divisions("them", facing), front.ours_of(fr.worth("them", facing)),
               "; we are also at war with %s" % ", ".join(allies[:8]) if allies else ""))


def _intel_line(g, tag):
    """Our army intel on a country and the division count range the game would show:
    +-80% at 5% intel, narrowing to the exact count at 70% (ARMY_ARMY_COUNT_RANGE_*).
    Centred on the true count: the game's own random offset is not reproduced."""
    try:
        i = g.intel_on(tag)["army"] / 100.0
        n = len(ops.B.arr(g.p, g.country(tag).ptr, 0x290))
    except Exception:
        return "%s ?" % tag
    r = 0.8 * (1 - min(1.0, max(0.0, (i - 0.05) / 0.65)))
    return "%s %d%%: %d-%d divisions" % (tag, round(i * 100), round(n * (1 - r)), round(n * (1 + r)))


def _plan_lines(ctx, m):
    """THE THEATRE PLAN and how the line stands against it."""
    gw, mv, th = ctx["gw"], ctx["mv"], m.theatre
    pl = th.get("plan")
    if not pl:
        return []
    out = ["YOUR THEATRE PLAN (of %s): threats: %s" % (pl["date"], pl["threats"]),
           "  layout: " + "; ".join("army %d on %s at %s a province" % (
               r["army"], ", ".join(mv.label(x) for x in r["states"]), r["per_province"])
               for r in pl["layout"]),
           "  reserve: %s; fallback: %s; plan again when: %s" % (
               pl.get("reserve") or "none", pl.get("fallback") or "none", pl["replan_when"])]
    stretch = gw.run(ops.army_stretches, th["id"])
    check = []
    for r in pl["layout"]:
        divs, provs = stretch.get(r["army"], (0, 0))
        if not provs:
            check.append("army %d holds no stretch (plan: %s a province)" % (
                r["army"], r["per_province"]))
            continue
        have = divs / float(provs)
        if have < 0.7 * r["per_province"]:
            check.append("army %d holds %d provinces with %d divisions, %.1f a province "
                         "against %s planned" % (r["army"], provs, divs, have, r["per_province"]))
    # by worth, not heads (25.10, n3: Beiping "grew from 5 to 40" divisions a third of ours
    # each, and the plan's trigger cancelled the blow); a plan written before has no worth
    if pl.get("seen_worth") is not None:
        seen = gw.run(zonepic.seen_by_state, mv, ctx["holders"], m.region(ctx), th["enemy"],
                      worth=True)
        was = {int(k): v for k, v in pl["seen_worth"].items()}
        for st, n in seen.most_common():
            if st is not None and n >= 3 and n >= 1.5 * was.get(st, 0) + 1.5:
                check.append("the enemy seen in %s grew from worth %d to worth %d of our "
                             "divisions" % (mv.label(st), round(was.get(st, 0)), round(n)))
    out.append("PLAN CHECK: " + ("; ".join(check) + ". Act, or ask_replan if the plan no "
                                 "longer fits" if check else "the line stands as planned"))
    return out


def marshal_budget(m, due):
    """Bound local planning without changing the separate strategic planner."""
    if not hasattr(m, "base_reasoning"):
        m.base_reasoning, m.base_max_tokens, m.base_budget = m.reasoning, m.max_tokens, m.budget_s
    m.planning = bool(due)
    m.reasoning = 3072 if due else m.base_reasoning
    m.max_tokens = 8192 if due else m.base_max_tokens
    m.budget_s = 60 if due else m.base_budget


def marshal(ctx, m):
    A = _t()
    gw, mv, th = ctx["gw"], ctx["mv"], m.theatre
    # a planning session or an ordinary turn (the user, 21.10: rules piled on rules and
    # still gaps; the theatre is thought through once, then executed and checked)
    due = m.plan_due(ctx)
    marshal_budget(m, due)
    tid, region, faces = th["id"], m.region(ctx), A.facing(ctx, m)
    war_with = faces & set(ctx["enemies"])
    coming = (set(faces) | {th["enemy"]}) & set(ctx.get("coming_wars") or ())
    from .agents import theatre_enemies
    enemies = war_with or theatre_enemies(ctx, m.theatre) or {th["enemy"]}
    line = A.line_of(ctx, m)
    for a in gw.run(lambda g: [ops._hid(g, x) for x in ops.theatre_armies(g, tid)
                               if ops._fronts(g, x)]):
        gw.run(ops.consolidate, mv, ctx["holders"], a, line)
    v = gw.run(ops.command_view, mv, ctx["holders"], tid, faces or enemies, True, enemies)
    pieces = gw.run(ops.theatre_line, mv, ctx["holders"], region)
    groups = sorted({a["army_group"] for a in v["armies"] if a["army_group"]})
    lines = (["THEATRE PLANNING SESSION (%s). Think the theatre through, then write YOUR "
              "THEATRE PLAN (set_theatre_plan): where the enemy will strike and why, what "
              "each stretch of the line needs (the forts, the ground, his divisions seen), "
              "which army holds what at how many divisions a province, where the reserve "
              "stands and when it strikes, which ally's front you hold (hold_ally_front) and "
              "what you do when an ally falls, the fallback line, and what would make you "
              "plan again. Carry it out in this turn (set_sectors in line order, its states "
              "are length hints; compare the returned actual states with your layout; "
              "hold_line, hold_ally_front), and ask Le Chonk for the divisions it lacks. "
              "YOUR CRAFT is at the end of this briefing." % due] if due else []) + [
             "DATE: %s" % ctx["date"],
             "YOUR THEATRE: %s (theatre %d), %d divisions%s; its states: %s; %s"
             % (th["name"], tid, v["divisions"],
                ", army groups %s" % ", ".join(map(str, groups)) if groups else "",
                ", ".join(mv.label(x) for x in sorted(region)),
                "AT WAR WITH %s" % ", ".join(sorted(war_with)) if war_with else
                "NOT AT WAR YET; THE WAR WITH %s COMES BY THE GAME'S EVENT (we do not start "
                "it): draw your attacks now and leave them unexecuted, so they are ripe on "
                "its first day; nothing crosses the border before it. A drawn attack moves "
                "no division until it is executed: 'no offensive' or 'hold' before the war "
                "means do not EXECUTE, and the attacks are drawn all the same"
                % ", ".join(sorted(coming)) if coming else
                "NOT AT WAR YET: nothing crosses the border before the war; if your "
                "directive is an offensive, draw the attacks now so they ripen; if it is a "
                "defence, set the line and the reserve"),
             "DIRECTIVE FROM LE CHONK: %s" % th["directive"]] + (
                ["LE CHONK'S LATEST WORD (%s, after the directive; where they differ, it "
                 "rules): %s" % tuple(th["word"])] if th.get("word") else []) + [

             "YOUR ORDER is Le Chonk's: his directive, and his latest word where it differs. "
             "The war plan above is the background he commands from: where it differs from "
             "his order, his order stands, and you tell him (report_to_supreme) what the "
             "difference costs. Within his order the how is yours: the armies' stretches, "
             "their postures, the blows and the reserve.",
             _enemy_line(ctx, m)] + gw.run(zonepic.picture, mv, ctx["holders"], region,
                                           th.get("forward") or (), th["enemy"]) + [
             "YOUR LINE, the whole border of your theatre with %s, from its FIRST end (state, "
             "provinces of the line in it); set_sectors cuts it among your armies in this "
             "order: %s" % (", ".join(sorted(faces)) or "nobody",
                            ops.describe_line(mv, pieces) or "no border with anyone now")]
    lines += war_threat_lines(ctx, th["enemy"])
    lines += _theatre_overview(ctx, tid)
    forts = collections.OrderedDict()
    for pc in pieces:
        for i in pc["idx"]:
            pr = pc["line"][i]
            forts.setdefault(mv.state_of.get(pr), []).append(mv.forts.get(pr, 0))
    if forts:
        # the user, 19.10: all five armies of the group sat at the Maginot line
        lines.append("FORTS ON YOUR LINE (levels of its provinces, as they stand now): "
                     + "; ".join("%s %s" % (mv.label(st), "/".join(map(str, lv)))
                                 for st, lv in forts.items() if st is not None))
    # his forts across the line (20.10: the harness read only the 1936 forts, saw no
    # Westwall, and the North-East attacked across the Rhine into it for weeks)
    ours = {pc["line"][i] for pc in pieces for i in pc["idx"]}
    held = ctx["holders"]
    his = collections.OrderedDict()
    for a in sorted(ours):
        for b in mv.adj.get(a, ()):
            st = mv.state_of.get(b)
            if b in mv.land and mv.forts.get(b) and held.get(st, (None, None))[1] in faces:
                his.setdefault(st, {})[b] = mv.forts[b]
    if his:
        lines.append("HIS FORTS ACROSS YOUR LINE (province: level; an attack into a fort "
                     "loses 15% of its strength per level): " + "; ".join(
                         "%s %s" % (mv.label(st), ", ".join("p%d: %d" % kv for kv in pv.items()))
                         for st, pv in his.items()))
    intent = ctx["supreme"].memory[-1:] if ctx.get("supreme") else []
    if intent:
        # the field marshal fights better knowing what his theatre is for (the user, 19.10)
        lines.append("LE CHONK'S INTENT FOR THE WAR (his latest note): %s"
                     % intent[0].split(": ", 1)[-1][:500])
    began = [d for t, d in (ctx.get("war_began") or {}).items() if t in war_with]
    if began and ctx.get("day") is not None and ctx["day"] - min(began) <= 14:
        lines.append(
            "THE WAR IS %d DAYS OLD: his line is at its thinnest now, and within a week he "
            "pours divisions into every city we threaten (n3: Beiping held 5 Chinese "
            "divisions on the first day and 50 a week later). Strike now where WEAK POINTS "
            "are best, with every army that touches the point, his great cities and hubs "
            "first; execute this turn. A mountain objective waits."
            % (ctx["day"] - min(began)))
    if not th.get("executing", True) and war_with:
        lines.append("YOUR PLAN IS STOPPED (you ordered it): nothing attacks or moves until "
                     "you execute again")
    al = m.alerts(ctx)
    if al:
        order = {"HOLE": 0, "POCKET": 1, "IDLE": 2, "NO SECTOR": 3}
        lines.append("ALERTS:\n- " + "\n- ".join(t for k, _, t in sorted(
            al, key=lambda x: order.get(x[0], 9))))
    fr = front.of(ctx, line)
    zone = gw.run(front.group_zone, fr, tid)
    if zone and war_with:
        c = fr.collapse(zone)
        spear = gw.run(lambda g: ops.spearheads(g, [a["army"] for a in v["armies"]]))
        if c["broken"] and not spear:
            # 14.05.1940: Bock wrote "the French line has broken" and kept his slow arrows
            # on Le Chonk's "keep the arrows as drawn"
            lines.append("BREAKTHROUGH: his front before you has broken (%d of %d provinces "
                         "of his line empty, %d divisions of his against our %d) and none of "
                         "your armies has a spearhead. Give the "
                         "armoured and motorised armies pursue now, to THE FAR END below. A "
                         "spearhead re-cuts no sector and loses no planning bonus (it has "
                         "none): \"keep the arrows\" does not forbid it."
                         % (c["holes"], c["line"], c["his"], c["ours"]))
    if zone:
        lines.append("YOUR FRONT, PROVINCE BY PROVINCE (p<id> = a province; ratios are "
                     "attack:defence after terrain, rivers, forts and combat width):")
        lines.append(gw.run(lambda g: front.picture(fr, zone)))
    if war_with:
        # where our weight stands against his, and his coast with nobody on it (23.10, the
        # user: 30 divisions stood in the Gobi while six bled on Beiping, and a wall of
        # hundreds of divisions was never turned from the sea)
        try:
            bal = gw.run(ops.line_weight, mv, ctx["holders"], tid, sorted(war_with | set(ctx["enemies"])))
        except Exception:
            bal = None
        if bal and bal["armies"]:
            lines.append("WEIGHT ALONG YOUR LINE (ours standing on the stretch : his within two "
                         "provinces of it, and what they are worth in divisions of ours): " +
                         "; ".join("army %d on %s: %d : %d (worth %d)" % (
                             r["army"], r["stretch"], r["on_stretch"], r["enemy_near"],
                             round(fr.worth("them", r.get("near") or ())))
                                   for r in bal["armies"]))
            ours = sum(r["on_stretch"] for r in bal["armies"])
            # each of his divisions once, by worth (25.10, n3: the stretches overlap, the
            # same Chinese were summed three times into "270 against our 32", and the wall
            # text told Kan'in every turn that no point of it could be broken)
            near = set().union(*[set(r.get("near") or ()) for r in bal["armies"]])
            heads = fr.divisions("them", near)
            his = fr.worth("them", near)
            if his >= 2.5 * max(1, ours):
                coast = bal["empty_coast"]
                lines.append(
                    "A WALL: he has %d divisions at your line, worth %d of ours, against our %d. "
                    "Where WEAK POINTS give 1.5:1 or better, strike there; beyond them the wall "
                    "is not broken head on: it is turned. From the sea: "
                    "a landing behind it (naval_invasion with an army off the line, marines "
                    "first; it needs convoys and the admiralty's naval supremacy on the way) "
                    "where his coast is empty%s. From the air: paratroopers on a city behind "
                    "it. Ask Le Chonk for the landing force and the fleet; meanwhile the line "
                    "holds thin where he is thin and the weight stands where the blow will "
                    "go." % (heads, round(his), ours, ": " + ", ".join(
                        "p%d %s (%d provinces behind the line, %d of his beside it%s)" % (
                            c["province"], c["state"], c["from_line"], c["enemy_beside"],
                            ", a port" if c["port"] else "") for c in coast) if coast else
                        " (none is now; it will be where he pulls his garrisons to the wall)"))
    # where each army's divisions stand (25.10, n3: 14 of 486's 19 stood in Chahar, 4 in
    # East Hebei, and nothing told Kan'in; he read "19 on the line")
    def _where(g):
        out = []
        for x in ops.theatre_armies(g, tid):
            c = collections.Counter(mv.label(mv.state_of.get(ops.province_of(g, d)))
                                    for d in ops.division_bases(g, x))
            out.append("army %d: %s" % (ops._hid(g, x), ", ".join(
                "%s %d" % kv for kv in c.most_common()) or "none"))
        return out
    try:
        lines.append("WHERE YOUR ARMIES' DIVISIONS STAND (by state): " + "; ".join(gw.run(_where)))
    except Exception:
        pass
    if war_with:
        # his divisions in all against those seen near our lines (the user, 25.10: what
        # is not at the front is in reserve or garrison, out of sight)
        try:
            total = gw.run(lambda g: sum(len(ops.B.arr(g.p, g.country(t).ptr, 0x290))
                                         for t in war_with))
            near = sum(gw.run(zonepic.seen_by_state, mv, ctx["holders"], region,
                              th["enemy"]).values())
            if total > near:
                lines.append("OUT OF SIGHT: %s have %d divisions in all; %d of them stand "
                             "near our lines here, so about %d are deeper (reserves, "
                             "garrisons, other fronts)" % ("+".join(sorted(war_with)), total,
                                                          near, total - near))
        except Exception:
            pass
    if war_with:
        try:
            lt = gw.run(ops.landing_targets, mv, ctx["holders"], set(ctx["enemies"]))
        except Exception:
            lt = []
        if lt:
            # 25.10, the user: landings break the Chinese line; a port behind it with little
            # around it is the best of blows (land: marines into the port, infantry beside)
            lines.append("LANDING TARGETS (his ports, the thinnest and deepest behind his line "
                         "first; resistance = what stands in the port and around it, in "
                         "divisions of ours): " + "; ".join(
                             "p%d %s%s, port %d, resistance %.1f, %d provinces behind his line, "
                             "beaches %s" % (x["port"], x["state"],
                                             " [%s]" % x["city"] if x["city"] else "",
                                             x["level"], x["resistance"], x["depth"],
                                             ",".join("p%d" % b for b in x["beaches"][:4]) or "none")
                             for x in lt))
        pos = th.get("posture") or {}
        lines.append("POSTURES (your tactical staff strikes with the attack armies): " +
                     "; ".join("army %d %s" % (a["army"], pos.get(str(a["army"])) or "attack")
                               for a in v["armies"]))
        if th.get("tactics_last"):
            lines.append("YOUR TACTICAL STAFF ON %s: %s" % tuple(th["tactics_last"]))
        elif any(a.get("on_sector") for a in v["armies"]) and all(
                (pos.get(str(a["army"])) or "attack") != "attack"
                for a in v["armies"] if a.get("on_sector")):
            # 25.10, n3c: Kan'in kept every army on hold for weeks of war, waiting for the
            # Beiping count; the staff struck nothing, not even the 3:1 points elsewhere
            lines.append("NO ARMY OF YOURS IS ON ATTACK: your tactical staff strikes nothing, "
                         "not even the weak points at 1.5:1 or better below. hold is for a "
                         "stretch Le Chonk orders held; an army waiting for its big blow still "
                         "takes the small ones on attack (set_posture).")
    lines.append("YOUR ARMIES (targets: enemy states the army can reach; p = provinces "
                 "away; div = enemy divisions seen there):")
    # what each army has lost since this marshal last looked (20.10: the North-East bled
    # 100,000 men against the Rhine and its marshal never saw a number for it)
    seen = m.__dict__.setdefault("army_strength", {})
    for a in v["armies"]:
        ls = _army_lines(a)
        was = seen.get(a["army"])
        try:
            now = float(a["hp"])
        except (TypeError, ValueError):
            now = None
        if was and now is not None and was[0] and abs(now - was[0]) / was[0] >= 0.02:
            ls[0] += " (was %.1f on %s: %+.0f%%)" % (was[0], was[1], 100 * (now / was[0] - 1))
        if now is not None:
            seen[a["army"]] = (now, ctx["date"][:10])
        lines += ls
    lost = [d["name"] for d in lost_ground(ctx) if d.get("controller") in enemies]
    if lost:
        lines.append("OUR STATES HELD BY THIS ENEMY: " + ", ".join(lost))
    if m.last_divisions is not None and v["divisions"] < m.last_divisions:
        lines.append("LOSSES SINCE YOUR LAST TURN: %d divisions"
                     % (m.last_divisions - v["divisions"]))
    m.last_divisions = v["divisions"]
    lines += _plan_lines(ctx, m)
    if not war_with:
        # before the war only: at war it told Kan'in to draw arrows his orders forbade;
        # the supreme reads it there and settles it (25.10, n3)
        try:
            from . import warplan
            lines += warplan.theatre_check(ctx, th, v["armies"])
        except Exception as e:
            print("    .. war plan check failed: %r" % e, flush=True)
    lines += operations_lines(ctx)
    if m.planning:
        from . import doctrine
        lines.append("YOUR CRAFT:" + doctrine.MARSHAL)
    return "\n".join(lines)


def events(ctx):
    """For Le Chonk's quick turn on events: the events and the bare situation."""
    gw = ctx["gw"]
    return "\n".join([
        "DATE: %s" % ctx["date"],
        "AT WAR WITH: %s" % (", ".join(sorted(ctx["enemies"])) or "nobody"),
        "YOUR STRATEGIC INTENT (your notes): " + _j(ctx["supreme"].memory[-3:]),
        "EVENTS WAITING FOR YOUR DECISION: " + _j(gw.run(ops.pending_events)),
        "REQUESTS FROM OTHER COUNTRIES, WAITING FOR YOUR ANSWER (unanswered they lapse): "
        + _j([{k: r[k] for k in ("request", "what")} for r in gw.run(ops.requests)]),
        "Answer each event with answer_event and each request with answer_request, then "
        "end_turn."])


def industry(ctx):
    """The minister's picture: factories, lines, stockpile, resources, construction,
    research."""
    from . import economy
    gw, mv = ctx["gw"], ctx["mv"]
    e = gw.run(economy.picture, mv)
    idle = e["military_factories"] - e["military_on_lines"]
    docks_idle = e["dockyards"] - e["dockyards_on_lines"]
    intent = ctx["supreme"].memory[-1:] if ctx.get("supreme") else []
    lines = ["DATE: %s" % ctx["date"],
             "AT WAR WITH: %s" % (", ".join(sorted(ctx["enemies"])) or "nobody")]
    if intent:
        lines.append("LE CHONK'S INTENT (his latest note): %s" % intent[0].split(": ", 1)[-1][:600])
    lines.append("FACTORIES: %d civilian, %d military (%d on production lines%s), %d dockyards "
                 "(%d on lines%s)" % (
                     e["civilian_factories"], e["military_factories"], e["military_on_lines"],
                     ", %d IDLE" % idle if idle > 0 else "", e["dockyards"],
                     e["dockyards_on_lines"], ", %d IDLE" % docks_idle if docks_idle > 0 else ""))
    try:
        from . import intel
        st = gw.run(intel.status)
        if st.get("exists") and (not st.get("built") or st.get("upgrading")):
            n = 5 if not st.get("built") else intel.upgrade_info().get(st["upgrading"], (5,))[0]
            lines.append("THE INTELLIGENCE AGENCY TAKES %d CIVILIAN FACTORIES NOW (%s): they "
                         "build nothing until it is done" % (
                             n, "being founded" if not st.get("built")
                             else "upgrade " + st["upgrading"]))
    except Exception:
        pass
    lines.append("PRODUCTION LINES (index: equipment \"design\", factories working (asked, "
                 "when the game could not give them all: it fills the lines top down), output "
                 "per day): " + "; ".join(
                     "%d: %s, %s, %.1f%s" % (i, eq, f, out, " [dockyards]" if naval else "")
                     for i, eq, f, out, naval in e["lines"]))
    lines.append("OUR DESIGNS (id: equipment \"design\"; new_line opens a line for one): "
                 + "; ".join("%d: %s%s" % (i, eq, (' "%s"' % d) if d else "")
                             for i, eq, d in e["variants"]))
    lines.append("STOCKPILE: " + ", ".join("%s %d" % (k, v) for k, v in e["stockpile"][:25]))
    from . import army_needs
    lines += gw.run(army_needs.picture, e["lines"], e["stockpile"])
    tl = gw.run(trains_line)
    if tl:
        lines.append(tl)
    try:
        from .army_needs import air_unmade
        au = gw.run(air_unmade)
        if au:
            lines.append("AIRCRAFT WE FLY THAT NO LINE BUILDS (losses and worn planes cannot be "
                         "replaced; the game's 'Missing Equipment Production'): " + "; ".join(
                             "%s (%s, %d wing%s)" % (what, f, n, "s" if n > 1 else "")
                             for f, what, n in au) + ". A design and a line for those the war "
                         "needs; the air staff orders aircraft through you")
    except Exception:
        pass
    occ = sorted(sid for sid, (o, c) in ctx.get("holders", {}).items()
                 if c == ctx["own"] and o not in (ctx["own"],) and o in ctx["enemies"])
    if occ:
        lines.append("OCCUPIED: %d enemy states held; their garrisons draw infantry equipment, "
                     "support equipment and artillery from the stockpile (short of it, the "
                     "occupation laws lose their effect and resistance grows): keep a reserve "
                     "of those beyond the army's needs" % len(occ))
    try:
        from . import mio
        lines += gw.run(mio.industry_lines)
    except Exception as e:
        lines.append("(military industrial organisations could not be read: %s)" % type(e).__name__)
    lines += gw.run(economy.design_hints)
    short = gw.run(economy.shortages, ctx["enemies"], ctx["own"])
    lines.append("RESOURCES (per day: at hand incl. imports, of which imported, used by "
                 "production, balance): " + "; ".join(
                     "%s %g/%g/%g/%+g" % (k, av, imp, used, bal)
                     for k, (av, imp, used, bal) in e["resources"].items()))
    lines += gw.run(economy.oil_line, ctx["enemies"], ctx["own"])
    lines += gw.run(economy.energy_line)
    dl = gw.run(lambda g: B_module("industry").deals(g))
    if dl:
        lines.append("OUR IMPORT DEALS (seller resource amount a day, share arriving): " + "; ".join(
            "%s %s %g, %d%%%s" % (s, r, a, sh * 100, " CUT OFF" if sh < 0.5 else "")
            for s, r, a, sh in dl))
    try:
        if economy.MARKET_READY:
            lines += gw.run(economy.market_lines, ctx["own"], ctx["enemies"])
    except Exception as e:
        lines.append("(the arms market could not be read: %s)" % type(e).__name__)
    for res, bal, who in short:
        lines.append("SHORTAGE of %s (%+g a day: the lines using it slow down). Sellers with "
                     "a surplus: %s" % (res, bal, ", ".join("%s %d" % x for x in who) or "none"))
    try:
        lines.append(gw.run(economy.repair_line))
    except Exception as e:
        lines.append("(repair factories unreadable: %s)" % type(e).__name__)
    stock, cap, net, left = gw.run(economy.fuel_report)
    lines.append(gw.run(economy.fuel_line) + (
        " (FULL: what more comes in is lost; fuel silos add storage)" if cap and stock >= 0.95 * cap
        else ""))
    tl = gw.run(economy.fuel_target_line)
    if tl:
        lines.append(tl)
    try:
        law = gw.run(lambda g: B_module("cabinet").laws(g)["trade"]["now"])
        lines += gw.run(economy.trade_law_lines, law, bool(ctx["enemies"]))
    except Exception:
        pass
    try:
        civ, nq = gw.run(lambda g: (g.player().civilian_factories, len(g.construction_queue())))
        if civ > 15 * nq:
            lines.append("CIVILIAN FACTORIES IDLE: %d free for building, the %d project(s) in the "
                         "queue take at most %d (15 each); queue at least %d more projects"
                         % (civ, nq, 15 * nq, -(-(civ - 15 * nq) // 15)))
    except Exception:
        pass
    lines.append("CONSTRUCTION QUEUE (buildings queued, each level counted; how far the "
                 "started ones are): " + (", ".join(
        "%s x%d%s" % (b, n, " (%s)" % ", ".join("%.0f%%" % p for p in pcts) if pcts else "")
        for b, n, pcts in e["construction"])
                                           or "EMPTY: civilian factories build nothing"))
    if e["construction"]:
        lines.append("QUEUE ORDER (projects front to back; the civilian factories go to "
                     "the front ones first): " + gw.run(economy.queue_order))
    # with how far each one is (20.10, 1936: without it the minister refilled the busy
    # slots every turn, airframes one day, machine tools the next, and nothing finished)
    lines.append("RESEARCH SLOTS: " + "; ".join(
        "%d: %s" % (s, ("%s, %d%% done%s%s" % (
            t, pct, ", %d days left" % left if left is not None else "",
            " (ahead of time: speed x%.2f)" % sp if sp < 1 else ""))
                    if t else "EMPTY") for s, t, sp, pct, left in e["research"]))
    # shown with every slot busy too: a slow one may be replaced
    boost = economy.research_boosts()
    lines.append("AVAILABLE RESEARCH (xN: the speed left after the ahead-of-time penalty; "
                 "none shown = full speed; RESEARCH +N% = it speeds up all research for the "
                 "rest of the game, take it first): " + ", ".join(
                     t + (" x%.2f" % sp if sp < 1 else "")
                     + (" RESEARCH +%d%%" % round(100 * boost[t]) if t in boost else "")
                     for t, sp in sorted(e["available_research"],
                                         key=lambda x: (x[0] not in boost or x[1] < 0.75, -x[1]))))
    if e.get("branch_closed"):
        lines.append("BRANCHES CLOSED BY OUR CHOICE (mutually exclusive: never research these, "
                     "finishing one throws away the branch we hold): " + ", ".join(
                         "%s (we chose %s)" % (t, o) for t, o in sorted(e["branch_closed"].items())))
    lines.append("OUR STATES (for build): " + ", ".join(gw.run(economy.our_states, mv,
                                                               ctx["holders"])))
    try:
        from . import air
        bs = gw.run(air.bases, ctx["holders"], ctx["own"])
        lines.append("OUR AIR BASES (level, planes of our wings on it of its room; %d planes "
                     "a level): " % air.CAPACITY_PER_LEVEL + "; ".join(
                         "#%d level %d, %d of %d" % (b["state"], b["capacity"] //
                                                     air.CAPACITY_PER_LEVEL, b["planes"],
                                                     b["capacity"]) for b in bs))
    except Exception:
        pass
    from . import projects
    try:
        lines.append(gw.run(projects.facilities_line))
    except Exception:
        pass
    full = gw.run(economy.full_states)
    if full:
        lines.append("FULL (the game refused a factory there; the levels queued take the "
                     "slots, and a slot frees only if a project is cancelled): "
                     + ", ".join("#%d" % s for s in full))
    return "\n".join(lines)


# what always runs on the military factories (the user, 20.10: rifles, artillery and
# support equipment always; a major power also trucks, which its supply hangs on)
BASE_LINES = ("infantry_equipment", "artillery_equipment", "support_equipment",
              "motorized_equipment")


def base_idle(g):
    """The kinds of the base no factory works on now."""
    from .army_needs import kind
    working = {kind(x["equipment"]) for x in g.production_lines()
               if x["factories"] > 0 and "Naval" not in (x.get("class") or "")}
    return [k for k in BASE_LINES if k not in working]


def supply_picture(ctx):
    """Each theatre's supply for the army staff, and the trucks in stock."""
    A = _t()
    gw, out = ctx["gw"], []
    try:                               # the game's own record, not what we asked for
        live = gw.run(lambda g: g.hub_motorization())
        front.MOTORIZED.clear()
        front.MOTORIZED.update(live)
    except Exception:
        pass
    for tid, m in ctx["marshals"].items():
        try:
            fr = front.of(ctx, A.line_of(ctx, m))
            out += gw.run(lambda g: front.supply_lines(fr, front.group_zone(g, fr, tid),
                                                       m.theatre["name"]))
        except Exception:
            continue
    if out:
        out.append("TRUCKS IN STOCK: %s" % gw.run(lambda g: g.trucks_in_stock()))
    return out


def supply_alarm(ctx):
    """Reasons to wake the army staff: armies whose supply runs low."""
    A = _t()
    gw, why = ctx["gw"], []
    for tid, m in ctx["marshals"].items():
        try:
            fr = front.of(ctx, A.line_of(ctx, m))
            low = gw.run(lambda g: front.low_supply(fr, front.group_zone(g, fr, tid)))
        except Exception:
            continue
        if low:
            why.append("supply runs low in %s: %s" % (m.theatre["name"], ", ".join(
                "army %d %.0f%%" % (a, pct) for a, pct, _ in low)))
    return why


TRAIN_MARGIN = 1.2     # NDefines DEFAULT_SUPPLY_TRAIN_NEED_FACTOR: the AI keeps 1.2x its use


def trains(g):
    """(in stock, needed now, share met, factories on train lines) or None. The user,
    24.10: trains were not counted. The game's own need, from the supply network."""
    t = g.trains()
    if not t:
        return None
    stock = sum(x["count"] for x in g.equipment_stockpile(limit=200)["stockpile"]
                if (x["equipment"] or "").startswith("train_equipment"))
    lines = sum(x["factories"] for x in g.production_lines()
                if (x["equipment"] or "").startswith("train_equipment"))
    return stock, t["needed"], t["share"], lines


def trains_line(g):
    t = trains(g)
    if not t:
        return None
    stock, need, share, lines = t
    out = ("TRAINS: %d in the stockpile; the supply lines need %d now (the game's own count; "
           "it grows with the army, with every railway step from the capital to the front, "
           "and at war bombers kill trains), %s; %s" % (
               stock, need, "all of them met" if share >= 0.995
               else "ONLY %d%% MET: supply reaches the front short" % (share * 100),
               "%d factories on train lines" % lines if lines else "no line makes trains"))
    if stock < TRAIN_MARGIN * need or share < 0.995:
        out += (". SHORT: keep at least %d (%.1fx the need); a line of train_equipment, "
                "its factories taken from the least urgent line" % (
                    int(TRAIN_MARGIN * need + 0.999), TRAIN_MARGIN))
    return out


def industry_alarm(g):
    """Why the minister must act now, or None: an idle research slot, idle military
    factories, or an empty construction queue."""
    why = []
    from . import economy
    for s in g.research_slots():
        if s["researching"] is None:
            why.append("research slot %d is empty" % s["slot"])
        elif economy.ahead_speed(g, s["researching"]) < economy.SLOW:
            why.append("research slot %d runs %s at x%.2f, ahead of its time: replace it "
                       "(start_research with that slot)" % (
                           s["slot"], s["researching"], economy.ahead_speed(g, s["researching"])))
    for sl, t, o in economy.branch_conflicts(g):
        why.append("research slot %d researches %s, which EXCLUDES %s (mutually exclusive "
                   "branches): when it finishes, %s and everything after it are lost. Replace "
                   "it now (start_research on slot %d with replace=true)" % (sl, t, o, o, sl))
    c = g.player()
    on = sum(x["factories"] for x in g.production_lines() if "Naval" not in (x.get("class") or ""))
    if c.military_factories - on > 2:
        why.append("%d military factories are on no line" % (c.military_factories - on))
    docks = sum(x["factories"] for x in g.production_lines() if "Naval" in (x.get("class") or ""))
    if c.naval_dockyards - docks > 0:
        # any idle yard (the user, 20.10: dockyards never stand idle)
        why.append("%d dockyards build nothing: they build only ships, put a ship line "
                   "at the end of the list" % (c.naval_dockyards - docks))
    q = g.construction_queue()
    if not q:
        why.append("the construction queue is empty")
    else:
        spare = c.civilian_factories - 15 * len(q)
        if spare >= 10:
            # 26.10: 70 free civilian factories on 2 projects of at most 15 each, 40 idle,
            # and nothing woke him because the queue was not empty
            why.append("%d civilian factories build nothing: the queue holds %d project(s) of "
                       "at most 15 factories each; queue more" % (spare, len(q)))
    er = economy.energy_ratio(g)
    if er is not None and er < 0.95:
        # by tens, so a deepening shortage wakes him again but a steady one does not
        why.append("energy covers under %d%% of the factories' demand"
                   % (min(100, int(er * 10) * 10 + 10)))
    idle = base_idle(g)
    if idle:
        why.append("no factory makes %s: the base always runs" % ", ".join(idle))
    ind = B_module("industry")
    names = {v["id"]: v["design"] for v in ind.variants(g)}
    newer = {names[i]: n for i, n in ind.outdated(g).items() if names.get(i)}
    ds = ind.line_designs(g)
    # not a ship line: a hull on the slip is finished as laid down, the newer class goes on
    # a line of its own (20.10, 1936: Le Fantasque woke him every 3 days for a Mogador he
    # had no experience to design)
    old = sorted({"%s (newer: %s)" % (ds[i], newer[ds[i]]) for i, x in enumerate(g.production_lines())
                  if i < len(ds) and ds[i] in newer and x["factories"]
                  and "Naval" not in (x.get("class") or "")})
    if old:
        why.append("lines still build outdated designs: %s" % ", ".join(old))
    # an older generation of plain equipment (artillery_equipment_1 with _2 ours): the
    # designs check above sees named designs only (20.10, 1939: 1936 guns all summer)
    ours = {v["equipment"] for v in ind.variants(g) if not v["frame"]}   # not empty frames
    gen = set()
    for x in g.production_lines():
        m = re.match(r"(.+)_(\d+)$", x["equipment"] or "")
        if m and x["factories"] and "Naval" not in (x.get("class") or "") and any(
                re.fullmatch(re.escape(m.group(1)) + r"_(\d+)", e)
                and int(e.rsplit("_", 1)[1]) > int(m.group(2)) for e in ours):
            gen.add(x["equipment"])
    if gen:
        why.append("lines make an older generation than ours: %s (a new line for the "
                   "newer one, its factories moved over)" % ", ".join(sorted(gen)))
    t = trains(g)
    if t and (t[2] < 0.995 or (t[0] < TRAIN_MARGIN * t[1] and not t[3])):
        # by tens of the share, so a deepening shortage wakes him again
        why.append("trains are short: %d in the stockpile, the supply lines need %d%s, and "
                   "%s" % (t[0], t[1], ", %d%% met" % (int(t[2] * 10) * 10) if t[2] < 0.995
                           else "", "%d factories make them" % t[3] if t[3]
                           else "no line makes them"))
    try:
        from . import mio
        why += mio.alarm(g)
    except Exception:
        pass
    from .army_needs import unmade
    miss = unmade(g)
    if miss:
        why.append("the templates use equipment no line makes: %s" % "; ".join(miss))
    left = economy.fuel_report(g)[3]
    if left is not None and left < 90:
        why.append("fuel runs dry in about %d days" % left)
    why += economy.fuel_policy_alarm(g)
    why += economy.forts_waiting_alarm(g)
    try:
        why += economy.repair_alarm(g)
    except Exception:
        pass
    return "; ".join(why) or None


def politics(ctx):
    """The minister of state's picture: political power, focus, laws, advisors, decisions."""
    from . import politics as pol
    gw = ctx["gw"]
    e = gw.run(pol.picture)
    intent = ctx["supreme"].memory[-1:] if ctx.get("supreme") else []
    lines = ["DATE: %s" % ctx["date"],
             "AT WAR WITH: %s" % (", ".join(sorted(ctx["enemies"])) or "nobody")]
    if intent:
        lines.append("LE CHONK'S INTENT (his latest note): %s" % intent[0].split(": ", 1)[-1][:600])
    from .planner import course_line
    lines.append(course_line())
    lines.append("POLITICAL POWER: %d" % e["political_power"])
    try:
        lines += gw.run(pol.country_mechanics)
    except Exception as exc:
        lines.append("(stability and country mechanics could not be read: %s)" % type(exc).__name__)
    if e["focus"]:
        lines.append("NATIONAL FOCUS: %s (%s) running; %d focuses completed"
                     % (e["focus"], e["focus_key"], e["completed"] or 0))
    else:
        # what each gives, from the game's files (24.10: the focus that makes Mengjiang
        # a protectorate stood among the startable ones as a bare name)
        from .planner import _TREE, _effects
        lines.append("NATIONAL FOCUS: NONE RUNNING (%d completed). STARTABLE FOCUSES (key: "
                     "name [what it gives]): %s" % (e["completed"] or 0, "; ".join(
                         "%s: %s%s" % (f["key"], f["name"],
                                       " [%s]" % _effects(_TREE[f["key"]])[:120]
                                       if f["key"] in _TREE else "")
                         for f in e["startable"]) or "none"))
    try:
        from . import economy
        mp, n, low = gw.run(pol.manpower)
        er = gw.run(economy.energy_ratio)
        lines.append("MANPOWER: %dk available%s. ENERGY: %s" % (
            mp // 1000, (" for %d divisions (the next conscription law is due below %dk)"
                         % (n, low // 1000)) if low else "",
            "%d%% of the factories' demand covered" % (er * 100) if er is not None else "?"))
    except Exception:
        pass
    lines.append("LAWS: " + "; ".join(
        "%s now %s, may adopt %s" % (cat, v["now"] or "?", ", ".join(v["may_adopt"]) or "nothing")
        for cat, v in e["laws"].items()))
    lines += pol.law_lines(e["laws"])
    try:
        lines += gw.run(economy.trade_law_lines, (e["laws"].get("trade") or {}).get("now"),
                        bool(ctx["enemies"]))
    except Exception:
        pass
    by_slot = {}
    for r in e["hireable"]:
        by_slot.setdefault(r["slot"], []).append("%s (%s, %d PP)" % (r["character"], r["name"],
                                                                     r["cost"]))
    # 24.10: "ADVISORS YOU MAY HIRE" did not read as free slots; the minister wrote "no
    # free advisor slot" beside three of them
    # the game's check counts the price too: a slot left out is full OR beyond our power
    lines.append("EMPTY ADVISOR SLOTS WE CAN FILL NOW (the game takes a hire into each NOW; "
                 "who may fill it): " + ("; ".join("%s: %s" % (s, ", ".join(v))
                                                for s, v in sorted(by_slot.items()))
                                        or "none") +
                 ". A slot not listed is either filled or dearer than the power we hold "
                 "(political advisors cost about 150)")
    try:
        cc = gw.run(pol.concerns)
        lines.append("INDUSTRIAL CONCERNS: " + ("in force: %s" % ", ".join(cc["in_force"])
                     if cc["in_force"] else "THE SLOT IS EMPTY; may take: " + ("; ".join(
                         "%s (%d PP: %s)" % (o["key"], o["cost"], o["effect"] or "?")
                         for o in cc["options"]) or "none")))
    except Exception:
        pass
    lines += gw.run(pol.mission_lines)
    lines.append("DECISIONS YOU MAY TAKE: " + (", ".join(e["decisions"]) or "none"))
    targeted = e.get("targeted_decisions", [])
    lines.append("COUNTRY-TARGETED DECISIONS YOU MAY TAKE (key and target checked together; "
                 "take_decision requires target): " + ("; ".join(
                     "%s target=%s" % (d["key"], d["target"]) for d in targeted) or "none"))
    for key in dict.fromkeys(e["decisions"] + [d["key"] for d in targeted]):
        lines.append("DECISION %s: %s" % (key, pol.decision_description(key)))
    from . import exile
    try:
        lines += gw.run(exile.decision_lines)
    except Exception:
        pass
    from .politics import decision_terms
    from .spirits import country_tags
    own = ctx["own"]
    # the game's visible list holds other countries' targeted decisions too (POL_, FIN_)
    shut = [(k, decision_terms(k)) for k in e.get("visible_decisions", [])
            if k not in e["decisions"] and k not in exile.DECISIONS and (k.split("_", 1)[0] == own
                                             or k.split("_", 1)[0] not in country_tags())]
    shut = [(k, "nothing beyond its cost, so political power or its timing" if t.startswith("-")
             else t) for k, t in shut if t][:12]
    if shut:
        lines.append("DECISIONS NOT OPEN YET (what the game asks first, from its files): " +
                     "; ".join("%s: %s" % (k, t[:220]) for k, t in shut))
    lines.append("NATIONAL SPIRITS AND LAWS IN FORCE: " + ", ".join(e["ideas"]))
    if e["removers"]:
        from .events import loc
        lines.append("WHAT ENDS OUR SPIRITS (from the game's files; the quickest ways first):"
                     + "".join("\n- %s \"%s\": %s" % (k, loc(k) or k, "; ".join(v))
                               for k, v in e["removers"].items()))
    from . import intel
    lines += ctx["gw"].run(intel.lines)
    lines += ctx["gw"].run(intel.crypto_lines)
    try:
        from . import mio
        lines += ctx["gw"].run(mio.politics_lines)
    except Exception as e:
        lines.append("(organisation policies could not be read: %s)" % type(e).__name__)
    try:
        from . import faction
        lines += ctx["gw"].run(faction.picture)
        lines += ctx["gw"].run(faction.rules_picture)
    except Exception as e:
        lines.append("(faction rules could not be read: %s)" % type(e).__name__)
    try:
        from . import subjects
        lines += ctx["gw"].run(subjects.lines)
    except Exception as e:
        lines.append("(subjects could not be read: %s)" % type(e).__name__)
    return "\n".join(lines)


def B_game():
    from .bridge import module
    return module("game")


def army_staff(ctx):
    """The army staff's picture: manpower, divisions, templates, training, equipment."""
    from . import army
    gw = ctx["gw"]
    e = gw.run(army.picture)
    intent = ctx["supreme"].memory[-1:] if ctx.get("supreme") else []
    lines = ["DATE: %s" % ctx["date"],
             "AT WAR WITH: %s" % (", ".join(sorted(ctx["enemies"])) or "nobody")]
    lines += war_threat_lines(ctx)
    if intent:
        lines.append("LE CHONK'S INTENT (his latest note): %s" % intent[0].split(": ", 1)[-1][:600])
    lines.append("MANPOWER AVAILABLE: %d" % e["manpower"])
    lines.append("OUR DIVISIONS BY TEMPLATE: " + ", ".join("%s %d" % x for x in e["divisions"]))
    lines.append("OUR TEMPLATES (battalion columns | ... ; support companies): " + "; ".join(
        "%s: %s" % kv for kv in gw.run(army.template_grids).items()))
    lines.extend(gw.run(army.below_standard, e))
    lines.append("BATTALION AND SUPPORT KEYS (for edit_template; locked ones are refused): "
                 + "; ".join(gw.run(army.battalion_types)))
    lines.append("IN TRAINING: " + ("; ".join(
        "%s %d (trained %.0f%%, worst equipped %.0f%%)" % (k, n, tr * 100, eq * 100)
        for k, (n, tr, eq) in e["training"].items()) or "NOTHING"))
    training = gw.run(army.training_details)
    lines.append("TRAINING CONVEYORS (id | template | parallel lines | series | active | "
                 "completed | remaining divisions): " + ("; ".join(
        "%d | %s | %d | %s | %d | %d | %s" % (
            c["id"], c["template"], c["parallel"],
            "0 INFINITE" if c["repeat"] else str(c["series"]),
            sum(x["active"] for x in c["lines"]),
            sum(x["completed"] for x in c["lines"]),
            "unbounded" if c["remaining_divisions"] is None else c["remaining_divisions"])
        for c in training["conveyors"]) or "none"))
    lines.append("TEMPLATE EQUIPMENT COSTS (one full division; fielded/in training): " +
                 "; ".join("%s (%d/%d): %s" % (
                     t["template"], t["fielded"], t["in_training"],
                     ", ".join("%s %g" % kv for kv in t["equipment_per_division"].items()))
                     for t in training["templates"]))
    lines.append("CURRENT TRAINING BATCH FULL EQUIPMENT: " +
                 ", ".join("%s %g" % kv for kv in training["queue_full_equipment"].items()) +
                 ". " + training["equipment_note"])
    try:
        def lf(g):
            L = ops.landing_divisions(g)
            by, tm = {}, collections.Counter()
            for aid, og in ops.armies(g).items():
                n = sum(d in L for d in ops.division_bases(g, og))
                if n:
                    by[aid] = (n, len(ops.division_bases(g, og)), bool(ops._fronts(g, og)))
            for d in L:
                t = g.p.u64(d + 0x3B8)
                tm[B_game().item_name(g.p, t + 0x20)] += 1
            return len(L), by, dict(tm)
        n, by, tm = gw.run(lf)
        if n:
            mixed = [a for a, (k, tot, _) in by.items() if k < tot]
            lines.append("LANDING FORCE (marine divisions: %s): %d, in armies %s. They land "
                         "behind the enemy and stay off the line: keep them together in one "
                         "army of their own with a general (create_army with composition and "
                         "take_from_armies)%s; Le Chonk sends that army to the theatre of the "
                         "war plan's next landing." % (
                             ", ".join("%s %d" % kv for kv in tm.items()), n,
                             "; ".join("%d: %d of its %d%s" % (a, k, tot, ", on a sector" if f else "")
                                       for a, (k, tot, f) in by.items()),
                             " (now mixed into armies %s)" % ", ".join(map(str, mixed)) if mixed else ""))
    except Exception:
        pass
    try:
        sf = gw.run(army.special_forces)
        lines.append("SPECIAL FORCES (marines, mountaineers, paratroopers...): %d battalions in "
                     "divisions, %d in training, of about %d allowed (5%% of the other "
                     "battalions, at least 24; a doctrine or focus may allow more); "
                     "by kind: %s; templates with them: %s" % (
                     sf["have"], sf["training"], sf["cap"],
                     ", ".join("%s %d (+%d in training)" % (k, h, t) for k, (h, t)
                               in sorted(sf["by_kind"].items())) or "none",
                     ", ".join("%s (%d)" % kv for kv in sf["templates"].items()) or "none"))
        old = gw.run(army.outdated_templates, dict(e["production"]))
        if old:
            lines.append("TEMPLATES ON OLD EQUIPMENT (our lines make newer): " + "; ".join(
                "%s has %s (%s, no longer made) where %s (%s) is made" % x for x in old) +
                ". A new template (edit_template with new_name) for the divisions to come "
                "keeps the old ones whole; changing the template itself re-equips all its "
                "divisions, which stand short until the new equipment comes.")
        # 23.10, the user: no paratroopers without transport planes being made
        tp_stock = sum(n for k, n in e["stockpile"] if "transport_plane" in k)
        tp_made = sum(v for k, v in e["production"] if "transport_plane" in k)
        lines.append("TRANSPORT PLANES (what paratroopers fly in): %d in stock, %s" % (
            tp_stock, "%.1f made a day" % tp_made if tp_made else
            "NO production line: paratroopers cannot drop, do not raise them until the "
            "industry makes transport planes"))
    except Exception:
        pass
    lines.append("STOCKPILE: " + ", ".join("%s %d" % x for x in e["stockpile"]))
    lines.append("PRODUCED A DAY: " + ", ".join("%s %.1f" % x for x in e["production"]))
    # the organisation of the army: groups, armies, commanders, tactical air (18.10)
    sit = _situation(ctx)
    al = []
    free = gw.run(lambda g: len(ops.unassigned(g)))
    if free:
        al.append("%d divisions are in no army: nobody commands them" % free)
        if not ctx["marshals"]:
            # 20.10, a fresh 1936: eleven turns "blocked: no theatre" (create_army needed one)
            al.append("WE HAVE NO THEATRE YET: create_army without a theatre forms an army in "
                      "none; Le Chonk opens the theatres with the armies you form")
    # idle wings at war are the air staff's alarm now (air.strategic_alarm, 24.10)
    try:
        dr = gw.run(ops.drill_picture)
        lines.append("EXERCISES (army: divisions, below Regular, mean experience level "
                     "0-1 where Regular is 0.30, drilling): " + "; ".join(
                         "a%d %d div, %d below Regular, %.2f, %s" % (
                             aid, n, low, mean, "DRILLING" if on else "no")
                         for aid, (n, low, mean, on) in sorted(dr.items())))
    except Exception:
        pass
    sl = supply_picture(ctx)
    if sl:
        lines.append("SUPPLY (yours: motorize_hub; full motorization takes %d trucks a hub):"
                     % front.HUB_TRUCKS)
        lines += ["- " + x for x in sl]
    if al:
        lines.append("ALERTS:\n- " + "\n- ".join(al))
    oob = gw.run(ops.order_of_battle)
    _groups(ctx, sit, oob, lines)
    _commanders(ctx, oob, lines)
    from . import doctrine_files
    lines += gw.run(doctrine_files.picture, "land")
    lines += gw.run(doctrine_files.picture, "special_forces")
    from . import exile
    try:
        lines += gw.run(exile.lines)
    except Exception:
        pass
    from . import projects
    try:
        ctx["projects_stuck"] = gw.run(projects.stuck, "specialization_land")
        if ctx["projects_stuck"]:
            lines.insert(1, "ALERT, SPECIAL PROJECTS STAND STILL: " + ctx["projects_stuck"])
        lines += gw.run(projects.lines, "specialization_land")
    except Exception as e:
        lines.append("(special projects unavailable: %s)" % e)
    return "\n".join(lines)


TACTICAL_AIR = ("fighter", "close air support", "tactical bomber", "transport")
STRATEGIC_AIR = ("fighter", "strategic bomber", "naval bomber")


def _aircraft(g):
    """The air force's production: designs, lines, planes in stock (19.10: France had
    made no aircraft for 15 months; nobody owned the air force's production)."""
    ind = B_module("industry")
    plane = lambda eq: "plane" in eq or "airframe" in eq
    old = ind.outdated(g)
    designs = [(v["id"], v["equipment"], (v["design"] + (", OUTDATED: %s is newer" % old[v["id"]]
                                                          if v["id"] in old else ""))
                if v["design"] else ("EMPTY FRAME, no weapons: make a design on it"
                                     if v["frame"] else ""))
               for v in ind.variants(g) if plane(v["equipment"])]
    names = ind.line_designs(g)
    prod = [(x["equipment"], names[i] if i < len(names) else "", x["factories"],
             x["output_per_day"]) for i, x in enumerate(g.production_lines())
            if plane(x["equipment"])]
    stock = {}
    for x in g.equipment_stockpile(limit=200)["stockpile"]:
        if x["equipment"] and plane(x["equipment"]):
            stock[x["equipment"]] = stock.get(x["equipment"], 0) + x["count"]
    return ["OUR AIRCRAFT DESIGNS (id: airframe \"design\"): " + "; ".join(
                "%d: %s%s" % (i, eq, (' "%s"' % d) if d else "") for i, eq, d in designs),
            "AIRCRAFT IN PRODUCTION (airframe \"design\", factories, planes a day): " + (
                "; ".join('%s "%s", %d, %.2f' % x for x in prod) or "NONE"),
            "AIRCRAFT IN STOCK (not yet in wings): " + (
                ", ".join("%s %d" % kv for kv in stock.items()) or "none")]


def air_staff(ctx):
    """The air staff's picture: the air force, the armies' battles and the regions over
    the fronts, our home regions and the enemy's industry regions."""
    gw, mv = ctx["gw"], ctx["mv"]
    intent = ctx["supreme"].memory[-1:] if ctx.get("supreme") else []
    lines = ["DATE: %s" % ctx["date"],
             "AT WAR WITH: %s" % (", ".join(sorted(ctx["enemies"])) or "nobody")]
    lines += war_threat_lines(ctx)
    if intent:
        lines.append("LE CHONK'S INTENT (his latest note): %s" % intent[0].split(": ", 1)[-1][:600])
    lw = gw.run(air.summary, air_regions(mv))
    _air(ctx, lw, lines)
    lines += gw.run(air.reserve_lines)
    lines += gw.run(air.transfer_lines)
    deploy_bases = gw.run(air.bases, ctx["holders"], ctx["own"])
    lines.append("DEPLOYMENT AIR BASES (state_id, capacity, aircraft of all sides based "
                 "here, free spaces): " + ("; ".join(
                     "%s: capacity %d, aircraft %d, free %d" %
                     (mv.label(b["state"]), b["capacity"], b["planes"],
                      max(0, b["capacity"] - b["planes"])) for b in deploy_bases) or "none"))
    lines += _battles(ctx)
    regions = air_regions(mv)
    if ctx["enemies"]:
        try:
            home_st = [s for s, (o, c) in ctx["holders"].items() if c == ctx["own"]]
            lines += gw.run(air.sky_lines, regions, sorted(ctx["enemies"]),
                            set(regions.over(home_st)))
        except Exception as e:
            lines.append("(the sky by region unreadable: %s)" % type(e).__name__)
    owners = ctx.get("air_owner", {})
    ws = gw.run(air.wings)
    lines.append("WINGS (id kind planes, base, what it flies; at a carrier: the "
                 "admiralty's): " + "; ".join(
                     "%d %s %d, at %s, %s%s" % (w["id"], w["kind"], w["planes"],
                                         mv.name(w["base"]) if w["base"] else "a carrier",
                                         ("attached to army %s" % w["army"]) if w["army"] else
                                         ("%s over %s" % (",".join(w["missions"]),
                                                          regions.label(w["region"])))
                                         if w["missions"] and w["region"] else "IDLE",
                                         (" [%s]" % owners[w["id"]]) if w["id"] in owners else "")
                     for w in ws))
    lines += gw.run(_aircraft)
    from . import doctrine_files
    lines += gw.run(doctrine_files.picture, "air")
    from . import economy
    lines += gw.run(economy.design_hints, lambda eq: "plane" in eq or "airframe" in eq)
    from . import projects
    try:
        if ctx["enemies"]:
            lines.append(gw.run(economy.plane_offers_line, ctx["own"], ctx["enemies"]))
        ctx["projects_stuck"] = gw.run(projects.stuck, "specialization_air")
        if ctx["projects_stuck"]:
            lines.insert(1, "ALERT, SPECIAL PROJECTS STAND STILL: " + ctx["projects_stuck"])
        lines += gw.run(projects.lines, "specialization_air")
    except Exception as e:
        lines.append("(special projects unavailable: %s)" % e)
    h = ctx["holders"]
    own = [s for s, (o, c) in h.items() if o == ctx["own"] and c == ctx["own"]]
    big = sorted(own, key=lambda s: -sum(v for _, v in mv.cities.get(s, [])))[:12]
    lines.append("OUR HEARTLAND (regions over our biggest cities, where enemy bombers "
                 "strike): " + "; ".join(regions.label(r) for r in regions.over(big)[:6]))
    for t in sorted(ctx["enemies"]):
        theirs = [s for s, (o, c) in h.items() if o == t and c == t]
        big = sorted(theirs, key=lambda s: -sum(v for _, v in mv.cities.get(s, [])))[:10]
        if big:
            lines.append("%s's HEARTLAND (regions over its biggest cities and industry): %s"
                         % (t, "; ".join(regions.label(r) for r in regions.over(big)[:5])))
    return "\n".join(lines)


def _battles(ctx):
    """THE ARMIES' BATTLES for the air staff: per theatre, which armies attack and where,
    so fighters and close air support go over them."""
    gw, mv = ctx["gw"], ctx["mv"]
    A = _t()
    out = []
    for tid, m in ctx["marshals"].items():
        th = m.theatre
        try:
            v = gw.run(ops.command_view, mv, ctx["holders"], tid,
                       A.facing(ctx, m) or {th["enemy"]})
        except Exception:
            continue
        rows = ["army %d %s (%s)" % (a["army"], "ATTACKING" if a["attacks"] else "holding",
                                    ", ".join(x.split(" (#")[0] for x in a["sector"][:4])
                                    or "reserve")
                for a in v["armies"]]
        out.append("- %r vs %s, %s: %s" % (th["name"], th["enemy"] or "-",
                                          "ATTACK UNDER WAY" if th.get("executing")
                                          and th["enemy"] in ctx["enemies"] else "holds",
                                          "; ".join(rows) or "no armies"))
    if out:
        out.insert(0, "THE ARMIES' BATTLES (theatre, its armies and their sectors; give the "
                      "attacking ones fighters and close air support, attach_air_to_army):")
    return out


def operations_lines(ctx, sea=None):
    """Our planned landings and paradrops, and the ports they can sail from."""
    gw, mv = ctx["gw"], ctx["mv"]
    ops_ = gw.run(ops.operations)
    out = []
    if ops_:
        out.append("OPERATIONS (army, operation, from, to, divisions, state): " + "; ".join(
            "army %d op %d %s from %s to %s, %d div, %s" % (
                o["army"], o["operation"], o["kind"], mv.label(mv.state_of.get(o["source"])),
                ", ".join("p%d (%s)" % (t, mv.label(mv.state_of.get(t))) for t in o["targets"]),
                o["divisions"], "LAUNCHED" if o["launched"] else "planned")
            for o in ops_))
        if sea is not None:
            regs = collections.Counter()
            for o in ops_:
                if o["kind"] == "naval invasion":
                    for q in [o["source"]] + o["targets"]:
                        for b in mv.adj.get(q, ()):
                            if b in sea.region_of and b not in mv.land:
                                regs[sea.region_of[b]] += 1
            if regs:
                out.append("THE LANDINGS SAIL THROUGH (they need our naval dominance there: "
                           "patrol and strike force, naval_invasion_support): "
                           + "; ".join(sea.label(r) for r in regs))
    own, holders = ctx["own"], ctx["holders"]
    ports = sorted(((q, lvl) for q, lvl in mv.ports.items()
                    if holders.get(mv.state_of.get(q), (None, None))[1] == own),
                   key=lambda x: -x[1])
    if sea is None and ports:
        out.append("OUR PORTS (a landing sails from one; province, state, level): " + "; ".join(
            "p%d %s %d" % (q, mv.label(mv.state_of.get(q)), lvl) for q, lvl in ports[:12]))
    return out
