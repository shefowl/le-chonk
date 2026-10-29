"""The admiralty: the staff that commands the navy and builds it.

It works on its own (the user, 22.10: a player runs the country and hands the navy to
the model) or under the supreme commander in a full run. It touches only the navy: the
fleets, their admirals, bases and missions, and the dockyards' production lines.
"""
import collections

from . import economy, navy, ops
from .agents import COUNTRY, Agent, Nudge, deliver, tool
from .bridge import module as B

NAVY_SYSTEM = """You are the admiralty of {country} in Hearts of Iron IV: you command its navy
and you build it. The country's own AI does not move our fleets: an order you do not give
is not given.

How the navy works (version 1.19):
- SPECIAL PROJECTS (naval facility): super-heavy battleships, carrier and submarine
  projects that unlock hulls and modules no research gives. A facility works on one
  project at a time and needs a scientist on it; keep it busy with what our war needs.
- A FLEET has an admiral, a home base and task forces. Its mission's SEA REGIONS are set
  for the whole fleet (fleet_regions); each TASK FORCE has its own mission
  (task_force_mission): patrol (finds the enemy and builds naval dominance), strike_force
  (waits at base and sails to fight what patrols find), convoy_raiding (submarines and
  light ships against enemy trade), convoy_escort (guards our convoys),
  mines_planting/mines_sweeping, training (navy experience, costs fuel and wear),
  reserve_fleet, hold (stays in port), naval_invasion_support.
- NAVAL DOMINANCE in a sea region comes from our ships on patrol or strike force there,
  plus our naval bases, air bases and radar on its coast. A region is ours when our
  dominance reaches 60% of what is needed against the enemy's. Naval invasions need
  dominance along their whole path; dominance also cuts the convoys our trade needs.
  A landing sails from any port of ours, of any level: never hold a landing for a port
  being enlarged (n3: the Shanghai landing waited three months on Kitakyushu going from
  level 6 to 8, a level the landing never needed).
- A fleet only acts within its range of its home base: base it near where it fights
  (fleet_home_base). A naval base of level 8 can take a naval supply hub (a second
  base for range) and, with the expansion, a naval headquarters for an admiral.
- A NAVAL HEADQUARTERS (a building at a naval base of level 8) takes one admiral
  (hq_admiral): he lends his skills to every task force of ours within its range and
  gains experience from them. Up to three admirals can sit in HQs. An HQ without an
  admiral gives nothing: fill it.
- An ADMIRAL raises his fleet's fighting (attack, defence, manoeuvre, coordination);
  a fleet without one fights at a loss. Put the best admirals on the battle fleets.
- CARRIERS fight with their air groups: fighters (cover the fleet), dive bombers and
  torpedo bombers (strike ships). A carrier without planes is a hull. Put our carrier
  wings on the carriers (wing_to_carrier); a wing short of planes refills itself from the
  stockpile, so carrier planes must keep coming from the factories: order them
  (order_carrier_planes) in step with the carriers we build (a deck holds 20-60).
- Each task force has an ENGAGEMENT level (how big a fight it accepts) and a REPAIR
  priority (how damaged it must be to go home on its own). A weak force set to engage
  always is sunk; a raider fleet on "never repair" sails until it dies. New ships come
  out in the reserve fleet: give them to the fleets (task_forces_to_fleet) or send them
  to a task force (ships_to_task_force).
- Big ships decide battles, screens (destroyers, light cruisers) protect them,
  carriers strike from afar, submarines raid convoys. A fleet that fights a stronger
  one is sunk; keep it in port or out of reach until it is the stronger.
- CONVOYS carry our trade and the supply of every army overseas; without free convoys
  imports stop and armies across the sea starve. Enemy raiders sink them: escort them
  (convoy_escort) where the enemy hunts. They are built on the dockyards.
- The DOCKYARDS are yours: production lines marked [dockyards] build only ships, and a
  dockyard on no line is lost. A ship laid down is worth finishing: never take the
  yards off a line with a hull on the slip; end a series with ships=1 instead. A new
  ship class needs a design (new_design from a preset, paid in navy experience).
  Ship lines stand at the end of the production list and get resources after the army's,
  so the yards cost the army nothing: never strip them to save steel. When no sea war is
  coming, keep the yards on what is always useful: convoys (they carry our trade and the
  supply of every army overseas), escorts, submarines against an enemy's convoys.

At peace, prepare the navy for the war the country expects: build what that war needs,
keep the fleets based where they will fight, and keep the battle fleets in port (hold):
a fleet at sea burns FUEL, and at peace a patrol wins nothing. Train a part of the fleet
(training) when navy experience is short and the fuel allows, not all of it.
At war, give every fleet a mission and regions; idle fleets do nothing. At war only ships
new since the war began train, and only while no mission needs them.
At war the battle fleets go where the war is (25.10, n3: Yamamoto's battleships guarded
the empty Sea of Japan from Kita-Tohoku, the carriers sat on hold at Kitakyushu, and a
destroyer patrol met the Chinese cruisers alone in the Yellow Sea):
- ENEMY NAVY shows his ships and where they are. Waters with no enemy and no landing of
  ours need no battle fleet; bring it to the enemy's coast, based at our nearest big port.
- A patrol finds, a strike force of the SAME fleet fights: every fleet that patrols where
  the enemy has ships has its battleships, cruisers or carriers on strike_force in the
  same fleet, within reach. A patrol where the enemy is stronger and no strike force can
  come is sunk alone.
- Capital ships at war sit on hold only to repair or when the fuel runs out.
- STRIKE FORCE: the battleships, battlecruisers, heavy cruisers and carriers, with 3
  destroyers or light cruisers for each (the game screens a capital ship fully at 3; a
  carrier wants a battleship or cruiser beside it too); mission strike_force.
  PATROL: screens only, 1-2 light cruisers and 4-8 destroyers (a flotilla); a patrol
  watches one region, so a fleet with three mission regions needs three patrols.
  A big mixed task force does neither well: it keeps its capitals and 3 screens each, and
  its spare screens become patrols of their own: split_task_force with ship_type
  destroyer (then light_cruiser into them with ships_to_task_force). Submarines raid on
  their own. TASK FORCE COMPOSITION in the briefing says what each one lacks or spares.
- A PATROL is light ships on purpose: destroyers and light cruisers outrun battleships, and
  on low_risk engagement they turn away from a stronger force; the enemy's battle line does
  not catch them. When the patrol finds the enemy, the strike force of its fleet sails to
  the fight as its help. "His main fleet would sink a patrol" is no reason to leave a sea
  unwatched (27.10: nobody watched the central Mediterranean, and a convoy of five
  divisions met the Italian fleet off Malta alone).
- ENGAGEMENT: patrols, raiders and escorts low_risk (fight when the odds are good, run
  otherwise); a strike force always engages.
- SUBMARINE WAR: the submarines raid the enemy's convoys in groups of 5-8 boats, one group
  per sea his convoys sail (SUBMARINE WAR in the briefing; split_task_force makes the
  groups), all under one raider fleet you
  command, with a raider admiral (seawolf, submarine_officer). Submarines pass some
  straits surface ships cannot. Escorts the same way: groups of 5-8 destroyers on
  convoy_escort, one per sea our convoys and troops cross, under an anti-submarine admiral
  (hunter_killer, fleet_protector). Put each admiral where his traits work (ADMIRALS AND
  THEIR TRAITS); a raider on the battle line wastes both.
- NAVAL ZONES: each fleet with an admiral fights in a ZONE of sea regions under its own
  commander, who takes his turn after yours and orders that fleet's regions, missions and
  engagement. You draw the zones (naval_zone; every sea at war belongs to one), give each
  zone the ships its seas need (task_forces_to_fleet: capital ships with no enemy capital
  ship in their seas are spare, send them where his are), base its fleet where it reaches
  its seas (fleet_home_base) and keep an admiral on it (assign_admiral). A zone sea 2 or
  more regions from its fleet's base is reached late: give it to a fleet based nearer,
  or make one (a numbered fleet with an admiral, task forces moved in, based on that sea:
  Gibraltar for the western Mediterranean, not Alexandria). The commanders' reports come
  to you as MESSAGES: their requests for ships and zones are yours to answer. The fleets
  without a zone (convoy escorts, submarines, the reserve) you command directly.
- A tool that was refused changed nothing: end_turn says what stands now, not what was
  tried (26.10: "the Mediterranean Fleet is on patrol" after the game refused it).
- A landing of the war plan sails when the sea is ours: the regions on its way get
  patrols and a strike force before it, and naval_invasion_support where it lands.
- What the industry refuses twice (planes, a port) goes to Le Chonk: say it in end_turn,
  which he reads, as a request to change the plan's production; asking the industry a
  third time changes nothing.
Act through tools; each returns what really happened. Then end_turn with what you did
and what you watch for next.
"""


