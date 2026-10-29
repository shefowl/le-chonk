"""Fleet commanders: each fleet with an admiral fights in a zone of sea regions of its own.

27.10, the user: one Admiralty for every fleet held the eastern Mediterranean only, argued
the centre and the west away ("a patrol would be sunk by the Italian fleet"), and a convoy
of five divisions met the Italian fleet off Malta with nobody of ours to come. "Split the
fleets into theatres with clear zones of responsibility."

The Admiralty draws the zones (naval_zone), moves task forces between fleets, bases them
and builds; a commander orders his own fleet in his own seas, and nothing else.
"""
import collections

from . import navy, ops
from .admiralty import (CAPITALS, FAR, SURFACE, Admiralty, coast_regions, composition_lines,
                        reachable,
                        fleet_lines, hops_from, ports, _regions, region_graph, sea_reasons, tf_cap)
from .agents import COUNTRY, Agent, tool

ZONES = {}        # fleet name -> [sea region ids]; run.save keeps it
SEED_HOPS = 3     # a first zone takes the seas at war this near its fleet's base


def region_fleet():
    return {r: f for f, rs in ZONES.items() for r in rs}


def prune(reach):
    """Take the seas our ships cannot reach out of every zone: {fleet: [removed]}."""
    gone = {}
    for n, rs in list(ZONES.items()):
        bad = [r for r in rs if r not in reach]
        if bad:
            gone[n] = bad
            ZONES[n] = [r for r in rs if r in reach]
            if not ZONES[n]:
                del ZONES[n]
    return gone


def seed(fp, foes, mv, sea, holders, own, enemies, our_ports, reach=None):
    """First zones: every sea at war, and the seas on our coasts, go to the nearest fleet
    with an admiral and surface ships (ties: the bigger fleet); the Admiralty redraws."""
    why, _, _ = sea_reasons(foes, mv, sea, holders, own, enemies)
    want = set(why) | set(coast_regions(mv, sea, [q for q, _, _ in our_ports]))
    if reach is not None:
        want &= reach
    reach, size = {}, {}
    for f in fp:
        surface = sum(n for t in f["task_forces"] for k, n in t["ships"].items() if k in SURFACE)
        if not (f["admiral"] and f["home"] and surface) or \
                all(t.get("reserve") for t in f["task_forces"]):
            continue
        reach[f["name"]] = hops_from(mv, sea, list(coast_regions(mv, sea, [f["home"]])))
        size[f["name"]] = surface
    ZONES.clear()
    for r in want:
        cands = [(d[r], -size[n], n) for n, d in reach.items() if d.get(r, 99) <= SEED_HOPS]
        if cands:
            ZONES.setdefault(min(cands)[2], []).append(r)
    for n in ZONES:
        ZONES[n].sort()
    return dict(ZONES)


def troops_at_sea(g, sea):
    """{sea region: our divisions at sea there} (a division on its way stands on a sea
    province; 27.10: five divisions from home to Africa met German submarines in the Bay
    of Biscay with no escort, and the Italian fleet off Malta)."""
    out = collections.Counter()
    for d in g.divisions(limit=5000)["divisions"]:
        try:
            pid = navy._prov_id(g, int(d["province_ptr"], 16))
        except Exception:
            continue
        r = sea.region_of.get(pid)
        if r is not None:
            out[r] += 1
    return out


GUARDS = ("patrol", "strike_force", "convoy_escort")


def set_zone(g, fleet, regions, sea):
    """The Admiralty's naval_zone: these regions are this fleet's (taken from any other)."""
    regions = sorted({int(r) for r in regions})
    bad = [r for r in regions if r not in sea.name]
    if bad:
        return {"error": "not sea regions: %s" % bad}
    f = next((x for x in navy.fleet_picture(g) if x["name"].lower() == str(fleet).lower()), None)
    if f is None:
        return {"error": "no fleet of ours named %s" % fleet}
    if regions and not f["admiral"]:
        return {"error": "%s has no admiral: assign_admiral first (a zone needs a commander)"
                         % f["name"]}
    taken = {}
    for n, rs in list(ZONES.items()):
        if n == f["name"]:
            continue
        lost = [r for r in rs if r in regions]
        if lost:
            ZONES[n] = [r for r in rs if r not in regions]
            taken[n] = [sea.label(r) for r in lost]
            if not ZONES[n]:
                del ZONES[n]
    if regions:
        ZONES[f["name"]] = regions
    else:
        ZONES.pop(f["name"], None)
    out = {"fleet": f["name"], "zone": [sea.label(r) for r in regions]}
    if taken:
        out["taken_from"] = taken
    if not regions:
        out["note"] = "the fleet has no zone now: you command it directly"
    return out


