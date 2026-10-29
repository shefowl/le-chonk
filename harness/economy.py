"""The economy as the industry strategist sees it and orders it: factories, production
lines, stockpile, construction, research. Reads and the game's own player commands
(bridge/calling/game.py, verified layouts); no judgement here.

New production lines, trade and training go through bridge/calling/industry.py: each
command is checked by the game's own CanExecute before it is posted.
"""
import collections
import re
import struct

from . import bridge as B
from .ops import OpError as B_err

BUILDINGS = ("arms_factory", "industrial_complex", "dockyard", "infrastructure",
             "air_base", "anti_air_building", "synthetic_refinery", "fuel_silo",
             "radar_station", "bunker", "coastal_bunker", "naval_base",
             "land_facility", "air_facility", "naval_facility", "nuclear_facility")
FACILITIES = ("land_facility", "air_facility", "naval_facility", "nuclear_facility")


def picture(g, mv):
    """A compact dict of the economy."""
    c = g.player()
    lines = g.production_lines()
    on_lines = sum(x["factories"] for x in lines if "Naval" not in (x.get("class") or ""))
    on_docks = sum(x["factories"] for x in lines if "Naval" in (x.get("class") or ""))
    stock = collections.Counter()
    for s in g.equipment_stockpile(limit=200)["stockpile"]:
        if s["equipment"]:
            stock[s["equipment"]] += s["count"]
    cq = g.construction_queue()
    queue = collections.Counter()
    for x in cq:                       # an entry holds several levels (20.10)
        queue[x["building"] or "?"] += max(1, x.get("levels") or 1)
    # how far each project is, in queue order (20.10: the minister saw "arms_factory x11"
    # for seven months and never knew none had come close to done)
    started = collections.defaultdict(list)
    for x in cq:
        if x.get("percent"):
            started[x["building"] or "?"].append(x["percent"])
    slots = g.research_slots()
    ind = B.module("industry")
    old = ind.outdated(g)
    variants = [(v["id"], v["equipment"],
                 (v["design"] + (", OUTDATED: %s is newer" % old[v["id"]] if v["id"] in old else ""))
                 if v["design"] else ("EMPTY FRAME, no modules: make a design on it"
                                      if v["frame"] else ""))
                # ships: the finished classes only, not the empty hulls (20.10: with them
                # all hidden the minister tried new designs on 0 navy experience, and put
                # 11 dockyards on convoys with Mogador and Minerve ready to build)
                for v in ind.variants(g)
                if not v["equipment"].startswith("ship_hull") or v["design"]]
    designs = ind.line_designs(g)
    names = {v["id"]: v["design"] for v in ind.variants(g)}
    newer = {names[i]: n for i, n in ind.outdated(g).items() if names.get(i)}
    balance = ind.resource_balance(g)
    # a line on an older generation of equipment we have a newer one of (20.10: rifles of
    # 1936 were made all winter of 1939 with infantry_equipment_2 researched)
    import re as _re
    ours = {v["equipment"] for v in ind.variants(g) if not v["frame"]}   # not empty frames
    gen = {}
    for x in lines:
        m = _re.match(r"(.+)_(\d+)$", x["equipment"] or "")
        if m:
            later = [e for e in ours if _re.fullmatch(_re.escape(m.group(1)) + r"_(\d+)", e)
                     and int(e.rsplit("_", 1)[1]) > int(m.group(2))]
            if later:
                gen[x["equipment"]] = max(later, key=lambda e: int(e.rsplit("_", 1)[1]))
    return {
        "military_factories": c.military_factories, "civilian_factories": c.civilian_factories,
        "dockyards": c.naval_dockyards, "political_power": c.political_power,
        "military_on_lines": on_lines, "dockyards_on_lines": on_docks,
        "lines": [(x["index"], x["equipment"] + (' "%s"' % designs[i] if i < len(designs)
                                                 and designs[i] else "")
                   + (" OUTDATED, %s is newer" % newer[designs[i]]
                      if i < len(designs) and designs[i] in newer and x["factories"] else "")
                   + (" OUTDATED, %s is ours" % gen[x["equipment"]]
                      if x["equipment"] in gen and x.get("requested", x["factories"]) else "")
                   + ((" (endless series)" if x["ships"] is None else
                       " (%d ship(s) left, then the line closes)" % x["ships"])
                      if "ships" in x else ""),
                   x["factories"] if x.get("requested", x["factories"]) == x["factories"]
                   else "%d (asked %d)" % (x["factories"], x["requested"]),
                   round(x["output_per_day"], 1),
                   "Naval" in (x.get("class") or "")) for i, x in enumerate(lines)],
        "stockpile": stock.most_common(),
        "construction": [(b, n, started.get(b, [])) for b, n in queue.most_common()],
        "research": [(s["slot"], s["researching"],
                      ahead_speed(g, s["researching"]) if s["researching"] else None,
                      s.get("percent", 0.0), s.get("days_remaining"))
                     for s in slots],
        "available_research": [(t, ahead_speed(g, t)) for t in g.available_research()
                               if t not in branch_closed(g)],
        "branch_closed": branch_closed(g),
        "variants": variants,
        "resources": balance,
        "free_slots": sum(1 for s in slots if s["researching"] is None),
    }


_TECH_YEAR = None
AHEAD_FACTOR = 2        # NDefines.NTechnology.BASE_YEAR_AHEAD_PENALTY_FACTOR
SLOW = 0.5              # below this speed a technology is not taken (the user, 19.10)