CAPITALS = ("carrier", "battleship", "battle_cruiser", "heavy_cruiser")
SCREENS = ("destroyer", "light_cruiser")
SCREENS_PER_CAPITAL = 3     # NDefines SCREEN_RATIO_FOR_FULL_SCREENING_FOR_CAPITALS
PATROL_MAX = 12             # NDefines SCREEN_TASKFORCE_MAX_SHIP_COUNT; a flotilla is 8


def composition_lines(fp, at_war):
    """What each task force lacks or spares for its role (26.10, the user: the
    Mediterranean Fleet, 3 battleships and 32 destroyers in one task force, sat on hold at
    Alexandria with Italy at war; patrols are screens split off, capitals strike)."""
    out = []
    spare_tfs = []
    for f in fp:
        regs = set(f.get("regions", ()))
        # a squadron with no mission region keeps quiet waters: hold is right there
        busy = bool(regs)
        patrols = 0
        for x in f["task_forces"]:
            s = x["ships"]
            caps = sum(n for k, n in s.items() if k in CAPITALS)
            scr = sum(n for k, n in s.items() if k in SCREENS)
            subs = s.get("submarine", 0)
            notes = []
            if caps:
                need = SCREENS_PER_CAPITAL * caps
                if at_war and busy and x["mission"] == "hold" and (x.get("strength") or 1) > 0.7:
                    notes.append("CAPITAL SHIPS ON HOLD AT WAR: strike_force (or "
                                 "naval_invasion_support)")
                if scr < need and (busy or x["mission"] != "hold"):
                    notes.append("short of %d screens for its %d capital ships" % (need - scr, caps))
                elif scr - need >= 4:
                    notes.append("%d screens to spare beyond 3 per capital ship: they make "
                                 "patrols (1-2 light cruisers, 4-8 destroyers each)" % (scr - need))
                if subs:
                    notes.append("%d submarines in a battle group: they raid on their own" % subs)
            else:
                if subs and scr:
                    notes.append("submarines and surface ships mixed: the submarines raid alone, "
                                 "the destroyers and cruisers patrol or escort")
                if x["mission"] == "patrol":
                    patrols += 1
                    if scr > PATROL_MAX:
                        notes.append("%d ships is two patrols' worth: a patrol watches one region"
                                     % scr)
                if scr and not subs and x["mission"] in ("hold", "reserve_fleet") and not caps:
                    spare_tfs.append("%s (%s; %s)" % (x["name"], f["name"], ", ".join(
                        "%d %s" % (n, k) for k, n in s.most_common())))
            if notes:
                out.append("  %s / \"%s\" (%s): %s" % (f["name"], x["name"], x["mission"],
                                                      "; ".join(notes)))
        battle = any(sum(n for k, n in x["ships"].items() if k in CAPITALS)
                     for x in f["task_forces"])
        if at_war and regs and battle and patrols < len(regs):
            out.append("  %s: %d mission regions, %d patrols%s" % (
                f["name"], len(regs), patrols,
                ": its strike force never sails, nobody finds the enemy for it"
                if not patrols else ""))
    if not out:
        return []
    return (["TASK FORCE COMPOSITION (a strike force: capital ships with 3 screens each; a "
             "patrol: 1-2 light cruisers and 4-8 destroyers, one per mission region):"] + out +
            (["  screen-only task forces doing nothing, to move where patrols are short "
              "(task_forces_to_fleet): " + "; ".join(spare_tfs[:10])] if spare_tfs else []))


def ports(mv, holders, own):
    """[(province, level, state)] of our naval bases (live: run.refresh_forts)."""
    out = []
    for q, lvl in mv.ports.items():
        st = mv.state_of.get(q)
        if st is not None and holders.get(st, (None, None))[1] == own:
            out.append((q, lvl, st))
    return sorted(out, key=lambda x: -x[1])


def coast_regions(mv, sea, provinces):
    """Sea regions that touch these coastal provinces."""
    out = collections.Counter()
    for q in provinces:
        for b in mv.adj.get(q, ()):
            r = sea.region_of.get(b)
            if r is not None and b not in mv.land:
                out[r] += 1
    return out


_RADJ = {}
_RULES = None


def strait_rules(mv):
    """[(rule, required provinces, navy passes as enemy, as neutral, gated province pairs)]
    from map/adjacency_rules.txt and map/adjacencies.csv."""
    global _RULES
    if _RULES is None:
        import os
        import re
        from . import bridge
        d = os.path.join(bridge.game_dir(), "map")
        txt = re.sub(r"#[^\n]*", "", open(os.path.join(d, "adjacency_rules.txt"),
                                          encoding="utf-8-sig", errors="replace").read())
        pairs = collections.defaultdict(set)
        for row in open(os.path.join(d, "adjacencies.csv"), encoding="utf-8-sig",
                        errors="replace"):
            c = row.split(";")
            if len(c) > 8 and c[8].strip() and c[0].strip().isdigit() and c[1].strip().isdigit():
                pairs[c[8].strip()].add(frozenset((int(c[0]), int(c[1]))))
        _RULES = []
        for blk in re.split(r"\badjacency_rule\s*=", txt)[1:]:
            n = re.search(r'\bname\s*=\s*"([^"]+)"', blk)
            r = re.search(r"required_provinces\s*=\s*\{([^}]*)\}", blk)
            if not n or not r:
                continue

            def passes(part, unit):
                m = re.search(r"\b%s\s*=\s*\{([^}]*)\}" % part, blk)
                v = m and re.search(r"\b%s\s*=\s*(\w+)" % unit, m.group(1))
                return not v or v.group(1) == "yes"
            _RULES.append((n.group(1), [int(x) for x in re.findall(r"\d+", r.group(1))],
                           {u: (passes("enemy", u), passes("neutral", u))
                            for u in ("navy", "submarine")}, pairs.get(n.group(1), set())))
    return _RULES


