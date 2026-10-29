"""Submarine raiding, convoy escort groups, and admirals fitted to their fleets.

27.10, the user: the submarines raid in a few big flotillas; split them into groups of
5-8 and send them along every route of the Reich and Italy, under a raider admiral; form
the destroyer escorts the same way. Why small groups (common/defines, 1.19.3): the side
with more ships loses 25% positioning per +100% ship ratio from 4 ships up, to 75%
(HIGHER_SHIP_RATIO_POSITIONING_PENALTY_FACTOR, MIN_SHIPS_FOR_HIGHER_SHIP_RATIO_PENALTY),
and each task force searches on its own: more groups watch more sea. The AI's own optimum
is 16 subs (SUB_TASKFORCE_MAX_SHIP_COUNT); the user's number is smaller, and it is his.
"""
import collections

from .admiralty import coast_regions, reachable

SUB_GROUP = (5, 8)
ESCORT_GROUP = (5, 8)

# admiral traits by role (common/unit_leader/00_traits.txt, 1.19.3)
ROLES = {
    "raider": ("seawolf", "submarine_officer", "silent_hunter", "concealment_expert",
               "lone_wolf", "torpedo_expert", "loading_drill_master"),
    "escort": ("hunter_killer", "fleet_protector", "blockade_runner", "destroyer_officer",
               "search_pattern_expert", "spotter"),
    "battle": ("superior_tactician", "ironside", "big_guns_expert", "heavy_ship_officer",
               "cruiser_officer", "gunnery_expert", "air_controller", "carrier_officer",
               "marksman", "crisis_magician", "fly_swatter"),
}
WHAT = {"seawolf": "submarine attack +20%", "hunter_killer": "finds and hits submarines +20%",
        "fleet_protector": "screening +20%", "blockade_runner": "convoys escape faster",
        "silent_hunter": "subs reveal themselves less", "superior_tactician": "positioning +25%",
        "ironside": "capital ship defence +10%", "spotter": "spotting +10%"}


def _base(t):
    """trait_destroyer_officer_xp_gain_factor: on the way to destroyer_officer."""
    if t.startswith("trait_") and t.endswith("_xp_gain_factor"):
        return t[len("trait_"):-len("_xp_gain_factor")]
    return t


def role_of(traits, growing=True):
    traits = [_base(t) for t in traits] if growing else traits
    score = {r: sum(t in ts for t in traits) for r, ts in ROLES.items()}
    best = max(score.values())
    return [r for r, v in score.items() if v == best] if best else []


def kind(t):
    """A task force's kind by its ships: submarines, escort (screens only), battle, mixed."""
    s = t["ships"]
    subs = s.get("submarine", 0)
    scr = sum(s.get(k, 0) for k in ("destroyer", "light_cruiser"))
    other = sum(s.values()) - subs - scr
    if subs and not (scr or other):
        return "submarines"
    if scr and not (subs or other):
        return "screens"
    if subs:
        return "mixed"
    return "battle"


def admiral_lines(g, fp):
    """Each admiral with his traits and the role they fit; mismatches named."""
    from . import leaders
    try:
        rows = leaders.leaders(g, (2,))
    except Exception:
        return []
    traits = {r["name"]: [t for t in r["traits"] if t != "?"] for r in rows}
    fleet_of = {f["admiral"]: f for f in fp if f["admiral"]}
    out, free = [], collections.defaultdict(list)
    for name, ts in sorted(traits.items()):
        roles = role_of(ts)
        f = fleet_of.get(name)
        out.append("%s: %s%s" % (name, ", ".join(
            "%s (%s)" % (t, WHAT[t]) if t in WHAT else
            "growing into %s" % _base(t) if _base(t) != t else t for t in ts) or "no traits",
            "; fits %s" % "/".join(roles) if roles else ""))
        if not f:                       # only a trait he has, not one he grows into
            for r in role_of(ts, growing=False):
                free[r].append(name)
    if not any("raider" in role_of(ts) for ts in traits.values()):
        out.append("NO RAIDER ADMIRAL: a submarine fleet's admiral grows into "
                   "submarine_officer by commanding it; learn_trait seawolf when it opens")
    lines = ["ADMIRALS AND THEIR TRAITS (raider: seawolf, submarine_officer, silent_hunter, "
             "concealment_expert, lone_wolf; escort and anti-submarine: hunter_killer, "
             "fleet_protector, blockade_runner, destroyer_officer; battle: superior_tactician, "
             "ironside, big_guns_expert, heavy_ship_officer): " + "; ".join(out)]
    wrong = []
    for f in fp:
        if not f["task_forces"]:
            continue
        kinds = collections.Counter()
        for t in f["task_forces"]:
            kinds[kind(t)] += sum(t["ships"].values())
        main = kinds.most_common(1)[0][0]
        want = {"submarines": "raider", "screens": "escort", "battle": "battle"}.get(main)
        if not want:
            continue
        have = role_of(traits.get(f["admiral"], [])) if f["admiral"] else []
        if want not in have and free.get(want):
            wrong.append("%s (mostly %s) has %s; free %s admirals: %s" % (
                f["name"], main, f["admiral"] or "no admiral", want, ", ".join(free[want][:3])))
    if wrong:
        lines.append("ADMIRALS OUT OF PLACE (assign_admiral the fitting one): " + "; ".join(wrong))
    return lines