def _tech_years():
    """{tech: start_year} from common/technologies."""
    global _TECH_YEAR
    if _TECH_YEAR is None:
        import glob, os, re
        from .spirits import _block_end
        _TECH_YEAR = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "technologies", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\t([\w.\-]+)\s*=\s*\{", txt):
                body = txt[m.end() - 1:_block_end(txt, m.end() - 1)]
                y = re.search(r"\bstart_year\s*=\s*(\d{4})", body)
                if y:
                    _TECH_YEAR[m.group(1)] = int(y.group(1))
    return _TECH_YEAR


def ahead_speed(g, tech):
    """The research speed a technology keeps after its ahead-of-time penalty (1.0 = none):
    1 / (1 + 2 x years ahead), as the game counts it (checked 1937: basic_medium_tank_chassis
    of 1938, started 1.01.1936, ran at 0.21 with our +5% bonus)."""
    y = _tech_years().get(tech)
    if not y:
        return 1.0
    yy, mm, dd = (int(x) for x in g.date()["text"].split(".")[:3])
    now = yy + ((mm - 1) * 30.4 + dd) / 365.0
    return round(1.0 / (1 + AHEAD_FACTOR * max(0.0, y - now)), 2)


CO_FUEL = 0x1520            # CCountryFuelStatus*
FS_STOCK, FS_CAPACITY = 0x08, 0x10
_FUEL_LOG = []              # (day, stock), one a day, for the real daily balance


def fuel(g):
    """(stock, capacity) of the country's fuel."""
    p = g.p
    fs = p.u64(g.player().ptr + CO_FUEL)
    return p.i64(fs + FS_STOCK) / 1e5, p.i64(fs + FS_CAPACITY) / 1e5


def track_fuel(g, day):
    """One sample a day (run.py). The game's own gain field does not read as a daily
    figure (2.0 while the stock grew 15.7 a day), so the balance is measured."""
    if _FUEL_LOG and _FUEL_LOG[-1][0] == day:
        return
    if _FUEL_LOG and day < _FUEL_LOG[-1][0]:
        # A loaded save starts a different timeline; its stocks are not comparable.
        _FUEL_LOG.clear()
    _FUEL_LOG.append((day, fuel(g)[0]))
    del _FUEL_LOG[:-90]


FUEL_PER_OIL = 48     # BASE_FUEL_GAIN_PER_OIL 2 an hour per oil left over after production


def oil_line(g, enemies, own_tag):
    """When fuel falls: how much oil a day would stop it, and who sells oil."""
    stock, _, net, _ = fuel_report(g)
    t = fuel_target(g)
    if t and stock < 0.9 * t[0] and (net is None or net < (t[0] - stock) / 120):
        who = B.module("industry").sellers(g, "oil", exclude=set(enemies) | {own_tag})
        return ["FUEL UNDER THE PLAN'S RESERVE: %d of %d, %s. About %d oil a day stops the "
                "fall and refills it in two months (oil left over after production becomes "
                "fuel, %d a day each); our factories pay for it. Oil sellers: %s" % (
                    stock, t[0], "falling %d a day" % -net if net and net < 0 else "not refilling",
                    oil_to_refill(stock, t[0], net), FUEL_PER_OIL,
                    ", ".join("%s %g" % x for x in who) or "none")]
    if net is None or net >= 0:
        return []
    who = B.module("industry").sellers(g, "oil", exclude=set(enemies) | {own_tag})
    return ["FUEL FALLS %d a day: oil left over after production becomes fuel, %d a day "
            "for each oil a day, so about %d oil a day stops the fall (buy oil; more builds "
            "the reserve). Oil sellers: %s" % (-net, FUEL_PER_OIL, -(-(-net) // FUEL_PER_OIL),
            ", ".join("%s %g" % x for x in who) or "none")]


def fuel_report(g):
    """(stock, capacity, net per day over the last weeks or None, days left or None)."""
    stock, cap = fuel(g)
    net = None
    if len(_FUEL_LOG) >= 2:
        # Older campaign files can contain both sides of a rewind. Use only the
        # latest increasing run, even when pre-rewind dates fit the 30-day window.
        start = 0
        for i in range(len(_FUEL_LOG) - 1, 0, -1):
            if _FUEL_LOG[i][0] <= _FUEL_LOG[i - 1][0]:
                start = i
                break
        recent = [x for x in _FUEL_LOG[start:] if x[0] >= _FUEL_LOG[-1][0] - 30]
        (d0, s0), (d1, s1) = recent[0], recent[-1]
        if d1 > d0:
            net = (s1 - s0) / (d1 - d0)
    left = stock / -net if net is not None and net < 0 else None
    return stock, cap, net, left


# THE PLAN's fuel policy (run.py sets it each day): war_days, war_burn, source, at_war
FUEL_POLICY = {}


def fuel_target(g):
    """(target stock, burn a day, measured) from THE PLAN's fuel policy, or None. At war
    the measured deficit replaces the planner's estimate of the burn."""
    pol = FUEL_POLICY
    days = pol.get("war_days")
    if not days:
        return None
    net = fuel_report(g)[2]
    measured = bool(pol.get("at_war") and net is not None and net < 0)
    burn = -net if measured else pol.get("war_burn")
    if not burn:
        return None
    return days * burn, burn, measured


def fuel_target_line(g):
    t = fuel_target(g)
    if not t:
        return None
    target, burn, measured = t
    stock = fuel(g)[0]
    return ("FUEL RESERVE OF THE PLAN: %d days of war at %.0f a day (%s) = %d; we hold %d "
            "(%d days); source: %s" % (
                FUEL_POLICY["war_days"], burn, "measured at war" if measured else
                "the planner's estimate", target, stock, stock / burn,
                FUEL_POLICY.get("source") or "not set"))


def fuel_policy_alarm(g):
    """Why the fuel reserve falls short of THE PLAN, or []."""
    t = fuel_target(g)
    if not t:
        return []
    target, burn, measured = t
    stock, cap, net, left = fuel_report(g)
    src = FUEL_POLICY.get("source") or "trade or refineries"
    why = []
    if cap and target > cap * 1.02:
        why.append("the plan's fuel reserve (%d) is more than our storage (%d): fuel silos "
                   "hold the rest" % (target, cap))
    if FUEL_POLICY.get("at_war"):
        if left is not None and left < FUEL_POLICY["war_days"]:
            why.append("fuel lasts about %d days, the plan wants %d: more by %s"
                       % (left, FUEL_POLICY["war_days"], src))
        # 27.10, the user: the fleets sailed, the stock fell to 26.8k against a reserve of
        # 68k, and nobody told the industry to buy oil; at war only "runs dry" woke it
        elif net is not None and net < 0 and stock + 60 * net < target:
            why.append("fuel falls %d a day (fleets and planes burn it): under the plan's "
                       "reserve of %d within %d days; buy oil, about %d a day"
                       % (-net, target, max(0, (stock - target) / -net), oil_to_refill(stock, target, net)))
        elif stock < 0.9 * target and (net is None or net < (target - stock) / 120):
            why.append("fuel %d is under the plan's reserve of %d and not refilling: buy oil, "
                       "about %d a day refills it in two months"
                       % (stock, target, oil_to_refill(stock, target, net)))
    elif stock < 0.95 * min(target, cap or target) and (net is None or net <= 0.5):
        why.append("fuel reserve %d is below the plan's %d and not growing: more by %s"
                   % (stock, target, src))
    return why


def oil_to_refill(stock, target, net, days=60):
    """Oil a day that stops the fall and refills the reserve in `days`."""
    need = max(0.0, -(net or 0)) + max(0.0, target - stock) / days
    return int(-(-need // FUEL_PER_OIL))


def fuel_line(g):
    stock, cap, net, left = fuel_report(g)
    spare = B.module("industry").resource_balance(g)["oil"][3]
    gain = FUEL_PER_OIL * (max(0.0, spare) + 1)
    burn = gain - net if net is not None else None
    return "FUEL: %d of %d storage (%d%%)%s%s%s" % (
        stock, cap, 100 * stock / cap if cap else 0,
        (", %+.0f a day over the last weeks" % net) if net is not None else "",
        (", runs dry in about %d days" % left) if left is not None else "",
        ". Oil left over after production: %g a day, about %d fuel a day; so about %d a "
        "day burns (planes, ships, tanks, lorries). One civilian factory buys 8 oil a day = "
        "about %d fuel a day" % (spare, gain, max(0, burn), 8 * FUEL_PER_OIL)
        if burn is not None else "")


# 27.10, the user: "if the fleet's operations burn fuel and we have plenty of factories,
# buy oil until the balance is positive". At war oil is bought, not saved: the cap is a
# share of the civilian factories, not the plan's reserve (the reserve of 60 days at the
# planner's 500 a day cut every deal of 120 to 16, while 4000 a day burned).
OIL_WAR_SHARE = 0.25         # of the civilian factories, at 8 oil a day each


# 27.10: the trade law holds back its share of what our states mine for the world market,
# sold or not (UK 1940, export_focus: 135 aluminium mined, 92 at hand with 23 imported;
# at hand = (mined - synthetic) x 0.5 + synthetic + imports, the same for all seven).
def oil_room(g):
    """(oil a day the war may still buy, the sellers' spare), for the fuel-hold check."""
    ind = B.module("industry")
    have = sum(a for _, res, a, _ in ind.deals(g) if res == "oil")
    room = int(OIL_WAR_SHARE * g.player().civilian_factories * 8 - have)
    spare = sum(x for _, x in ind.sellers(g, "oil", exclude={g.player_tag()}, top=8))
    return room, spare


RES_SYNTH = 0x110            # the part no law exports (synthetic refineries and the like)
LAW_SHARE = {"free_trade": 0.8, "export_focus": 0.5, "limited_exports": 0.25,
             "closed_economy": 0.0}


def held_back(g):
    """{resource: (mined less synthetic, held back by the trade law, sold by deals)}."""
    ind = B.module("industry")
    p = g.p
    r = p.u64(g.player().ptr + ind.CO_RESOURCES)
    sold = collections.Counter()
    base = p.u64(r + ind.CR_DEALS)
    if base:
        for nm, rp in ind.strategic_resources(g).items():
            i = p.i32(rp + ind.SR_INDEX)
            d, n = p.u64(base + 24 * i), p.i32(base + 24 * i + 0xC)
            if d and 0 < n < 500:
                for k in range(n):
                    sold[nm] += p.i64(p.u64(d + 8 * k) + ind.EX_AMOUNT) / 1e5
    out = {}
    for i, nm in enumerate(ind.RES_NAMES):
        own = p.i64(r + ind.RES_OWN + 16 * i) / 1e5
        synth = p.i64(r + RES_SYNTH + 16 * i) / 1e5
        av = p.i64(r + ind.RES_AVAILABLE + 16 * i) / 1e5
        imp = p.i64(r + ind.RES_IMPORTED + 16 * i) / 1e5
        out[nm] = (own - synth, max(0.0, own - av + imp), sold[nm])
    return out


def trade_law_lines(g, law, at_war):
    """What the trade law in force costs our lines, and what a lower one gives back."""
    share = LAW_SHARE.get(law)
    if not share or share <= 0.25:
        return []
    h = held_back(g)
    bal = B.module("industry").resource_balance(g)
    lost = [(nm, w, s) for nm, (m, w, s) in h.items() if w >= 4]
    if not lost:
        return []
    back = {nm: m * (share - 0.25) for nm, (m, w, s) in h.items()}
    short = [nm for nm, v in bal.items() if v[3] < 0.1 * max(v[2], 10)]
    over = ["%s %d of %d" % (nm, s, 0.25 * h[nm][0]) for nm, w, s in lost if s > 0.25 * h[nm][0] + 1]
    out = ["TRADE LAW %s holds back %d%% of what our states mine for the world market, "
           "sold or not; what other countries buy of it pays us civilian factories, the "
           "rest is lost to our lines. A day: %s." % (
               law, share * 100, ", ".join("%s %d held back (%d sold)" % (nm, w, s)
                                           for nm, w, s in lost))]
    out.append("  limited_exports (150 political power; %s) holds back 25%%: %s a day more "
               "for our lines%s, military factory output +5%%, construction speed -5%% "
               "(export_focus gives +10%%)%s." % (
                   "a democracy may take it at war" if at_war else "a democracy only at war",
                   ", ".join("%s +%d" % (nm, v) for nm, v in back.items() if v >= 4),
                   " (short or thin now: %s)" % ", ".join(short) if short else "",
                   "; deals beyond the new share are cut: %s" % ", ".join(over) if over else ""))
    return out


def our_states(g, mv, holders):
    """Our own states (owned and held), as 'Name #id', for construction orders."""
    own = g.player_tag()
    return [mv.label(s) for s, (o, c) in sorted(holders.items(), key=lambda kv: mv.name(kv[0]))
            if o == own and c == own]


_XOR = None
_BOOST = None


def research_boosts():
    """{tech: research speed it adds} from common/technologies (26.10: computing_machine
    stood in AVAILABLE RESEARCH for 51 briefings and the user put it in a slot himself)."""
    global _BOOST
    if _BOOST is None:
        import glob
        import os
        _BOOST = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "technologies", "*.txt")):
            txt = re.sub(r"#[^\n]*", "", open(f, encoding="utf-8-sig", errors="replace").read())
            heads = list(re.finditer(r"(?m)^\t(\w+)\s*=\s*\{", txt))
            for i, m in enumerate(heads):
                body = txt[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(txt)]
                r = re.search(r"\bresearch_speed_factor\s*=\s*([\d.]+)", body)
                if r:
                    _BOOST[m.group(1)] = float(r.group(1))
    return _BOOST


def xor_of():
    """{tech: {techs it excludes}} from the XOR blocks of common/technologies (26.10: the
    UK took concentrated_industry, then put dispersed_industry in a slot; finishing it
    would have burnt the concentrated branch)."""
    global _XOR
    if _XOR is None:
        import glob
        import os
        _XOR = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "technologies", "*.txt")):
            txt = re.sub(r"#[^\n]*", "", open(f, encoding="utf-8-sig", errors="replace").read())
            heads = list(re.finditer(r"(?m)^\t(\w+)\s*=\s*\{", txt))
            for i, m in enumerate(heads):
                body = txt[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(txt)]
                x = re.search(r"\bXOR\s*=\s*\{([^}]*)\}", body)
                if x:
                    for o in x.group(1).split():
                        _XOR.setdefault(m.group(1), set()).add(o)
                        _XOR.setdefault(o, set()).add(m.group(1))
    return _XOR