def closed_straits(mv, holders, own, enemies, unit="navy"):
    """{rule name: who holds it} of the straits our ships (unit: navy or submarine) cannot
    pass (27.10: the Black Sea stood in Somerville's zone behind the Turkish straits)."""
    out = {}
    for name, req, ok, pairs in strait_rules(mv):
        enemy_ok, neutral_ok = ok[unit]
        if not pairs:
            continue
        who = {holders.get(mv.state_of.get(q), (None, None))[1] for q in req} - {None}
        if not who or who <= {own}:
            continue
        hostile = who & set(enemies)
        if (hostile and not enemy_ok) or (not hostile and not neutral_ok):
            out[name] = ", ".join(sorted(who))
    return out


def region_graph(mv, sea, closed=()):
    """{sea region: sea regions next to it}, without the passages of closed straits."""
    cut = set()
    for name, _, _, pairs in strait_rules(mv):
        if name in closed:
            cut |= pairs
    key = frozenset(closed)
    if key not in _RADJ:
        g = collections.defaultdict(set)
        for q, r in sea.region_of.items():
            for b in mv.adj.get(q, ()):
                r2 = sea.region_of.get(b)
                if r2 is not None and r2 != r and frozenset((q, b)) not in cut:
                    g[r].add(r2)
        _RADJ[key] = g
    return _RADJ[key]


def reachable(mv, sea, holders, own, enemies, our_ports, unit="navy"):
    """(sea regions our ships reach from our bases, the closed straits)."""
    closed = closed_straits(mv, holders, own, enemies, unit)
    g = region_graph(mv, sea, closed)
    start = set(coast_regions(mv, sea, [q for q, _, _ in our_ports]))
    seen, todo = set(start), list(start)
    while todo:
        r = todo.pop()
        for b in g.get(r, ()):
            if b not in seen:
                seen.add(b)
                todo.append(b)
    return seen, closed


def hops_from(mv, sea, start):
    """{sea region: regions crossed from the regions in `start` (0 for those)}."""
    g = region_graph(mv, sea)
    dist = {r: 0 for r in start}
    todo = list(start)
    while todo:
        nxt = []
        for r in todo:
            for b in g.get(r, ()):
                if b not in dist:
                    dist[b] = dist[r] + 1
                    nxt.append(b)
        todo = nxt
    return dist


def _regions(d):
    return "?" if d is None else "on it" if d == 0 else "%d region%s" % (d, "" if d == 1 else "s")


FAR = 2   # a sea this many regions from a fleet's base is beyond its quick reach


SURFACE = ("destroyer", "light_cruiser", "heavy_cruiser", "battleship", "battle_cruiser",
           "carrier")


def sea_reasons(foes, mv, sea, holders, own, enemies):
    """Why each sea is at war: ({region: [reasons]}, regions with enemy surface ships
    at sea or in a port on them, {region: Counter of enemy capital ships there})."""
    from . import garrison
    why = collections.defaultdict(list)
    seen = collections.defaultdict(collections.Counter)
    for t, tfs in foes.items():
        for x in tfs:
            loc = x["location"]
            if loc in sea.region_of:
                seen[(sea.region_of[loc], t, "at sea")].update(x["ships"])
            elif loc in mv.state_of:
                for r in coast_regions(mv, sea, [loc]):
                    seen[(r, t, "in port " + mv.label(mv.state_of[loc]).split(" (#")[0])].update(
                        x["ships"])
    surface = {r for (r, t, where), c in seen.items() if any(k in SURFACE for k in c)}
    capitals = collections.defaultdict(collections.Counter)
    for (r, t, where), c in seen.items():
        capitals[r].update({k: n for k, n in c.items() if k in CAPITALS})
    for (r, t, where), c in seen.items():
        why[r].append("%s %s %s" % (t, ", ".join("%d %s" % (n, k) for k, n in c.most_common()),
                                    where))
    for t in enemies:
        theirs = [q for q in mv.ports if holders.get(mv.state_of.get(q), (None, None))[1] == t]
        for r in coast_regions(mv, sea, theirs):
            why[r].append("%s's coast" % t)
    for s, names in garrison.strait_keys(mv).items():
        if holders.get(s, (None, None))[1] != own:
            continue
        provs = mv.provinces.get(s, set()) & mv.coast
        for r in coast_regions(mv, sea, provs):
            why[r].append("our %s holds the %s" % (mv.label(s).split(" (#")[0], " and ".join(names)))
    return why, surface, capitals


def war_seas(fp, foes, mv, sea, holders, own, enemies, our_ports, open_out=None, zones=None,
             reach=None):
    """Lines: the seas at war, which command answers for each (the nearest home base by
    sea) and whether anything of ours holds it (27.10, the user: Somerville held the
    eastern Mediterranean from Alexandria, and the centre, the west and Gibraltar had
    nobody; the Admiralty saw each fleet's own regions only, never a sea nobody had)."""
    if not enemies:
        return []
    why, surface, enemy_caps = sea_reasons(foes, mv, sea, holders, own, enemies)
    if reach is not None:          # seas behind a strait we cannot pass are not ours to hold
        why = {r: w for r, w in why.items() if r in reach}
    if not why:
        return []
    held, escorted = collections.defaultdict(list), collections.defaultdict(list)
    for f in fp:
        for t in f["task_forces"]:
            for r in t["regions"]:
                if t["mission"] in ("patrol", "strike_force"):
                    held[r].append("%s %s" % (t["name"], t["mission"]))
                elif t["mission"] in ("convoy_escort", "convoy_raiding", "naval_invasion_support"):
                    escorted[r].append("%s %s" % (t["name"], t["mission"]))
    reach = {}
    for f in fp:
        if f["home"]:
            reach[f["name"]] = hops_from(mv, sea, list(coast_regions(mv, sea, [f["home"]])))
    size = {f["name"]: sum(sum(t["ships"].values()) for t in f["task_forces"]) for f in fp}
    home = {f["name"]: mv.label(mv.state_of.get(f["home"])).split(" (#")[0] for f in fp if f["home"]}
    base_reach = [(q, lvl, st, hops_from(mv, sea, list(coast_regions(mv, sea, [q]))))
                  for q, lvl, st in our_ports]
    zone = collections.defaultdict(list)
    for r in why:
        if zones is not None:          # the zones the Admiralty drew (naval_zone)
            zone[zones.get(r)].append(r)
            continue
        cands = [(d[r], -size[n], n) for n, d in reach.items() if r in d]
        zone[min(cands)[2] if cands else None].append(r)
    caps = {f["name"]: collections.Counter() for f in fp}
    for f in fp:
        for t in f["task_forces"]:
            caps[f["name"]].update({k: n for k, n in t["ships"].items() if k in CAPITALS})
    out = ["WAR SEAS AND WHO ANSWERS FOR THEM (a sea at war has enemy ships, an enemy's coast or "
           "a strait we hold; it falls to %s; HELD = a patrol or strike force of ours has it "
           "among its regions; NOBODY = the enemy sails it freely):" % (
               "the fleet whose ZONE holds it (naval_zone; that fleet's commander fights it)"
               if zones is not None else "the command whose home base is nearest by sea")]
    for n in sorted(zone, key=lambda n: (n is None, n or "")):
        rows = []
        for r in sorted(zone[n], key=lambda r: (reach.get(n, {}).get(r, 99), r)):
            d = reach.get(n, {}).get(r)
            state = ("HELD (%s)" % ", ".join(held[r][:3]) if held[r] else
                     "NOBODY, escorts only (%s)" % ", ".join(escorted[r][:2]) if escorted[r] else
                     "NOBODY")
            far = ""
            if not held[r]:
                near = sorted(((h.get(r, 99), -lvl, mv.label(st).split(" (#")[0], lvl)
                               for q, lvl, st, h in base_reach if r in h))
                hostile = r in surface or any(" coast" in w for w in why[r])
                strait = any(w.startswith("our ") for w in why[r])
                # the seas the turn must answer for: enemy warships, or a strait of ours on
                # an enemy's coast, with a base of ours on it or next to it
                far_zone = zones is not None and n is not None and (d is None or d >= FAR)
                if (open_out is not None and near and near[0][0] <= 1 and
                        (zones is None or n is None or far_zone) and
                        (r in surface or (strait and hostile))):
                    open_out.append("%s (%s; base %s%s)" % (
                        sea.label(r), "; ".join(why[r][:3]), near[0][2],
                        "" if near[0][0] == 0 else ", 1 region away"))
                if near and near[0][0] < (d if d is not None else 99):
                    far = "; our nearest base: %s (level %d%s)" % (
                        near[0][2], near[0][3], ", on it" if near[0][0] == 0 else
                        ", %s away" % _regions(near[0][0]))
            rows.append("    %s: %s; %s from %s's base%s; %s" % (
                sea.label(r), state, _regions(d), n, far, "; ".join(why[r][:4])))
        c = caps.get(n) or collections.Counter()
        theirs = collections.Counter()
        for r in zone[n]:
            theirs.update(enemy_caps.get(r, {}))
        out.append("  %s (home base %s; our capital ships: %s; enemy capital ships in these "
                   "seas: %s):" % (
            ("NO ZONE, no commander answers for these" if zones is not None else
             "no command reaches") if n is None else n, home.get(n, "?"),
            ", ".join("%d %s" % (k, v) for v, k in c.most_common()) or "none",
            ", ".join("%d %s" % (k, v) for v, k in theirs.most_common()) or "none"))
        out.extend(rows)
    out.append("  A sea %d or more regions from a fleet's base is reached late or not at all: a "
               "sea with NOBODY and a base of ours on it gets a fleet based there "
               "(fleet_home_base; a numbered fleet with an admiral and spare ships will do), "
               "with its own patrol and strike force. Capital ships facing an enemy with none "
               "in their seas are spare: send a task force of them where his are." % FAR)
    if zones is not None:
        out += spare_capitals(zone, caps, enemy_caps, open_out)
        out += patrol_shortage(zone, fp, surface, sea, open_out)
    return out