def zone_lines(fp, mv, sea):
    """Lines for the Admiralty: each zone, its seas, and those beyond its fleet's reach."""
    if not ZONES:
        return []
    by = {f["name"]: f for f in fp}
    out = ["NAVAL ZONES (each fleet's commander fights these seas; a sea more than %d regions "
           "from its fleet's base is reached late):" % FAR]
    for n, rs in sorted(ZONES.items()):
        f = by.get(n)
        d = hops_from(mv, sea, list(coast_regions(mv, sea, [f["home"]]))) if f and f["home"] else {}
        far = [r for r in rs if d.get(r, 99) >= FAR]
        out.append("  %s (admiral %s): %s%s" % (
            n, (f or {}).get("admiral") or "NONE: assign one or the zone has no commander",
            ", ".join(sea.label(r) for r in rs),
            "; TOO FAR FROM ITS BASE: %s (give them to a fleet based nearer, or base a fleet "
            "there)" % ", ".join(sea.label(r) for r in far) if far else ""))
    gone = [n for n in ZONES if n not in by]
    if gone:
        out.append("  zones of fleets that no longer exist: %s (redraw them)" % ", ".join(gone))
    return out


FLEET_SYSTEM = """You command {fleet}, a fleet of the navy of {country} in Hearts of Iron IV,
in its ZONE: the sea regions the Admiralty gave it. The seas of your zone are yours to hold
and nobody else's fleet fights there; the country's own AI does not move your ships, so an
order you do not give is not given. You order only your own fleet: its mission regions
(fleet_regions, within your zone), each task force's mission, engagement and repair, ships
between your own task forces, and your home base. Ships, admirals and zones come from the
Admiralty: ask it in end_turn (it reads your report).

How your fleet fights (version 1.19):
- The fleet's mission SEA REGIONS are set for the whole fleet (fleet_regions); each TASK
  FORCE has its own mission: patrol (finds the enemy and builds naval dominance),
  strike_force (waits at base and sails to fight what the patrols find), convoy_escort,
  convoy_raiding, mines, naval_invasion_support, hold (stays in port), training.
- A PATROL is light ships on purpose: 1-2 light cruisers and 4-8 destroyers. They outrun
  battleships, and on low_risk engagement they turn away from a stronger force: the enemy's
  battle line does not catch them. When the patrol finds the enemy, the STRIKE FORCE of the
  same fleet (battleships, cruisers, carriers with 3 screens each) sails to the fight and
  arrives as the patrol's help. "His main fleet would sink my patrol" is no reason to leave
  a sea unwatched: send the patrol on low_risk; a sea nobody patrols is the enemy's, and
  his ships attack our convoys and landings there unseen.
- A patrol watches one region: as many patrols as mission regions where the enemy sails.
  Spare destroyers of a big task force become patrols: split_task_force (size 6,
  ship_type destroyer), then a light cruiser into each with ships_to_task_force.
  Patrols are few and burn fuel: they go where enemy warships sail, where our troops or
  convoys cross, or where a landing of ours goes. A QUIET sea of your zone (no enemy
  surface ship, none of our troops) gets no patrol: his submarines there hunt nothing of
  ours. Your mission regions are the seas you fight in, not your whole zone.
- ENGAGEMENT (engagement): patrols, raiders and escorts low_risk (they fight when the odds
  are good and run otherwise); medium_risk only for a force stronger than what it meets;
  a strike force always engages (the game sets it). REPAIR: a damaged force goes home on
  its own at the priority you set.
- NAVAL DOMINANCE in a region comes from our ships on patrol or strike force there and our
  bases, air bases and radar on its coast; landings and our convoys need it, and so does
  any army of ours across the sea.
- A fleet acts within reach of its home base: base it on the zone it fights
  (fleet_home_base, our bases on or next to your zone).
- OUR TROOPS crossing your zone sail as convoys, divisions aboard: a submarine or a cruiser
  that finds them sinks the divisions with the ships. Every region they cross gets
  convoy_escort and a patrol (destroyers hunt submarines) while they cross; where they
  leave your zone the next zone's commander takes them over (from home to Africa: the
  Home Fleet's zone to Gibraltar, the Mediterranean's beyond it).
- Capital ships at war sit on hold only to repair or when the fuel runs out. A fleet that
  meets a stronger one is sunk: where the enemy's battle line is stronger than yours, keep
  the strike force in port, patrol on low_risk, and ask the Admiralty for capital ships.
Act through tools; each returns what really happened. Then end_turn with what you did,
what you need from the Admiralty, and what you watch for next.
"""