def branch_closed(g, skip_slot=None):
    """{tech: the tech we hold or research that excludes it}."""
    X = xor_of()
    done = set(g.researched())
    have = done | {s["researching"] for s in g.research_slots()
                   if s["researching"] and s["slot"] != skip_slot}
    return {t: o for o in have for t in X.get(o, ()) if t not in done}


def branch_conflicts(g):
    """[(slot, tech in it, the tech it would cancel)]: a slot on the branch that burns
    one we hold or research in another slot."""
    out = []
    for s in g.research_slots():
        t = s["researching"]
        if t:
            o = branch_closed(g, skip_slot=s["slot"]).get(t)
            if o:
                out.append((s["slot"], t, o))
    return out


def research(g, tech, slot=None):
    r = g.start_research(tech, slot=slot)
    if not r.get("ok"):
        raise B_err(r.get("error") or "the game did not start %s (not available?)" % tech)
    return r


def set_line(g, line, factories):
    r = g.set_production_amount(int(line), int(factories))
    if not r.get("ok"):
        raise B_err(r.get("error") or r.get("note") or "not changed")
    return r


def remove_line(g, line):
    lines = g.production_lines()
    if not 0 <= int(line) < len(lines):
        raise B_err("no line %s" % line)
    eq = lines[int(line)]["equipment"]
    r = g.remove_production_line(int(line))
    if not r.get("ok"):
        raise B_err(r.get("error") or "the game kept the line")
    return {"removed": int(line), "equipment": eq,
            "note": "the lines after it moved up by one: read PRODUCTION LINES again"}