def patrol_shortage(zone, fp, surface, sea, open_out=None):
    """Lines: zones with fewer patrols than seas where enemy surface ships sail, against the
    destroyers and light cruisers waiting in the reserve task forces (27.10, the user:
    Somerville had two patrols for seven seas while the reserve fleets held 33 destroyers;
    the game keeps a reserve task force off its fleet's list, so it showed as nothing)."""
    by = {f["name"]: f for f in fp}
    short = []
    for n, rs in zone.items():
        if not n or n not in by:
            continue
        need = len([r for r in rs if r in surface])
        have = sum(1 for t in by[n]["task_forces"] if t["mission"] == "patrol")
        if need > have:
            short.append((need - have, n, need, have))
    waiting = []
    for f in fp:
        for t in f["task_forces"]:
            scr = {k: v for k, v in t["ships"].items() if k in SCREENS}
            if "(Detached)" in (t["name"] or ""):
                continue                # ships on their way to merge with another
            if scr and (t.get("reserve") or (f["name"] not in zone and
                                             t["mission"] in ("hold", "reserve_fleet",
                                                              "convoy_raiding"))):
                waiting.append("%s (%s, %s; %s)" % (t["name"], f["name"], t["mission"], ", ".join(
                    "%d %s" % (v, k) for k, v in t["ships"].most_common())))
    if not short or not waiting:
        return []
    short.sort(reverse=True)
    line = ("SHORT OF PATROLS (seas with enemy surface ships against the zone's patrols): %s. "
            "DESTROYERS AND LIGHT CRUISERS WAITING (reserve task forces, and screens on raiding "
            "or hold in fleets with no zone): %s. Send them: task_forces_to_fleet the task "
            "force to the short zone's fleet (its commander sends the submarines to his "
            "flotilla and makes it a patrol); if the game refuses a reserve task force, "
            "ships_to_task_force its destroyers and light cruisers to a patrol of that fleet" % (
                "; ".join("%s %d patrols for %d such seas" % (n, have, need)
                          for _, n, need, have in short), "; ".join(waiting[:8])))
    if open_out is not None:
        open_out.append(line)
    return ["  " + line]


def spare_capitals(zone, caps, enemy_caps, open_out=None):
    """Lines: zones whose seas hold more enemy capital ships than ours while another zone
    has capitals and no enemy one in its seas (27.10: the Admiralty asked Le Chonk for a
    second battle group for the Mediterranean while the Home Fleet held 9 battleships
    against a Germany with none)."""
    n_caps = lambda c: sum(c.values())
    theirs = {n: collections.Counter() for n in zone if n}
    for n in theirs:
        for r in zone[n]:
            theirs[n].update(enemy_caps.get(r, {}))
    for n in caps:
        theirs.setdefault(n, collections.Counter())
    spare = [n for n in caps if n_caps(caps[n]) and not n_caps(theirs[n])]
    short = [n for n in theirs if n_caps(theirs[n]) > n_caps(caps.get(n) or {})]
    if not (spare and short):
        return []
    fmt = lambda c: ", ".join("%d %s" % (v, k) for k, v in c.most_common())
    line = ("OUTGUNNED: %s; SPARE CAPITAL SHIPS (no enemy capital ship in their seas): %s. "
            "Move a strike force over: task_forces_to_fleet, its screens with it" % (
                "; ".join("%s has %s against %s" % (n, fmt(caps.get(n) or collections.Counter())
                                                    or "none", fmt(theirs[n])) for n in short),
                "; ".join("%s: %s" % (n, fmt(caps[n])) for n in spare)))
    if open_out is not None:
        open_out.append(line)
    return ["  " + line]


_CAP = None


def fleet_zones():
    from . import fleets
    return fleets.ZONES


def tf_cap():
    """NNavy ADMIRAL_TASKFORCE_CAP: task forces an admiral leads without penalties (26.10,
    the user: Cunningham's fleet stood at 12/10)."""
    global _CAP
    if _CAP is None:
        import os
        import re
        _CAP = 10
        try:
            from . import bridge
            t = open(os.path.join(bridge.game_dir(), "common", "defines", "00_defines.lua"),
                     encoding="utf-8", errors="replace").read()
            m = re.search(r"ADMIRAL_TASKFORCE_CAP\s*=\s*(\d+)", t)
            if m:
                _CAP = int(m.group(1))
        except OSError:
            pass
    return _CAP