def raiding_lines(fp, mv, sea, holders, own, enemies, our_ports):
    """Our submarine groups, the enemy's convoy seas our submarines reach, and our escort
    groups, with the group sizes the user set."""
    if not enemies:
        return []
    reach, closed = reachable(mv, sea, holders, own, enemies, our_ports, "submarine")
    seas = collections.Counter()
    for q, lvl in mv.ports.items():
        if holders.get(mv.state_of.get(q), (None, None))[1] in enemies:
            for r in coast_regions(mv, sea, [q]):
                if r in reach:
                    seas[r] += max(1, lvl)
    groups, big, mixed, escorts = [], [], [], []
    raided = collections.Counter()
    for f in fp:
        for t in f["task_forces"]:
            k, n = kind(t), sum(t["ships"].values())
            if k == "submarines":
                groups.append((f["name"], t["name"], n, t["mission"]))
                if n > SUB_GROUP[1]:
                    big.append("%s %d" % (t["name"], n))
                if t["mission"] == "convoy_raiding":
                    raided.update(t["regions"])
            elif k == "mixed":
                mixed.append("%s (%s, %d submarines)" % (t["name"], f["name"],
                                                         t["ships"]["submarine"]))
            if t["mission"] == "convoy_escort":
                escorts.append("%s %d" % (t["name"], n))
    subs = sum(n for _, _, n, _ in groups) + sum(
        t["ships"].get("submarine", 0) for f in fp for t in f["task_forces"] if kind(t) == "mixed")
    out = ["SUBMARINE WAR: %d submarines; groups (task force, boats, mission): %s. A raiding "
           "group is %d-%d boats: a bigger one finds one convoy at a time and fights at the "
           "large-fleet penalty, more groups watch more sea; one group per enemy convoy sea, "
           "under one raider fleet with a raider admiral, mission convoy_raiding, on low_risk."
           % (subs, "; ".join("%s %d %s" % (t, n, m) for _, t, n, m in groups) or "none",
              SUB_GROUP[0], SUB_GROUP[1])]
    if big:
        out.append("  GROUPS TOO BIG (more than %d): %s. Split them (split_task_force, "
                   "size 6, ship_type submarine)" % (SUB_GROUP[1],
                                                                          ", ".join(big)))
    if mixed:
        out.append("  SUBMARINES IN SURFACE TASK FORCES (they sail at the surface ships' "
                   "pace and are found with them): %s; send them to a submarine group"
                   % "; ".join(mixed[:8]))
    if seas:
        out.append("  ENEMY CONVOY SEAS OUR SUBMARINES REACH (his ports on them, weight by port "
                   "level): %s%s" % ("; ".join(
                       "%s %d%s" % (sea.label(r), w, ", raided" if raided[r] else "")
                       for r, w in seas.most_common(12)),
                       "; closed even to submarines: %s" % ", ".join(sorted(closed))
                       if closed else ""))
    big_e = [e for e in escorts if int(e.rsplit(" ", 1)[1]) > ESCORT_GROUP[1] + 2]
    out.append("ESCORT GROUPS (convoy_escort; %d-%d destroyers with a light cruiser or two, one "
               "per sea our convoys and troops cross, under an escort admiral): %s%s" % (
                   ESCORT_GROUP[0], ESCORT_GROUP[1], ", ".join(escorts) or "none",
                   "; TOO BIG: %s" % ", ".join(big_e) if big_e else ""))
    out.append("  split_task_force cuts a big group or flotilla into groups of a size; "
               "ships_to_task_force moves ships between groups.")
    return out