def prioritize_construction(g, building):
    """Move every queued project of a building type to the front of the construction
    queue, in their own order; the rest keep theirs behind them (21.10: forts for the
    Belgian border queued at the war sat behind dozens of arms factories)."""
    before = g.construction_queue()
    q = [x for x in before if x.get("building") == building]
    if not q:
        raise B_err("no %s in the construction queue" % building)
    moved = 0
    for pos in range(len(q)):
        current = g.construction_queue()
        matches = [x for x in current if x.get("building") == building]
        if pos >= len(matches):
            return {"ok": False, "confirmed": False, "moved": moved, "building": building,
                    "queue_front": queue_order(g), "error": "queue changed; read it before retrying"}
        row = matches[pos]
        if row["index"] == pos:
            continue
        result = g.move_construction(row["index"], pos)
        after = g.construction_queue()
        # Check observed order as well as the bridge result: an accepted post may no-op.
        at_head = [x.get("building") for x in after[:pos + 1]] == [building] * (pos + 1)
        same_row = (not row.get("handle") or
                    (len(after) > pos and after[pos].get("handle") == row["handle"]))
        if not result.get("ok") or not at_head or not same_row:
            return {"ok": False, "confirmed": False, "moved": moved, "building": building,
                    "queue_front": queue_order(g), "error": result.get("error") or
                    "priority change not confirmed; read queue before retrying"}
        moved += 1
    return {"ok": True, "confirmed": True, "moved": moved, "building": building,
            "queue_front": queue_order(g)}


def queue_order(g, top=8):
    """The construction queue front to back, runs of one building joined: 'bunker x3,
    arms_factory x5, ...'."""
    runs = []
    for x in g.construction_queue():
        b = x.get("building") or "?"
        if runs and runs[-1][0] == b:
            runs[-1][1] += 1
        else:
            runs.append([b, 1])
    return ", ".join("%s x%d" % (b, n) if n > 1 else b for b, n in runs[:top]) + (
        ", ..." if len(runs) > top else "")


def forts_waiting_alarm(g):
    """At war: forts (bunker, coastal_bunker) queued behind other building. The minister
    is told, and decides with prioritize_construction (21.10: the Maginot extension sat at
    the end of the queue behind dozens of arms factories after the war began)."""
    if not FUEL_POLICY.get("at_war"):
        return []
    q = g.construction_queue()
    first = next((i for i, x in enumerate(q) if x.get("building") in PROVINCE_BUILDINGS),
                 None)
    if first is None:
        return []
    ahead = [x for x in q[:first] if x.get("building") not in PROVINCE_BUILDINGS]
    if len(ahead) < 3:
        return []
    # no counts in the text: an alarm wakes once per wording (see QUEUE ORDER for them)
    return ["at war, fort projects wait behind other projects in the construction queue; "
            "prioritize_construction moves them up if they matter now"]