def fleet_lines(gw, mv, sea, fp, cap, name, zones=None):
    """Lines: each fleet with its admiral, base, mission regions and task forces."""
    zones = zones or {}
    out = []
    for f in fp:
        n = len(f["task_forces"])
        out.append("- %s: admiral %s; home base %s; %d task forces%s" % (
            f["name"], "%s (att %d, def %d, man %d, coord %d)" % ((f["admiral"],) + tuple(f["admiral_skills"]))
            if f["admiral"] else "NONE", name(f["home"]) if f["home"] else "?", n,
            " -- OVER THE ADMIRAL'S LIMIT (%d/%d): merge small task forces "
            "(ships_to_task_force) or move some to another fleet with its own admiral "
            "(task_forces_to_fleet, assign_admiral)" % (n, cap)
            if f["admiral"] and n > cap else "") + (
            "; UNDER ITS COMMANDER, zone of %d sea regions: he orders its task forces and "
            "missions; you move task forces in and out (task_forces_to_fleet) and redraw "
            "the zone (naval_zone)" % len(zones[f["name"]]) if f["name"] in zones else ""))
        regs = sorted({r for t in f["task_forces"] for r in t["regions"]})
        out.append("  configured fleet mission regions: %s; effective task-force regions: %s" % (
            ", ".join(sea.label(r) for r in f.get("regions", ())) or "none",
            ", ".join(sea.label(r) for r in regs) or "none (range or game filters may exclude them)"))
        for t in f["task_forces"]:
            here = sea.region_of.get(t["location"])
            eng, rep = gw.run(navy.settings, t["ptr"])
            auto = gw.run(lambda g: bool(g.p.u8(t["ptr"] - 0x10 + navy.TF_AUTO)))
            out.append("  task force \"%s\": %s; %s; at %s; org %s; strength %s; engages %s, "
                         "repairs %s, auto-reinforce %s%s" % (
                t["name"], t["mission"],
                ", ".join("%d %s" % (n, k) for k, n in t["ships"].most_common()),
                sea.label(here) if here else ("port, " + mv.label(mv.state_of.get(t["location"])))
                if t["location"] in mv.state_of else "prov %s" % t["location"],
                "%d%%" % round(100 * t["org"]) if t["org"] is not None else "?",
                "%d%%" % round(100 * t["strength"]) if t.get("strength") is not None else "?",
                eng, rep, "on" if auto else "off",
                ("; THE RESERVE: new ships come out here and do nothing until handed out "
                 "(ships_to_task_force to the task forces that need them, or task_forces_to_fleet "
                 "to another fleet); keep it empty" if t.get("reserve") else "") +
                (("; DAMAGED: " + ", ".join("%s %d%%" % (k, round(100 * v))
                                            for k, v in t["damaged"])) if t.get("damaged") else "")))
    return out


def briefing(ctx):
    gw, mv = ctx["gw"], ctx["mv"]
    sea = ctx.get("sea") or navy.SeaRegions(mv)
    own, holders = ctx["own"], ctx["holders"]
    lines = ["DATE: %s" % ctx["date"],
             "AT WAR WITH: %s" % (", ".join(sorted(ctx["enemies"])) or "nobody")]
    sup = ctx.get("supreme")
    if sup and sup.memory:
        lines.append("LE CHONK'S INTENT (his latest note): %s"
                     % sup.memory[-1].split(": ", 1)[-1][:600])
    if ctx.get("player_intent"):
        lines.append("THE PLAYER'S INTENT: %s" % ctx["player_intent"])
    our_ports = ports(mv, holders, own)

    def name(q):
        return "%s (prov %d, port level %d)" % (mv.label(mv.state_of.get(q)), q,
                                                 mv.ports.get(q, 0))
    fp = gw.run(navy.fleet_picture)
    cap = tf_cap()
    lines.append("OUR FLEETS (an admiral leads up to %d task forces; past that his whole fleet "
                 "fights worse):" % cap)
    lines += fleet_lines(gw, mv, sea, fp, cap, name, fleet_zones())
    lines += composition_lines(fp, bool(ctx["enemies"]))
    adm = gw.run(navy.admirals)
    free = [a for a in adm if a["fleet"] is None]
    lines.append("ADMIRALS WITHOUT A FLEET (att/def/man/coord): " + ("; ".join(
        "%s %s%s" % (a["name"], "/".join(map(str, a["skills"])),
                     " [in naval HQ %d]" % a["hq"] if a.get("hq") else "")
        for a in sorted(free, key=lambda a: -sum(a["skills"]))) or "none"))
    from . import leaders
    lines.extend(gw.run(leaders.lines, (2,)))
    hqs = gw.run(navy.headquarters)
    lines.append("OUR NAVAL HEADQUARTERS (id, admiral, our task forces in its range): " + (
        "; ".join("%d: %s, %d task forces" % (h["id"], h["admiral"] or "NO ADMIRAL",
                                               h["task_forces_in_range"]) for h in hqs)
        or "none"))
    lines.append("OUR NAVAL BASES (best first): " + "; ".join(
        "%s level %d" % (mv.label(st), lvl) + (" [prov %d]" % q) for q, lvl, st in our_ports[:14]))
    home = coast_regions(mv, sea, [q for q, _, _ in our_ports])
    lines.append("SEA REGIONS ON OUR COASTS: " + "; ".join(sea.label(r) for r, _ in home.most_common(12)))
    for t in sorted(set(ctx["enemies"]) | set(ctx.get("expected") or ())):
        theirs = [q for q, lvl in mv.ports.items()
                  if holders.get(mv.state_of.get(q), (None, None))[1] == t]
        if theirs:
            lines.append("%s's COASTAL WATERS (regions at its naval bases): %s" % (
                t, "; ".join(sea.label(r) for r, _ in coast_regions(mv, sea, theirs).most_common(8))))
    try:
        foes = gw.run(navy.enemy_fleets, sorted(set(ctx["enemies"])))
    except Exception:
        foes = {}
    opn = []
    from . import fleets
    try:
        reach, closed = reachable(mv, sea, holders, own, set(ctx["enemies"]), our_ports)
        gone = fleets.prune(reach)
        if closed:
            lines.append("STRAITS CLOSED TO OUR SHIPS (held by others; the seas behind them are "
                         "out of reach, in no zone): %s; seas out of reach: %s" % (
                             "; ".join("%s (%s)" % (k.replace("_", " ").title(), v)
                                       for k, v in sorted(closed.items())),
                             ", ".join(sorted(sea.label(r) for r in set(sea.name) - reach))
                             or "none"))
        if gone:
            lines.append("TAKEN OUT OF ZONES, out of reach: " + "; ".join(
                "%s: %s" % (n, ", ".join(sea.label(r) for r in rs)) for n, rs in gone.items()))
        if ctx["enemies"] and not fleets.ZONES:
            fleets.seed(fp, foes, mv, sea, holders, own, set(ctx["enemies"]), our_ports, reach)
            if fleets.ZONES:
                lines.append("NAVAL ZONES DRAWN NOW, each sea at war to the nearest fleet with an "
                             "admiral: check them and redraw what is wrong (naval_zone).")
        lines += fleets.zone_lines(fp, mv, sea)
        lines += war_seas(fp, foes, mv, sea, holders, own, set(ctx["enemies"]), our_ports, opn,
                          fleets.region_fleet() if fleets.ZONES else None, reach)
    except Exception as e:
        lines.append("(war seas unavailable: %s)" % e)
    ctx["navy_open_seas"] = opn
    if opn:
        lines.insert(2, "ALERT FOR THE ADMIRALTY: " + "; ".join(opn) + ". See NAVAL ZONES and "
                     "WAR SEAS below.")
    from . import raiders
    try:
        lines += gw.run(raiders.admiral_lines, fp)
        lines += raiders.raiding_lines(fp, mv, sea, holders, own, set(ctx["enemies"]), our_ports)
    except Exception as e:
        lines.append("(submarine war unavailable: %s)" % e)
    if ctx["enemies"]:
        if not any(foes.values()):
            lines.append("ENEMY NAVIES: the enemies at war with us have no ships at sea or "
                         "in port")
        for t, tfs in sorted(foes.items()):
            tot = collections.Counter()
            for x in tfs:
                tot.update(x["ships"])
            lines.append("ENEMY NAVY %s: %d ships (%s) in %d task forces: %s" % (
                t, sum(tot.values()), ", ".join("%d %s" % (n, k) for k, n in tot.most_common()),
                len(tfs), "; ".join(
                    "%s at %s" % (", ".join("%d %s" % (n, k) for k, n in x["ships"].most_common()),
                                  sea.label(sea.region_of[x["location"]])
                                  if x["location"] in sea.region_of else
                                  ("port, " + mv.label(mv.state_of.get(x["location"]))
                                   if x["location"] in mv.state_of else "?"))
                    for x in tfs[:8])))
    def fuel(g):
        economy.track_fuel(g, ctx["day"])
        return economy.fuel_report(g)
    stock, cap, net, left = gw.run(fuel)
    lines.append("FUEL: %d of %d%s" % (stock, cap, "" if net is None else
                 ", %+d a day%s" % (net, ", gone in %d days" % left if left else "")))
    try:
        wrong = [t for t in gw.run(navy.task_forces, None)
                 if t["mission"] in ("patrol", "convoy_escort", "convoy_raiding")
                 and any(x in k for k in t["ships"] for x in ("carrier", "battleship",
                                                               "battle_cruiser", "heavy_cruiser"))]
        if wrong:
            lines.append("CAPITAL SHIPS ON LIGHT DUTY (they burn fuel for what destroyers and "
                         "light cruisers do; put them in a strike_force or naval_invasion_support, "
                         "or hold): " + "; ".join("%s on %s (%s)" % (
                             t["name"], t["mission"], ", ".join("%s %d" % kv for kv in t["ships"].items()))
                             for t in wrong))
    except Exception:
        pass
    cv = gw.run(navy.convoys)
    lines.append("CONVOYS: %d owned, %d in use (our trade and the supply of armies overseas), "
                 "%d free%s" % (cv["owned"], cv["in_use"], cv["free"],
                                "; TOO FEW: build convoys" if cv["free"] < 0.15 * max(1, cv["owned"])
                                else ""))
    lines += gw.run(navy.carrier_lines)
    from . import projects
    ctx["projects_stuck"] = gw.run(projects.stuck, "specialization_naval")
    if ctx["projects_stuck"]:
        lines.insert(2, "ALERT, SPECIAL PROJECTS STAND STILL: " + ctx["projects_stuck"])
    lines += gw.run(projects.lines, "specialization_naval")
    from .briefing import operations_lines
    lines += operations_lines(ctx, sea)
    pic = gw.run(economy.picture, mv)
    naval = [x for x in pic["lines"] if x[4]]
    idle = pic["dockyards"] - pic["dockyards_on_lines"]
    lines.append("DOCKYARDS: %d, %d on lines%s" % (
        pic["dockyards"], pic["dockyards_on_lines"],
        # 26.10 (UK 1937): a series ended, its yard stood idle for weeks unremarked
        "; %d IDLE: a dockyard on no line builds nothing; give it to a line "
        "(set_line_dockyards) or open one (new_ship_line)" % idle if idle > 0 else ""))
    working = [x for x in naval if str(x[2]).split()[0] != "0"]
    idle = collections.defaultdict(list)
    for i, what, n, out, _ in naval:
        if str(n).split()[0] == "0":
            idle[what.split(" (")[0]].append(i)
    lines.append("SHIP LINES AT WORK (line, what, dockyards): " + ("; ".join(
        "%d %s, %s yards" % (i, what, n) for i, what, n, out, _ in working) or "none"))
    if idle:
        lines.append("SHIP LINES WAITING FOR YARDS (no dockyard on them; each holds a hull "
                     "in the queue): " + "; ".join("%s: lines %s" % (w, ",".join(map(str, ls)))
                                                  for w, ls in idle.items()))
    ships = [v for v in pic["variants"] if v[1].startswith(("ship", "convoy"))]
    lines.append("OUR SHIP DESIGNS (id, hull, design): " + "; ".join(
        "%d %s %s" % v for v in ships))
    lines += gw.run(economy.design_hints, lambda eq: eq.startswith("ship"))
    from . import doctrine_files
    lines += gw.run(doctrine_files.picture, "naval")
    return "\n".join(lines)