def briefing(ctx, fleet):
    gw, mv = ctx["gw"], ctx["mv"]
    sea = ctx.get("sea") or navy.SeaRegions(mv)
    own, holders = ctx["own"], ctx["holders"]
    enemies = set(ctx["enemies"])
    zone = list(ZONES.get(fleet) or ())
    lines = ["DATE: %s" % ctx["date"],
             "AT WAR WITH: %s" % (", ".join(sorted(enemies)) or "nobody")]
    sup = ctx.get("supreme")
    if sup and sup.memory:
        lines.append("LE CHONK'S INTENT (his latest note): %s"
                     % sup.memory[-1].split(": ", 1)[-1][:400])
    adm = ctx.get("admiralty")
    if adm and adm.memory:
        lines.append("THE ADMIRALTY'S LATEST NOTE: %s" % adm.memory[-1].split(": ", 1)[-1][:500])
    fp = gw.run(navy.fleet_picture)
    f = next((x for x in fp if x["name"] == fleet), None)
    if f is None:
        ctx["zone_open"] = []
        return "\n".join(lines + ["YOUR FLEET %s NO LONGER EXISTS: end_turn and say so." % fleet])
    our_ports = ports(mv, holders, own)
    reach, closed = reachable(mv, sea, holders, own, enemies, our_ports)
    prune(reach)
    zone = list(ZONES.get(fleet) or ())

    def name(q):
        return "%s (prov %d, port level %d)" % (mv.label(mv.state_of.get(q)), q,
                                                 mv.ports.get(q, 0))
    lines.append("YOUR FLEET (an admiral leads up to %d task forces):" % tf_cap())
    lines += fleet_lines(gw, mv, sea, [f], tf_cap(), name)
    lines += composition_lines([f], bool(enemies))
    try:
        foes = gw.run(navy.enemy_fleets, sorted(enemies)) if enemies else {}
    except Exception:
        foes = {}
    why, surface, enemy_caps = sea_reasons(foes, mv, sea, holders, own, enemies) \
        if enemies else ({}, set(), {})
    base = hops_from(mv, sea, list(coast_regions(mv, sea, [f["home"]]))) if f["home"] else {}
    held = collections.defaultdict(list)
    guarded = set()
    for x in fp:
        for t in x["task_forces"]:
            if t["mission"] in GUARDS:
                guarded |= set(t["regions"])
            if t["mission"] in ("patrol", "strike_force"):
                for r in t["regions"]:
                    held[r].append("%s%s %s" % ("" if x["name"] == fleet else x["name"] + "'s ",
                                                t["name"], t["mission"]))
    opn = []
    troops = gw.run(troops_at_sea, sea)
    lines.append("YOUR ZONE (%d sea regions; HELD = a patrol or strike force has it; NOBODY = "
                 "the enemy sails it unseen; QUIET = no enemy surface ship and none of our "
                 "troops: no patrol needed):" % len(zone))
    for r in sorted(zone, key=lambda r: (base.get(r, 99), r)):
        d = base.get(r)
        state = "HELD (%s)" % ", ".join(held[r][:3]) if held[r] else "NOBODY"
        lines.append("  %s: %s; %s from your base%s%s" % (
            sea.label(r), state, _regions(d),
            "; " + "; ".join(why[r][:4]) if why.get(r) else "",
            "; ENEMY SURFACE SHIPS AND NOBODY OF OURS" if r in surface and not held[r] else
            "; QUIET" if r not in surface and not troops.get(r) else ""))
        if r in surface and not held[r]:
            opn.append("%s (%s)" % (sea.label(r), "; ".join(why[r][:2])))
    ours_at_sea = [(r, troops[r]) for r in zone if troops.get(r)]
    if ours_at_sea:
        lines.append("OUR TROOPS AT SEA IN YOUR ZONE (divisions aboard convoys): " + "; ".join(
            "%s: %d divisions, %s%s" % (
                sea.label(r), n, "guarded" if r in guarded else "NO ESCORT OR PATROL",
                " (enemy: %s)" % "; ".join(why[r][:2]) if why.get(r) else "")
            for r, n in ours_at_sea))
    for r, n in ours_at_sea:
        if r not in guarded:
            opn.append("%s (OUR %d DIVISIONS AT SEA THERE, unguarded%s)" % (
                sea.label(r), n, "; " + "; ".join(why[r][:2]) if why.get(r) else ""))
    mine = sorted(f.get("regions", ()))
    effective = sorted({r for t in f["task_forces"] for r in t["regions"]})
    lines.append("CONFIGURED FLEET REGIONS: %s; EFFECTIVE TASK-FORCE REGIONS: %s "
                 "(the game filters selection by reach and task-force availability)" % (
                     ", ".join(sea.label(r) for r in mine) or "none",
                     ", ".join(sea.label(r) for r in effective) or "none"))
    outside = [r for r in mine if r not in zone]
    quiet = [r for r in mine if r in zone and r not in surface and not troops.get(r)]
    if outside:
        opn.append("your mission regions %s are outside your zone or out of reach (%s): set "
                   "fleet_regions without them" % (", ".join(sea.label(r) for r in outside),
                                                   "; ".join("%s held by %s" % (
                                                       k.replace("_", " ").title(), v)
                                                       for k, v in sorted(closed.items()))
                                                   or "no strait closed"))
    if quiet:
        lines.append("QUIET SEAS AMONG YOUR MISSION REGIONS (a patrol there burns fuel for "
                     "nothing): " + ", ".join(sea.label(r) for r in quiet))
    ctx["zone_open"] = opn
    if opn:
        lines.insert(2, "ALERT: SEAS OF YOUR ZONE WHERE NOBODY OF OURS IS: %s. Enemy warships: "
                        "a patrol (low_risk) and your strike force. Our troops at sea: "
                        "convoy_escort and a patrol. fleet_regions, then the missions."
                     % "; ".join(opn))
    ours_c = collections.Counter()
    for t in f["task_forces"]:
        ours_c.update({k: n for k, n in t["ships"].items() if k in CAPITALS})
    theirs = collections.Counter()
    for r in zone:
        theirs.update(enemy_caps.get(r, {}))
    fmt = lambda c: ", ".join("%d %s" % (n, k) for k, n in c.most_common()) or "none"
    lines.append("CAPITAL SHIPS: yours %s; the enemy's in your zone's seas and ports %s%s" % (
        fmt(ours_c), fmt(theirs), "; HE IS STRONGER: ask the Admiralty for capital ships, and "
        "until they come patrol on low_risk and strike only what you can beat"
        if sum(theirs.values()) > sum(ours_c.values()) else ""))
    near = set(zone)
    for r in zone:
        near |= region_graph(mv, sea).get(r, set())
    rows = []
    for t, tfs in sorted(foes.items()):
        for x in tfs:
            loc = x["location"]
            regs = {sea.region_of[loc]} if loc in sea.region_of else \
                set(coast_regions(mv, sea, [loc])) if loc in mv.state_of else set()
            if regs & near:
                rows.append("%s %s at %s" % (t, fmt(x["ships"]), sea.label(sea.region_of[loc])
                                             if loc in sea.region_of else
                                             "port, " + mv.label(mv.state_of.get(loc))))
    lines.append("ENEMY SHIPS IN AND NEXT TO YOUR ZONE: " + ("; ".join(rows[:12]) or "none seen"))
    others = region_fleet()
    nxt = collections.defaultdict(list)
    for r in near - set(zone):
        nxt[others.get(r) or "NO ZONE"].append(sea.label(r))
    if nxt:
        lines.append("SEAS NEXT TO YOUR ZONE: " + "; ".join(
            "%s: %s" % (n, ", ".join(rs)) for n, rs in sorted(nxt.items())))
    bases = []
    for q, lvl, st in our_ports:
        regs = set(coast_regions(mv, sea, [q]))
        if regs & near:
            bases.append("%s level %d [prov %d]%s" % (
                mv.label(st), lvl, q, "" if regs & set(zone) else " (next to your zone)"))
    lines.append("OUR NAVAL BASES ON YOUR ZONE (fleet_home_base): " + ("; ".join(bases[:10]) or
                                                                       "none"))
    return "\n".join(lines)