def cancel_construction(g, building, count):
    """Take the last `count` queued projects of a building type out of the queue (the
    tail first: what would be built last). A started project is never taken out: the
    work in it is lost (20.10: count 0 took out a civilian factory at 96%)."""
    if int(count) < 1:
        raise B_err("count must be 1 or more")
    q = [x for x in g.construction_queue() if x.get("building") == building]
    if not q:
        raise B_err("no %s in the construction queue" % building)
    # one entry per state holds all its queued levels; it goes out whole (20.10)
    fresh, n = [], 0
    for x in [x for x in q if not x.get("done")][::-1]:
        if n >= int(count):
            break
        fresh.append(x["index"])
        n += max(1, x.get("levels") or 1)
    started = len(q) - len([x for x in q if not x.get("done")])
    if not fresh:
        raise B_err("every queued %s is already started (%d); a started project is "
                    "finished, not cancelled" % (building, started))
    cancelled, failures, pending = 0, [], False
    for i in fresh:                    # from the tail, so the indices ahead stay valid
        result = g.remove_construction(i)
        if result.get("confirmed"):
            cancelled += max(0, int(result.get("cancelled_levels") or 0))
        if not result.get("ok"):
            failures.append({"index": i, "error": result.get("error") or "cancellation not confirmed"})
            pending = bool(result.get("pending") or result.get("posted"))
            # An uncertain command must be reconciled from a new briefing; do not
            # send further cancellations using indices captured before that post.
            break
    out = {"ok": not failures, "cancelled": cancelled, "building": building,
           "selected_levels": n, "confirmed": not failures}
    if failures:
        out.update(pending=pending, error=failures[0]["error"], failures=failures)
    if started:
        out["kept"] = "%d started project(s) stay: a started project is finished" % started
    return out


SHARED_SLOTS = ("arms_factory", "industrial_complex", "dockyard", "synthetic_refinery",
                "fuel_silo")
PROVINCE_BUILDINGS = ("bunker", "coastal_bunker")


def border_provinces(mv, holders, state, own, facing=None, coast=False):
    """Land provinces of a state on the border: with another country's land (or with
    `facing` only), or with the sea for coastal forts."""
    out = []
    for p in mv.land_provinces(state):
        for n in mv.adj.get(p, ()):
            if coast:
                if n not in mv.land:
                    out.append(p)
                    break
                continue
            if n not in mv.land:
                continue
            o = holders.get(mv.state_of.get(n), (None, None))[0]
            if o and o != own and (facing is None or o == facing):
                out.append(p)
                break
    return out


def build_forts(g, mv, holders, state, building, levels=1, facing=None, provinces=None):
    """Forts are built in provinces, not states (19.10: every bunker queued in a state was
    refused): one queue entry of `levels` in each border province of the state."""
    own = g.player_tag()
    state = int(state)
    if provinces is not None:
        from . import ops
        provs = [int(pr) for pr in provinces]
        if not provs or len(provs) != len(set(provs)):
            raise B_err("give a nonempty list of unique province ids")
        ctl = ops._ctl(g)
        bad = [pr for pr in provs if pr not in mv.land or mv.state_of.get(pr) != state
               or ctl(pr) != own or (building == "coastal_bunker" and pr not in mv.coast)]
        if bad:
            raise B_err("provinces %s must be our controlled land in state #%d%s" % (
                bad, state, " on the coast" if building == "coastal_bunker" else ""))
    else:
        provs = border_provinces(mv, holders, state, own, facing,
                                 coast=building == "coastal_bunker")
    if not provs:
        raise B_err("state #%s has no %s province%s" % (
            state, "coastal" if building == "coastal_bunker" else "border",
            (" facing %s" % facing) if facing else ""))
    done, refused = [], []
    for p in provs:
        if g.add_construction(int(state), building, level=max(1, min(int(levels), 10)),
                              province=p).get("ok"):
            done.append(p)
        else:
            refused.append(p)
    if not done:
        raise B_err("the game queued no %s in state #%s (provinces %s: already at the "
                    "highest level, or no free slot)" % (building, state, provs))
    return {"ok": not refused, "confirmed": not refused, "queued": len(done),
            "requested_provinces": provs, "refused_provinces": refused,
            "building": building, "levels": levels, "state": state, "provinces": done}


def build_port(g, mv, holders, state, levels=1):
    """A naval base is built in one coastal province: the one that already holds the
    biggest base (the 1936 map), else the first on the coast. 24.10: industry had no
    naval_base and queued a dockyard for "a port at Kitakyushu"."""
    provs = border_provinces(mv, holders, int(state), g.player_tag(), coast=True)
    if not provs:
        raise B_err("state #%s has no coastal province" % state)
    provs.sort(key=lambda p: -mv.ports.get(p, 0))
    for p in provs:
        if g.add_construction(int(state), "naval_base", level=max(1, min(int(levels), 10)),
                              province=p).get("ok"):
            return {"queued": 1, "building": "naval_base", "levels": levels,
                    "state": int(state), "province": p,
                    "base_before": mv.ports.get(p, 0) or None}
    raise B_err("the game queued no naval_base in state #%s (coastal provinces %s: at the "
                "highest level, or no free slot)" % (state, provs))


def build_facility(g, mv, holders, state, building):
    """A special project facility stands in one province (one per state); a naval one on
    the coast."""
    provs = (border_provinces(mv, holders, int(state), g.player_tag(), coast=True)
             if building == "naval_facility" else list(mv.land_provinces(int(state))))
    if not provs:
        raise B_err("state #%s has no %sprovince for a %s" % (
            state, "coastal " if building == "naval_facility" else "", building))
    for p in provs[:4]:
        if g.add_construction(int(state), building, level=1, province=p).get("ok"):
            return {"queued": 1, "building": building, "state": int(state), "province": p}
    raise B_err("the game queued no %s in state #%s (one facility per state; the state may "
                "have one, or not be ours)" % (building, state))


# states where the game refused a factory: a slot frees only when a project is
# cancelled, so the minister is shown them (20.10: 40 refused orders in four months on
# interior states already filled by the levels queued in them)
FULL = {}                              # state id -> game day of the refusal


def full_states(g, days=60):
    today = g.date()["raw"] // 24
    return sorted(s for s, d in FULL.items() if today - d < days)


def build(g, state, building, count=1, mv=None, holders=None, facing=None, provinces=None):
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
        raise B_err("count must be an integer from 1 to 10")
    if building not in BUILDINGS:
        raise B_err("unknown building %r; one of %s" % (building, ", ".join(BUILDINGS)))
    if building in PROVINCE_BUILDINGS:
        return build_forts(g, mv, holders, state, building, count, facing, provinces)
    if provinces is not None:
        raise B_err("explicit provinces are supported for bunker and coastal_bunker")
    if building == "naval_base":
        return build_port(g, mv, holders, state, count)
    if building in FACILITIES:
        return build_facility(g, mv, holders, state, building)
    done = 0
    for _ in range(max(1, min(int(count), 10))):
        r = g.add_construction(int(state), building)
        if not r.get("ok"):
            break
        done += 1
    if done < max(1, min(int(count), 10)) and building in SHARED_SLOTS:
        FULL[int(state)] = g.date()["raw"] // 24
    elif done:
        FULL.pop(int(state), None)
    if not done:
        raise B_err("the game queued no %s in state #%s: no free slot there (the levels "
                    "already queued take slots too), or not ours" % (building, state))
    requested = max(1, min(int(count), 10))
    return {"ok": done == requested, "confirmed": done == requested,
            "queued": done, "requested": requested, "building": building, "state": int(state)}