def check_err(r):
    if isinstance(r, dict) and r.get("error"):
        raise ops.OpError(r["error"])
    return r


class Admiralty(Agent):
    role = "navy"
    max_tokens = 3000
    reasoning = "none"
    budget_s = 120

    def __init__(self, model, llm, journal):
        Agent.__init__(self, "Admiralty", model, llm, journal,
                       NAVY_SYSTEM.format(country=COUNTRY["name"]))

    def tools(self):
        return [
            tool("fleet_regions", "Set the sea regions of a fleet's mission (ids from the "
                 "briefing); every task force of the fleet takes them.",
                 {"fleet": {"type": "string"},
                  "regions": {"type": "array", "items": {"type": "integer"}}},
                 ["fleet", "regions"]),
            tool("task_force_mission", "Set a task force's mission.",
                 {"task_force": {"type": "string"},
                  "mission": {"type": "string", "enum": navy.MISSIONS}},
                 ["task_force", "mission"]),
            tool("fleet_home_base", "Base a fleet at one of our naval bases (its province "
                 "number from OUR NAVAL BASES).",
                 {"fleet": {"type": "string"}, "province": {"type": "integer"}},
                 ["fleet", "province"]),
            tool("assign_admiral", "Put an admiral in command of a fleet.",
                 {"fleet": {"type": "string"}, "admiral": {"type": "string"}},
                 ["fleet", "admiral"]),
            tool("learn_trait", "An admiral learns a trait (TRAITS in the briefing lists "
                 "what each can learn now); costs command power.",
                 {"admiral": {"type": "string"}, "trait": {"type": "string"}},
                 ["admiral", "trait"]),
            tool("new_ship_line", "Open a dockyard line for a ship design (id from OUR "
                 "SHIP DESIGNS) with this many dockyards.",
                 {"design_id": {"type": "integer"}, "dockyards": {"type": "integer"}},
                 ["design_id", "dockyards"]),
            tool("set_line_dockyards", "Set how many dockyards work on a ship line.",
                 {"line": {"type": "integer"}, "dockyards": {"type": "integer"}},
                 ["line", "dockyards"]),
            tool("set_line_ships", "How many ships a line still builds (ships=1 closes the "
                 "series after the ship on the slip), or endless.",
                 {"line": {"type": "integer"}, "ships": {"type": "integer"},
                  "endless": {"type": "boolean"}}, ["line"]),
            tool("new_design", "Create a ship design on a hull from a PRESET of the game "
                 "(DESIGN PRESETS). Costs navy experience; then open a line for it.",
                 {"frame_id": {"type": "integer"}, "preset": {"type": "string"},
                  "name": {"type": "string"}}, ["frame_id", "preset", "name"]),
            tool("hq_admiral", "Put an admiral in a naval headquarters (OUR NAVAL "
                 "HEADQUARTERS); the one there before leaves it.",
                 {"hq": {"type": "integer"}, "admiral": {"type": "string"}},
                 ["hq", "admiral"]),
            tool("engagement", "How readily a task force fights: do_not_engage, low_risk, "
                 "medium_risk, high_risk, always.",
                 {"task_force": {"type": "string"},
                  "level": {"type": "string", "enum": navy.ENGAGEMENT}},
                 ["task_force", "level"]),
            tool("repair_priority", "When a task force goes home for repairs: never, low "
                 "(only severe damage), medium, high (already at light damage).",
                 {"task_force": {"type": "string"},
                  "priority": {"type": "string", "enum": navy.REPAIR}},
                 ["task_force", "priority"]),
            tool("naval_zone", "Give a fleet (with an admiral) these sea regions as its zone; "
                 "they leave any other zone. Its commander fights them from his next turn. "
                 "An empty list takes its zone away (you command it directly then).",
                 {"fleet": {"type": "string"},
                  "regions": {"type": "array", "items": {"type": "integer"}}},
                 ["fleet", "regions"]),
            tool("task_forces_to_fleet", "Move whole task forces into another fleet (new "
                 "ships come out in the reserve fleet: give them to the fleets that need them).",
                 {"task_forces": {"type": "array", "items": {"type": "string"}},
                  "fleet": {"type": "string"}}, ["task_forces", "fleet"]),
            tool("ships_to_task_force", "Send ships from one task force to join another (they "
                 "sail over and merge): all, or of one type (destroyer, submarine, "
                 "light_cruiser, heavy_cruiser, battleship, battle_cruiser, carrier), up to "
                 "a count.", {"from": {"type": "string"}, "to": {"type": "string"},
                              "ship_type": {"type": "string"}, "count": {"type": "integer"}},
                 ["from", "to"]),
            tool("split_task_force", "Split a task force into groups of `size` ships, each a "
                 "new task force in the same fleet (the old one keeps the rest); of one "
                 "ship_type (submarine, destroyer...) or any. The new groups stand on hold: "
                 "give each its mission and engagement.",
                 {"task_force": {"type": "string"}, "size": {"type": "integer"},
                  "ship_type": {"type": "string"}, "groups": {"type": "integer"}},
                 ["task_force", "size"]),
            tool("auto_reinforce", "Turn automatic reinforcement on or off for task forces: "
                 "new ships of their kinds join them from the reserve on their own.",
                 {"task_forces": {"type": "array", "items": {"type": "string"}},
                  "on": {"type": "boolean"}}, ["task_forces", "on"]),
            tool("wing_to_carrier", "Put one of our carrier wings (CARRIER WINGS ON LAND, or "
                 "from another carrier) on a carrier (its number).",
                 {"wing": {"type": "integer"}, "carrier": {"type": "integer"}},
                 ["wing", "carrier"]),
            tool("order_carrier_planes", "Ask for carrier planes to be made (they come from "
                 "military factories, not the yards): which types, how many factories, why. "
                 "One order per turn.", {"order": {"type": "string"}}, ["order"]),
            tool("request_naval_building", "Ask for a naval building to be built (civilian "
                 "factories build it, not the yards): naval_base, naval_supply_hub (needs a "
                 "base of level 8), naval_headquarters (needs a base of level 8; each one "
                 "costs more); where (state) and why. One request per turn.",
                 {"building": {"type": "string", "enum": ["naval_base", "naval_supply_hub",
                                                          "naval_headquarters"]},
                  "state": {"type": "string"}, "why": {"type": "string"}},
                 ["building", "state", "why"]),
            tool("start_special_project", "Start a naval special project (SPECIAL "
                 "PROJECTS) at one of our facilities (by its id); a project without a "
                 "scientist does not advance.",
                 {"facility": {"type": "integer"}, "project": {"type": "string"}},
                 ["facility", "project"]),
            tool("assign_scientist", "Put a scientist (SCIENTISTS) on a facility's project.",
                 {"facility": {"type": "integer"}, "scientist": {"type": "string"}},
                 ["facility", "scientist"]),
            tool("adopt_doctrine", "Take a naval doctrine (DOCTRINES): a subdoctrine for "
                 "an EMPTY track, or another grand doctrine; by its name. Costs navy "
                 "experience.", {"name": {"type": "string"}}, ["name"]),
            tool("end_turn", "Finish this turn.", {"summary": {"type": "string"}},
                 ["summary"]),
        ]

    def _tf(self, g, name):
        for t in navy.task_forces(g):
            if (t["name"] or "").lower() == str(name).lower() and navy.OURS(t["fleet"]):
                return t
        return None

    def before_end(self, ctx):
        """27.10: the war seas stood in the briefing and the Admiralty moved destroyers at
        home while the Italian fleet sailed the central Mediterranean unopposed."""
        st = ctx.get("projects_stuck")
        stuck = None
        if st and type(self) is Admiralty and not \
                self.this_turn & {"called:start_special_project", "called:assign_scientist"}:
            stuck = "Not yet: the naval special projects stand still: %s." % st
        opn = ctx.get("navy_open_seas")
        if not opn:
            return stuck
        if self.this_turn & {"called:" + x for x in (
                "fleet_regions", "fleet_home_base", "task_forces_to_fleet", "task_force_mission",
                "naval_zone", "assign_admiral")}:
            return stuck
        return ("Not yet, this stands unanswered: " + "; ".join(opn) + ". A sea at war in no "
                "zone or out of its fleet's reach: naval_zone it to a fleet based near it, or "
                "make one there (task_forces_to_fleet into a numbered fleet, assign_admiral, "
                "fleet_home_base: the western Mediterranean from Gibraltar, not Alexandria). "
                "Spare capital ships go where the enemy's are (task_forces_to_fleet). Or call "
                "end_turn again and say why not.")

    def handle(self, ctx, fn, a):
        self.this_turn.add("called:" + fn)
        gw = ctx["gw"]
        sea = ctx.get("sea") or navy.SeaRegions(ctx["mv"])
        from . import fleets
        if type(self) is Admiralty and fn in ("fleet_regions", "task_force_mission",
                                              "engagement", "repair_priority",
                                              "ships_to_task_force"):
            fl = a.get("fleet") if fn == "fleet_regions" else gw.run(
                lambda g: (self._tf(g, a.get("task_force") or a.get("from")) or {}).get("fleet"))
            if fl in fleets.ZONES:
                raise ops.OpError(
                    "%s is under its own commander (zone: %s): he orders its regions, missions "
                    "and task forces. Yours: task_forces_to_fleet to move a task force out or "
                    "in, naval_zone to redraw the zone, fleet_home_base, assign_admiral; or "
                    "tell him in end_turn" % (fl, ", ".join(sea.label(r) for r in fleets.ZONES[fl])))
        if fn == "naval_zone":
            return check_err(gw.run(fleets.set_zone, a["fleet"], a.get("regions") or [], sea))

        def check(r):
            if isinstance(r, dict) and r.get("error"):
                raise ops.OpError(r["error"])
            return r
        if fn == "fleet_regions":
            return gw.run(navy.set_fleet_regions, a["fleet"], a["regions"], sea)
        if fn == "task_force_mission":
            def go(g):
                t = self._tf(g, a["task_force"])
                if t is None:
                    return {"error": "no task force named %s" % a["task_force"]}
                # the user, 24.10: at war nothing that fights drills; only ships new since
                # the war began, and only while no mission needs them
                if a["mission"] == "training" and ctx["enemies"] and \
                        t["name"] in (ctx.get("veterans") or {}).get("tf", ()):
                    return {"error": "%s does not train at war: it stood in the fleet when the "
                                     "war began. Exercises at war are for ships new since, and "
                                     "only while no mission needs them. Give it a mission (a "
                                     "strike force, escort, support of a landing) or hold it "
                                     "in port" % t["name"]}
                # capital ships do not patrol (the user, 23.10: the carriers were sent to
                # patrol against a China with no navy left and burnt the fuel)
                big = [k for k in t["ships"] if any(x in k.lower() for x in (
                    "carrier", "battleship", "battle_cruiser", "battlecruiser",
                    "heavy_cruiser", "super_heavy"))]
                if big and a["mission"] in ("patrol", "convoy_escort", "convoy_raiding",
                                            "mines_planting", "mines_sweeping"):
                    # what to do instead, by name (26.10: the Mediterranean Fleet was sent to
                    # patrol twice, refused twice, and left on hold at Alexandria)
                    tfs = navy.task_forces(g)
                    light = lambda x: x["ships"] and all(k in SCREENS for k in x["ships"])
                    here = [x["name"] for x in tfs if x["fleet"] == t["fleet"] and light(x)
                            and x["name"] != t["name"]]
                    idle = ["%s (%s, %s)" % (x["name"], x["fleet"], ", ".join(
                        "%d %s" % (n, k) for k, n in x["ships"].most_common()))
                            for x in tfs if x["fleet"] != t["fleet"] and light(x)
                            and x["mission"] in ("hold", "reserve_fleet")]
                    caps = sum(n for k, n in t["ships"].items() if k in CAPITALS)
                    spare = sum(n for k, n in t["ships"].items() if k in SCREENS) - \
                        SCREENS_PER_CAPITAL * caps
                    return {"error": "%s has capital ships (%s): they do not %s. Do this "
                                     "instead: (1) task_force_mission %s strike_force: it fights "
                                     "what a patrol finds and burns fuel only then. (2) For the "
                                     "%s, a task force of destroyers and light cruisers only: %s; "
                                     "then fill it with %s's spare screens (ships_to_task_force "
                                     "from %s, ship_type destroyer or light_cruiser, count; it "
                                     "has %d beyond 3 per capital ship)" % (
                                         t["name"], ", ".join(big), a["mission"], t["name"],
                                         a["mission"],
                                         ("this fleet has " + ", ".join(here)) if here else
                                         "this fleet has none, so bring one in with "
                                         "task_forces_to_fleet (to fleet %s): %s" % (
                                             t["fleet"], "; ".join(idle[:6]) or
                                             "no idle one anywhere: take one from a convoy fleet"),
                                         t["name"], t["name"], max(0, spare))}
                if a["mission"] not in ("hold", "reserve_fleet", "training") and \
                        not navy.configured_regions(g, t["fleet_ptr"]):
                    return {"error": "fleet %s has no mission regions: set them first "
                                     "(fleet_regions)" % t["fleet"]}
                return navy.set_task_force_mission(g, t["ptr"], a["mission"])
            return check(gw.run(go))
        if fn == "fleet_home_base":
            return check(gw.run(navy.set_home_base, a["fleet"], a["province"]))
        if fn == "assign_admiral":
            return check(gw.run(navy.assign_admiral, a["fleet"], a["admiral"]))
        if fn == "learn_trait":
            from . import leaders
            return check(gw.run(leaders.learn, a["admiral"], a["trait"]))
        if fn == "new_ship_line":
            ids = {v[0] for v in gw.run(economy.picture, ctx["mv"])["variants"]
                   if v[1].startswith(("ship", "convoy"))}
            if int(a["design_id"]) not in ids:
                raise ops.OpError("design %s is not one of OUR SHIP DESIGNS" % a["design_id"])
            return gw.run(economy.new_line, int(a["design_id"]), int(a["dockyards"]))
        if fn in ("set_line_dockyards", "set_line_ships"):
            lines = gw.run(economy.picture, ctx["mv"])["lines"]
            if not any(x[0] == int(a["line"]) and x[4] for x in lines):
                raise ops.OpError("line %s is not a ship line" % a["line"])
            if fn == "set_line_dockyards":
                return gw.run(economy.set_line, int(a["line"]), int(a["dockyards"]))
            key = ("ships", int(a["line"]))
            if key in self.this_turn:
                raise Nudge("this line's series was already set this turn")
            self.this_turn.add(key)
            if a.get("endless"):
                return gw.run(economy.set_ships, a["line"], None)
            if not a.get("ships") or int(a["ships"]) < 1:
                raise ops.OpError("give ships (1 or more) or endless=true")
            return gw.run(economy.set_ships, a["line"], int(a["ships"]))
        if fn == "new_design":
            return gw.run(economy.new_design, None, a["frame_id"], a["name"], a.get("preset"))
        if fn == "engagement":
            return check(gw.run(navy.set_engagement, a["task_force"], a["level"]))
        if fn == "repair_priority":
            return check(gw.run(navy.set_repair, a["task_force"], a["priority"]))
        if fn == "task_forces_to_fleet":
            return check(gw.run(navy.task_forces_to_fleet, a["task_forces"], a["fleet"]))
        if fn == "ships_to_task_force":
            return check(gw.run(navy.ships_to_task_force, a["from"], a["to"],
                                a.get("ship_type"), a.get("count")))
        if fn == "split_task_force":
            return check(gw.run(navy.split_task_force, a["task_force"], a["size"],
                                a.get("ship_type"), a.get("groups")))
        if fn == "auto_reinforce":
            return check(gw.run(navy.auto_reinforce, a["task_forces"], a.get("on", True)))
        if fn == "wing_to_carrier":
            return check(gw.run(navy.wing_to_carrier, a["wing"], a["carrier"]))
        if fn == "order_carrier_planes":
            if "order_carrier_planes" in self.this_turn:
                raise Nudge("one order for carrier planes per turn; this one was not sent")
            self.this_turn.add("order_carrier_planes")
            if ctx.get("industry"):
                return deliver(ctx, "Admiralty", "industry", "CARRIER PLANES: " + a["order"])
            ctx.setdefault("player_requests", []).append(a["order"])
            return {"sent_to": "the player, who runs the factories",
                    "note": "say it in your summary as well"}
        if fn == "request_naval_building":
            if "request_naval_building" in self.this_turn:
                raise Nudge("one building request per turn; this one was not sent")
            self.this_turn.add("request_naval_building")
            text = "BUILD %s in %s: %s" % (a["building"], a["state"], a["why"])
            if ctx.get("industry"):
                return deliver(ctx, "Admiralty", "industry", text)
            ctx.setdefault("player_requests", []).append(text)
            return {"sent_to": "the player, who runs construction"}
        if fn == "hq_admiral":
            return check(gw.run(navy.assign_to_hq, a["hq"], a["admiral"]))
        if fn in ("start_special_project", "assign_scientist"):
            from . import projects
            return check(projects.handle(gw, fn, a, "specialization_naval"))
        if fn == "adopt_doctrine":
            from . import doctrine_files
            r = gw.run(doctrine_files.adopt, "naval", a["name"])
            if not r.get("ok"):
                raise ops.OpError(r.get("error"))
            return r
        raise ops.OpError("unknown tool %s" % fn)