class FleetCommander(Admiralty):
    """A fleet's own commander in its zone; the Admiralty's tools, cut to his fleet."""
    role = "navy"

    def __init__(self, fleet, model, llm, journal):
        Agent.__init__(self, fleet, model, llm, journal,
                       FLEET_SYSTEM.format(fleet=fleet, country=COUNTRY["name"]))
        self.fleet = fleet

    def tools(self):
        keep = {"task_force_mission", "engagement", "repair_priority", "ships_to_task_force",
                "split_task_force", "end_turn"}
        out = [tool("fleet_regions", "Set your fleet's mission sea regions (ids from YOUR "
                    "ZONE); every task force of the fleet takes them.",
                    {"regions": {"type": "array", "items": {"type": "integer"}}}, ["regions"]),
               tool("fleet_home_base", "Base your fleet at one of our naval bases on your "
                    "zone (its province number).", {"province": {"type": "integer"}},
                    ["province"])]
        return out + [t for t in Admiralty.tools(self) if t["function"]["name"] in keep]

    def _mine(self, g, name):
        t = self._tf(g, name)
        return t is not None and t["fleet"] == self.fleet

    def handle(self, ctx, fn, a):
        gw, mv = ctx["gw"], ctx["mv"]
        sea = ctx.get("sea") or navy.SeaRegions(mv)
        zone = set(ZONES.get(self.fleet) or ())
        if fn == "fleet_regions":
            out = [int(r) for r in a.get("regions") or () if int(r) not in zone]
            if out:
                raise ops.OpError("not in your zone: %s. Your zone: %s. The Admiralty draws "
                                  "the zones: ask it in end_turn" % (
                                      ", ".join(sea.label(r) if r in sea.name else str(r)
                                                for r in out),
                                      ", ".join(sea.label(r) for r in sorted(zone))))
            a = dict(a, fleet=self.fleet)
        elif fn == "fleet_home_base":
            near = set(zone)
            for r in zone:
                near |= region_graph(mv, sea).get(r, set())
            if not set(coast_regions(mv, sea, [int(a["province"])])) & near:
                raise ops.OpError("province %s is no base on or next to your zone" % a["province"])
            a = dict(a, fleet=self.fleet)
        elif fn in ("task_force_mission", "engagement", "repair_priority"):
            if not gw.run(self._mine, a["task_force"]):
                raise ops.OpError("%s is no task force of %s" % (a["task_force"], self.fleet))
        elif fn == "split_task_force":
            if not gw.run(self._mine, a["task_force"]):
                raise ops.OpError("%s is no task force of %s" % (a["task_force"], self.fleet))
        elif fn == "ships_to_task_force":
            if not (gw.run(self._mine, a["from"]) and gw.run(self._mine, a["to"])):
                raise ops.OpError("both task forces must be of %s; ships from other fleets "
                                  "come from the Admiralty" % self.fleet)
        return Admiralty.handle(self, ctx, fn, a)

    def before_end(self, ctx):
        opn = ctx.get("zone_open")
        if not opn or self.this_turn & {"called:fleet_regions", "called:task_force_mission"}:
            return None
        return ("Not yet: these seas of your zone have nobody of ours: " + "; ".join(opn) +
                ". Add them to your mission regions (fleet_regions): where enemy warships "
                "sail, a patrol of light ships on low_risk (they outrun his battleships) and "
                "your strike force behind it; where our troops are at sea, convoy_escort and "
                "a patrol. Or call end_turn again and say why not.")