def construction_locations(g, mv, holders, state):
    """Province-level choices for the model; no site or defensive line is selected."""
    from . import ops
    state = int(state)
    own = g.player_tag()
    if holders.get(state, (None, None)) != (own, own):
        raise B_err("state #%d must be owned and controlled by us" % state)
    ctl = ops._ctl(g)
    return {"state": state, "provinces": [
        {"province": pr, "terrain": mv.terrain.get(pr), "city": mv.city_at.get(pr),
         "coastal": pr in mv.coast, "land_fort_level": mv.forts.get(pr, 0)}
        for pr in sorted(mv.land_provinces(state)) if ctl(pr) == own],
        "note": "Choose explicit provinces for inland forts. Existing forts are the "
                "last read snapshot; game CanExecute determines available construction."}



def _pick_modules(g, frame_id, choices):
    """{slot: module}: for a slot with choices, the first the game accepts (researched),
    tried one slot at a time on top of the single-choice slots."""
    ind = B.module("industry")
    base = {s: c[0] for s, c in choices.items() if len(c) == 1 and c[0] != "empty"}
    out = dict(base)
    for slot, cands in choices.items():
        if len(cands) < 2:
            continue
        for m in cands:
            if m == "empty":
                continue
            probe = dict(base)
            probe[slot] = m
            if ind.design_from_modules(g, frame_id, probe, "probe", dry_run=True,
                                       check_required=False).get("ok"):
                out[slot] = m
                break
    return out


def broken_designs(g, only=None):
    """[(id, equipment, design, [empty required slots])] of our designs."""
    ind = B.module("industry")
    out = []
    for v in ind.variants(g):
        if v["frame"] or (only and not only(v["equipment"])):
            continue
        miss = ind.missing_required(g, v["ptr"])
        if miss:
            out.append((v["id"], v["equipment"], v["design"], miss))
    return out


def new_design(g, design_id, frame_id, name, preset=None):
    ind = B.module("industry")
    if preset:
        from . import designs
        fr = next((v for v in ind.variants(g) if v["id"] == int(frame_id) and v["frame"]), None)
        if fr is None:
            raise B_err("%s is not one of our frames (EMPTY FRAME in the designs)" % frame_id)
        pr = next((x for x in designs.for_frame(fr["equipment"], g.player_tag())
                   if x["name"] == preset), None)
        if pr is None:
            raise B_err("no preset %s for %s" % (preset, fr["equipment"]))
        mods = _pick_modules(g, int(frame_id),
                             designs.expand(pr["modules"], set(g.researched())))
        empty = [s for s, c in designs.expand(pr["modules"], set(g.researched())).items()
                 if not c]
        if empty:
            raise B_err("preset %s needs modules we have not researched for %s"
                        % (preset, ", ".join(empty)))
        r = ind.design_from_modules(g, int(frame_id), mods, str(name))
    elif design_id is None:
        raise B_err("give a preset, or a design_id whose modules go on the frame")
    else:
        r = ind.new_design(g, int(design_id), int(frame_id), str(name))
    if not r.get("ok"):
        raise B_err(r.get("error") or "no design made")
    r.pop("ok", None)
    r["next"] = ("open a production line for it (new_line %d) and move the factories of "
                 "the old design to it" % r["id"])
    return r


def design_hints(g, only=None):
    """Lines: our experience, and designs that could go on a newer frame."""
    ind = B.module("industry")
    xp = ind.experience(g)
    from . import designs
    rows = [r for r in ind.newer_frames(g) if not only or only(r[2])]
    out = ["EXPERIENCE (pays for new designs): army %s, navy %s, air %s"
           % (xp["army"], xp["navy"], xp["air"])]
    tag = g.player_tag()
    pre = []
    for v in ind.variants(g):
        if not v["frame"] or (only and not only(v["equipment"])):
            continue
        ps = designs.for_frame(v["equipment"], tag)
        if ps:
            pre.append("frame %d %s: %s" % (v["id"], v["equipment"], ", ".join(
                "%s (%s: %s)" % (x["name"], "/".join(x["roles"]), " ".join(
                    "|".join(c) for c in x["modules"].values() if c != ["empty"]))
                for x in ps[:4])))
    if pre:
        out.append("DESIGN PRESETS, the game's own designs by role (new_design with frame_id "
                   "and preset; a preset needing a module we have not researched is refused): " + "; ".join(pre))
    bad = broken_designs(g, only)
    if bad:
        out.append("DESIGNS THAT CANNOT FIGHT (a required slot is empty: no weapon or no "
                   "engine; every plane or tank of them is wasted): " + "; ".join(
                       '%d %s "%s": empty %s' % (i, eq, d, ", ".join(m))
                       for i, eq, d, m in bad)
                   + ". Make a working design (new_design with a preset), move the line's "
                   "factories to it and close the broken line.")
    if rows:
        out.append("NEWER FRAMES WITHOUT A DESIGN OF OURS (new_design puts a design's "
                   "modules on the newer frame; until then the old model is all we can "
                   "build): " + "; ".join('design %d "%s" (%s) -> frame %d %s'
                                         % r for r in rows))
    return out


def new_line(g, variant_id, factories=0):
    """Open a production line for one of our designs and give it factories."""
    r = B.module("industry").add_line(g, int(variant_id))
    if not r.get("ok"):
        raise B_err(r.get("error") or "the game opened no line (%s)" % r)
    out = {"line": r["lines_after"] - 1, "equipment": r["equipment"], "design": r["design"]}
    if factories:
        out["factories"] = set_line(g, out["line"], int(factories)).get("after")
    return out


def set_ships(g, line, ships):
    """How many ships a ship line still builds (None: endless)."""
    r = g.set_ships_to_build(int(line), ships)
    if not r.get("ok"):
        raise B_err(r.get("error") or "the game did not change the series (%s)" % r)
    return r


def buy(g, seller, resource, amount, own_tag, enemies):
    """Import a resource from another country (paid in civilian factories)."""
    seller = seller.upper()
    if seller == own_tag or seller in enemies:
        raise B_err("%s cannot sell to us" % seller)
    ind = B.module("industry")
    bal = ind.resource_balance(g).get(resource)
    if bal is None:
        raise B_err("unknown resource %r" % resource)
    av, imp, used, short = bal
    if resource == "oil" and FUEL_POLICY.get("at_war"):
        stock, cap = fuel(g)
        if cap and stock >= 0.9 * cap:
            raise B_err("fuel storage is full (%d of %d): oil bought now is lost; build "
                        "fuel silos first" % (stock, cap))
        have = sum(a for _, res, a, _ in ind.deals(g) if res == "oil")
        room = int(OIL_WAR_SHARE * g.player().civilian_factories * 8 - have)
        if room < 4:
            raise B_err("oil imports already take %d of %d a day, a quarter of our civilian "
                        "factories: build synthetic refineries for more" % (have, have + room))
        _deal_reaches(g, ind, seller, resource)
        amount = min(int(amount), room)
        r = ind.trade(g, seller, resource, amount)
        if not r.get("ok"):
            raise B_err(r.get("error") or "the game made no trade")
        return {"bought": resource, "amount": amount, "from": seller,
                "note": "at war oil is bought, not saved: up to a quarter of the civilian "
                        "factories (%d a day more allowed)" % (room - amount)}
    if resource == "oil":
        # oil left over after production becomes fuel; its balance says nothing about
        # the fuel (i36, 12.1939: five oil buys refused as "no shortage" while fuel fell
        # 360 a day toward the plan's floor)
        net = fuel_report(g)[2]
        t = fuel_target(g)
        if (net is None or net >= 0) and not (t and fuel(g)[0] < t[0]):
            raise B_err("fuel is not falling and stands at the plan's reserve: buy no oil")
        want = max(-(net or 0), 0) / FUEL_PER_OIL
        if t and fuel(g)[0] < t[0]:
            want += (t[0] - fuel(g)[0]) / (60 * FUEL_PER_OIL)   # refill over two months
        short = min(short, -want - 4)
    # 28.01.1940: the minister bought tungsten again at +4.6, from his own notes
    if short >= 0:
        raise B_err("no shortage of %s: %g at hand (%g of it imported) against %g used, "
                    "balance %+g. Deals already made keep delivering; buy nothing"
                    % (resource, av, imp, used, short))
    _deal_reaches(g, ind, seller, resource)
    amount = min(int(amount), int(-short) + 4)
    r = ind.trade(g, seller, resource, amount)
    if not r.get("ok"):
        raise B_err(r.get("error") or "the game made no trade")
    return {"bought": resource, "amount": int(amount), "from": seller}


def _deal_reaches(g, ind, seller, resource):
    # 20.03.1940: tungsten bought from Portugal three times over arrived at 5% (the
    # route runs through the blockade); the minister kept buying more
    for s, res, amt, share in ind.deals(g):
        if s == seller and res == resource and share < 0.5:
            raise B_err("our deal with %s for %s already arrives at only %d%% (%g of it a "
                        "day): the route is cut. Buying more there brings nothing; buy from "
                        "a seller whose goods can reach us, or do without"
                        % (seller, resource, share * 100, amt))


def shortages(g, enemies, own_tag):
    """[(resource, balance, [(seller, surplus)])] for every resource we lack."""
    ind = B.module("industry")
    out = []
    for res, (av, imp, used, bal) in ind.resource_balance(g).items():
        if bal < 0:
            out.append((res, bal, ind.sellers(g, res, exclude=set(enemies) | {own_tag})))
    return out


# ------------------------------------------------------------------ energy (coal)
# The game's energy_ratio trigger (CEnergyFullfilmentTrigger, 1.19.3) reads the share of
# the factories' energy demand met as a fixed-point x1e5 at (country +0xF40) +0x3A8; it
# stops at 1.0. Energy = 10 + 9 per coal (+15% per coal from each of two industry techs);
# each factory costs 0.25, rising with the factory count (cap 6.6); a civilian economy
# needs 30% less. Below 100% the factories lose output. (23.10: Japan of 1936 met 100%
# with 56 coal and 54 factories.)
def energy_ratio(g):
    from .bridge import module as B
    p = g.p
    eco = p.u64(g.player().ptr + 0xF40)
    return p.i64(eco + 0x3C0) / 1e5 if eco else None      # 1.19.3: +0x3A8


def energy_line(g):
    r = energy_ratio(g)
    if r is None:
        return []
    if r >= 0.995:
        return ["ENERGY: the factories' energy demand is met (100%). It grows faster than "
                "the factory count; coal (and the coal-energy industry techs) covers it."]
    return ["ENERGY SHORT: only %d%% of the factories' energy demand is met, and the "
            "factories lose output for it. Energy comes from coal (9 per coal a day, +15%% "
            "per coal for each of the two industry techs on coal-firing); buy or take "
            "coal, research those techs; a lower mobilization law needs less." % (r * 100)]


# consumer goods by economy law (common/ideas/_economic.txt consumer_goods_expected_value;
# national spirits move it further and are not counted)
CONSUMER_GOODS = {"isolation": 0.40, "civilian_economy": 0.35, "low_economic_mobilisation": 0.30,
                  "partial_economic_mobilisation": 0.25, "war_economy": 0.20,
                  "tot_economic_mobilisation": 0.15}


def construction_civs(g):
    """About how many civilian factories build: all of them less the consumer goods and
    the factories paying for imports (8 resources a day a factory)."""
    c = g.player()
    law = B.module("cabinet").laws(g)["economy"]["now"]
    cg = CONSUMER_GOODS.get(law, 0.30)
    try:
        trade = sum(a for _, _, a, _ in B.module("industry").deals(g)) / 8.0
    except Exception:
        trade = 0.0
    return max(0, int(round(c.civilian_factories - cg * (c.civilian_factories + c.military_factories)
                            - trade)))


# ------------------------------------------------------------------ the arms market (26.10)
# Checked live 26.10 (UK 1938): 5 rifles put on sale showed in the game's market; 20
# rifles bought from Canada left its market stockpile and reached ours in three days.
# Only what a seller has put on the market can be bought.
MARKET_READY = True
def market_lines(g, own, enemies):
    """What others sell on the international market, by equipment, and what we offer."""
    M = B.module("market")
    offers = M.offers(g, exclude=set(enemies) | {own})
    by = {}
    for t, items in offers.items():
        for _, eq, d, amt in items:
            fam = re.sub(r"_\d+$", "", eq or "?")
            by.setdefault(fam, []).append((amt, t, eq, d))
    out = []
    if by:
        rows = []
        for fam, xs in sorted(by.items(), key=lambda kv: -sum(x[0] for x in kv[1])):
            xs.sort(reverse=True)
            rows.append("%s %d (%s)" % (fam, sum(x[0] for x in xs), ", ".join(
                "%s %d %s%s" % (t, a, eq, ' "%s"' % d if d else "") for a, t, eq, d in xs[:4])))
        out.append("ARMS MARKET, what other countries sell (equipment total (seller amount "
                   "model)); market_buy asks a seller for some, paid in civilian factories "
                   "for as long as the contract runs: " + "; ".join(rows[:14]))
    ours = M.on_sale(g)
    out.append("OUR MARKET STOCKPILE (on sale to others; market_sell puts more there): " + (
        ", ".join("%s%s %d" % (eq, ' "%s"' % d if d else "", a) for _, eq, d, a in ours)
        or "nothing"))
    return out


def plane_offers_line(g, own, enemies):
    """Planes others sell on the arms market (27.10, the user: buy fighters there while
    the Luftwaffe contests our sky and the factories cannot keep up)."""
    M = B.module("market")
    rows = []
    for t, items in M.offers(g, exclude=set(enemies) | {own}).items():
        for _, eq, d, amt in items:
            if eq and any(w in eq for w in ("plane", "fighter", "bomber", "cas_", "interceptor")):
                rows.append((amt, t, eq, d))
    rows.sort(reverse=True)
    return ("PLANES ON THE ARMS MARKET (seller amount equipment model; the industry buys them "
            "with market_buy, ask it with order_aircraft): " + ("; ".join(
                "%s %d %s%s" % (t, a, eq, ' "%s"' % d if d else "") for a, t, eq, d in rows[:10])
                or "none on sale"))


def market_buy(g, seller, equipment, amount, design=None):
    M = B.module("market")
    seller = seller.upper()
    items = [x for x in M.on_sale(g, seller) if x[1] == equipment
             and (not design or x[2] == design)]
    if not items:
        have = ", ".join("%s %d" % (x[1], x[3]) for x in M.on_sale(g, seller)) or "nothing"
        raise B_err("%s sells no %s%s (it sells: %s)" % (
            seller, equipment, ' "%s"' % design if design else "", have))
    v, eq, d, avail = max(items, key=lambda x: x[3])
    amount = min(float(amount), avail)
    ok, why, price = M.buy(g, seller, v, amount)
    if not ok:
        raise B_err(why)
    return {"asked": seller, "equipment": eq, "design": d, "amount": amount,
            "price": price, "note": "the seller answers; the contract shows in the market"}


def market_sell(g, design_id, amount):
    ind = B.module("industry")
    v = next((x for x in ind.variants(g) if x["id"] == int(design_id)), None)
    if v is None:
        raise B_err("no design %s of ours" % design_id)
    ok, why = B.module("market").put_on_sale(g, v["ptr"], float(amount))
    if not ok:
        raise B_err(why)
    return {"on_sale": v["equipment"], "design": v["design"], "amount": float(amount)}


# ------------------------------------------------------------------ repairs
# CSetMaxAllowedRepairFactoriesCommand (27.10, 1.19.3): ctor 0x22636E0 (this, int* our tag
# index, n), CanExecute 0x2263750 allows 0 <= n <= our civilian factories ([country+0xF40]
# +0x378, x1e5). The game's tooltip: "the number of Factories that will prioritize building
# repairs over construction". The user, 27.10: the Luftwaffe bombs the home islands; keep
# about 5% of the factories on repairs, at least 1-3.
REPAIR_CTOR = 0x22636E0
CO_INDUSTRY, IND_CIV = 0xF40, 0x390    # 1.19.3: 0x378


def civilian_factories(g):
    return g.p.i64(g.p.u64(g.player().ptr + CO_INDUSTRY) + IND_CIV) / 1e5


def repair_rule(civ):
    """Factories to keep on repairs: 5%, at least 1 (under 40), 2 (under 100) or 3."""
    floor = 1 if civ < 40 else 2 if civ < 100 else 3
    return max(floor, int(round(0.05 * civ)))


IND_REPAIR = 0x4C8     # int, set by the command (found 27.10: 0 -> 6; 1.19.3: 0x4B0)


def repair_factories(g):
    return g.p.i32(g.p.u64(g.player().ptr + CO_INDUSTRY) + IND_REPAIR)


def repair_line(g):
    civ = civilian_factories(g)
    n, want = repair_factories(g), repair_rule(civ)
    return ("REPAIR FACTORIES: %d of our %d civilian factories put repairs of bombed buildings "
            "before construction (repair_factories); the rule is about 5%%, at least 1-3: %d%s"
            % (n, civ, want, "; TOO FEW: set it" if n < want else ""))


def repair_alarm(g):
    n, want = repair_factories(g), repair_rule(civilian_factories(g))
    return ["only %d factories put repairs first, the rule is %d: repair_factories" % (n, want)] \
        if n < want else []


def set_repair_factories(g, n):
    p = g.p
    n = int(n)
    own = g._inj is None
    if own:
        g.attach()
    try:
        api = B.module("api")
        api.allow_call(REPAIR_CTOR)
        tag = g.malloc(8)
        p.write(tag, struct.pack("<i", B.module("diplomacy").tag_index(g, g.player_tag())))
        cmd = g.malloc(0x40)
        p.write(cmd, b"\0" * 0x40)
        g.call(REPAIR_CTOR, [cmd, tag, n])
        ce = p.u64(p.u64(cmd) + B.module("offsets").VT_CANEXECUTE) - p.base
        api.allow_call(ce)
        if not g.call(ce, [cmd, 0]) & 0xFF:
            return {"error": "the game refused %d (0 to our %d civilian factories)"
                             % (n, civilian_factories(g))}
        g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    return {"repair_factories": n, "rule": repair_rule(civilian_factories(g))}
