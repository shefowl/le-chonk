"""The navy: fleets, task forces, ships and the commands the admiralty gives them.

Layouts found live on 22.10 (Japan, 1936.01.01), see docs/04-navy.md.

A FLEET (CFleet, country array +0x278) is the admiral's command and has a home base.
A TASK FORCE (CTaskForce, handle type 51) is what sails: it carries the mission and the
sea regions of that mission. A ship (CShip) belongs to one task force.

The handle table gives a task force as a pointer 0x10 into the object; offsets below are
from that pointer (TF_*), and commands that take the object want ptr - 0x10.
"""
import collections
import struct
import time

from .bridge import module as B

# --- layouts (handle-table pointer of a task force) ---
TF_NAME = 0x248        # CString
TF_LOCATION = 0x1E0    # CProvince*
TF_FLEET = 0x330       # CFleet*
TF_SHIPS = 0x338       # CPdxArray<CShip*>
TF_MISSION = 0x364     # int, MISSIONS index
FL_COUNTRY = 0xA0      # CCountry*
FL_HOME = 0xD0         # CProvince* of the home base
FL_NAME = 0xD8
FL_TASK_FORCES = 0xB0  # CPdxArray of its task forces (each entry is the object - 0x10)
SH_TYPE = 0x90         # sub-unit key: light_cruiser, destroyer, submarine...
PROV_ID = 0xA4

# NNavy mission order (defines MISSION_DOMINANCE_RATIOS)
MISSIONS = ["hold", "patrol", "strike_force", "convoy_raiding", "convoy_escort",
            "mines_planting", "mines_sweeping", "training", "reserve_fleet",
            "naval_invasion_support"]

SET_TYPE_CTOR = 0x2253A90      # (this, task force object, mission) size 0x48
SET_REGIONS_CTOR = 0x2254A40   # (this, task force ptr, CPdxArray<int region>&) size 0x48


class SeaRegions:
    """Strategic regions that are sea, from the game files."""

    def __init__(self, mv):
        from . import air
        regions = getattr(mv, "air_regions", None) or air.Regions(mv.dir, mv.state_of)
        self.name = {}
        self.provinces = {}
        # a region with any sea province is a sea (land regions hold lakes, never sea);
        # 27.10: "more than half sea" dropped the Tyrrhenian (Sardinia, Corsica), the
        # Aegean, the Caribbean, the Java, Solomon and East China seas
        seas = getattr(mv, "sea", None)
        for rid, provs in regions.provinces.items():
            sea = [q for q in provs if (q in seas if seas is not None else q not in mv.land)]
            if sea and (seas is not None or len(sea) * 2 > len(provs)):
                self.name[rid] = regions.name[rid]
                self.provinces[rid] = provs
        self.region_of = {q: r for r, ps in self.provinces.items() for q in ps}

    def label(self, rid):
        return "%s (#%d)" % (self.name.get(rid, "?"), rid)


def _prov_id(g, ptr):
    return g.p.i32(ptr + PROV_ID) if ptr else None


def task_forces(g, tag=None):
    """[{ptr, name, fleet, mission, location, ships: Counter(type)}] of a country."""
    p = g.p
    fleets = {int(f["ptr"], 16): f["name"] for f in g.fleets(tag)}
    out = []
    listed = set(B("armyops").handle_objects(g, 51).values())
    # a fleet's own list of its task forces (CFleet+0xB0, entries point 0x10 before the
    # object): 26.10, the reserve task forces new ships are deployed into ("United Kingdom
    # Reserve fleet 3", 22 ships) are not in the handle table, so the admiralty never saw them
    held = set()
    for fp in fleets:
        try:
            held |= {a + 0x10 for a in B("game")._arr(p, fp, FL_TASK_FORCES)}
        except Exception:
            continue
    for o in listed | held:
        try:
            fl = p.u64(o + TF_FLEET)
            if fl not in fleets or g.oi.class_of(o) != "CTaskForce":
                continue
            ships = B("game")._arr(p, o, TF_SHIPS)
            m = p.i32(o + TF_MISSION)
            out.append({
                "ptr": o, "name": B("game").item_name(p, o + TF_NAME), "fleet": fleets[fl],
                "fleet_ptr": fl, "mission": MISSIONS[m] if 0 <= m < len(MISSIONS) else m,
                # a task force made by a split is not in the handle table either (27.10)
                "location": _prov_id(g, p.u64(o + TF_LOCATION)),
                "reserve": o not in listed and o not in CREATED and
                "reserve" in (B("game").item_name(p, o + TF_NAME) or "").lower(),
                "ships": collections.Counter(B("game").item_name(p, s + SH_TYPE)
                                             for s in ships)})
        except Exception:
            continue
    return out


def enemy_fleets(g, tags):
    """{tag: [{name, location, ships: Counter(type)}]} of the task forces of these
    countries, in one pass (25.10, n3: the admiralty never saw the Chinese navy and sent a
    destroyer patrol alone into the Yellow Sea against a stronger squadron)."""
    p = g.p
    owner = {}
    for t in tags:
        try:
            for f in g.fleets(t):
                owner[int(f["ptr"], 16)] = t
        except Exception:
            continue
    out = collections.defaultdict(list)
    if not owner:
        return out
    # all task forces, not the handle table (it holds only ours)
    for o in g.oi.instances("CTaskForce"):
        try:
            fl = p.u64(o + TF_FLEET)
            if fl not in owner:
                continue
            ships = B("game")._arr(p, o, TF_SHIPS)
            if not ships:
                continue
            out[owner[fl]].append({
                "name": B("game").item_name(p, o + TF_NAME),
                "location": _prov_id(g, p.u64(o + TF_LOCATION)),
                "ships": collections.Counter(B("game").item_name(p, s + SH_TYPE)
                                             for s in ships)})
        except Exception:
            continue
    return out


def fleets(g, tag=None):
    p = g.p
    out = []
    for f in g.fleets(tag):
        fp = int(f["ptr"], 16)
        out.append({"ptr": fp, "name": f["name"],
                    "home": _prov_id(g, p.u64(fp + FL_HOME))})
    return out


# ------------------------------------------------------------------ commands
def _int_array(g, vals):
    p = g.p
    data = g.malloc(max(1, len(vals)) * 4)
    if vals:
        p.write(data, struct.pack("<%di" % len(vals), *vals))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, len(vals), len(vals),
                             p.base + B("armyops").ALLOC))
    return hdr


# set_mission is gone (27.10): it passed a task force to SET_REGIONS_CTOR, which takes
# a fleet (set_fleet_regions), and the game crashed in the command's execute at
# +0x213b51e. Regions belong to a fleet; a task force takes only its mission.


# ------------------------------------------------------------------ more layouts
TF_REGIONS = 0x428 - 0x10   # CPdxArray<CStrategicRegion*> of the fleet's mission
FL_REGIONS = 0x40          # CFleet configured regions, execute 0x2254DC0 -> 0x2001D00
SR_ID = 0x48                # CStrategicRegion id
SH_HP = 0x6E0               # current strength (fixed 1e5); max at SH_HP_MAX
SH_HP_MAX = 0x300
SH_ORG = 0x6E8              # current organisation; max at SH_ORG_MAX
SH_ORG_MAX = 0x2F8
SH_XP = 0x6F0               # the ship's experience
# (22.10: found by putting submarines on training for five months; accidents took three
#  from 15 to 10.25 and 1.5 strength, training took their organisation from 30 to 6)
FL_ADMIRAL = 0xA8           # {4713, id} handle of the fleet's admiral, 0 if none
AL_RANK = 0xD7C             # 2 = admiral
AL_FLEET = 0xE60            # {61, id} handle of the admiral's fleet
AL_NAVAL_SKILLS = (0xE68, 0xE78, 0xE88, 0xE98)   # attack, defence, manoeuvre, coordination

HOME_CTOR = 0x225E2F0       # (this, CFleet*, CNavalBase*, 1) size 0x38
NAVAL_BASE_OF = 0xDA9290    # (CProvince*) -> CNavalBase*
LEADER_CTOR = 0x2333370     # (this, CArmyLeader*, CFleet*) size 0x38


def mission_regions(g, tf_ptr):
    """Effective task-force regions, filtered by the game from the fleet selection."""
    p = g.p
    try:
        return [p.i32(r + SR_ID) for r in B("game")._arr(p, tf_ptr, TF_REGIONS)]
    except Exception:
        return []


def configured_regions(g, fleet_ptr):
    """The actual selected fleet regions, before task-force range/filter checks.

    1.19.4: 0x2001D00 reads CFleet+40/count+4C and CStrategicRegion+48.
    0x2278FD0 separately builds the filtered TF+418 array. An empty latter
    does not mean the fleet has no region selected.
    """
    return [g.p.i32(r + SR_ID) for r in B("game")._arr(g.p, fleet_ptr, FL_REGIONS)]


def org_share(g, ships, cur_off=SH_ORG, max_off=SH_ORG_MAX):
    p = g.p
    cur = mx = 0
    for s in ships:
        try:
            cur += max(0, min(p.i32(s + cur_off), p.i32(s + max_off)))
            mx += max(0, p.i32(s + max_off))
        except Exception:
            pass
    return cur / mx if mx else None


def damaged(g, ships, below=0.7):
    """[(type, strength share)] of the ships under this share of their strength."""
    p = g.p
    out = []
    for s in ships:
        mx = p.i32(s + SH_HP_MAX)
        if mx > 0 and p.i32(s + SH_HP) < below * mx:
            out.append((B("game").item_name(p, s + SH_TYPE), p.i32(s + SH_HP) / mx))
    return out


def admirals(g, tag=None):
    """[{name, key, ptr, skills (att, def, man, coord), fleet handle id or None}]"""
    p = g.p
    out = []
    for ch in g.all_characters(tag):
        lp = p.u64(ch + g.CH_ARMY_LEADER)
        if not lp or p.i32(lp + AL_RANK) != 2:
            continue
        ft, fid = struct.unpack("<ii", p.read(lp + AL_FLEET, 8))
        out.append({"name": B("game").item_name(p, ch + B("game").CH_NAME) or "?",
                    "key": B("game").item_name(p, ch + B("game").CH_KEY),
                    "ptr": lp, "skills": tuple(p.i32(lp + o) for o in AL_NAVAL_SKILLS),
                    "fleet": fid if ft == 61 else None,
                    "hq": struct.unpack("<ii", p.read(lp + 0xEA8, 8))[1] or None})
    return out


def OURS(name):
    """Whether a fleet is the admiralty's (roster.json, set by the run; 23.10)."""
    return True


def fleet_picture(g, tag=None):
    """Fleets with their admiral, home base and task forces, for the briefing."""
    p = g.p
    adm = {a["ptr"]: a for a in admirals(g, tag)}
    by_handle = {}
    for a in adm.values():
        try:
            by_handle[struct.unpack("<ii", p.read(a["ptr"] + 8, 8))] = a
        except Exception:
            pass
    tfs = task_forces(g, tag)
    out = []
    for f in fleets(g, tag):
        if not OURS(f["name"]):
            continue
        h = struct.unpack("<ii", p.read(f["ptr"] + FL_ADMIRAL, 8))
        a = by_handle.get(h)
        mine = [t for t in tfs if t["fleet_ptr"] == f["ptr"]]
        for t in mine:
            ships = B("game")._arr(p, t["ptr"], TF_SHIPS)
            t["org"] = org_share(g, ships)
            t["strength"] = org_share(g, ships, SH_HP, SH_HP_MAX)
            t["damaged"] = damaged(g, ships)
            t["regions"] = mission_regions(g, t["ptr"])
        out.append(dict(f, admiral=a["name"] if a else None,
                        regions=configured_regions(g, f["ptr"]),
                        admiral_skills=a["skills"] if a else None, task_forces=mine))
    return out


def _find_fleet(g, name):
    for f in g.fleets():
        if f["name"].lower() == str(name).lower() and OURS(f["name"]):
            return int(f["ptr"], 16)
    return None


def _post(g, ctor, size, args, check):
    api = B("api")
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        api.allow_call(ctor)
        cmd = g.malloc(size)
        p.write(cmd, b"\0" * size)
        g.call(ctor, [cmd] + list(args))
        why = check(cmd)
        if why:
            return why
        if not B("industry").can_execute(g, cmd):
            return "the game does not allow it (CanExecute false)"
        g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    time.sleep(0.5)
    return None


def set_fleet_regions(g, fleet_name, regions, sea):
    """The sea regions of a fleet's mission (every task force of the fleet takes them)."""
    fp = _find_fleet(g, fleet_name)
    if fp is None:
        return {"error": "no fleet named %s" % fleet_name}
    regions = [int(r) for r in regions]
    bad = [r for r in regions if r not in sea.name]
    if bad or not regions:
        return {"error": "not sea regions: %s" % (bad or "none given")}
    p = g.p
    why = _post(g, SET_REGIONS_CTOR, 0x48, [fp, _int_array(g, regions)],
                lambda c: None if p.i32(c + 0x3C) == len(regions) and
                struct.unpack("<ii", p.read(c + 0x24, 8)) ==
                struct.unpack("<ii", p.read(fp + 8, 8)) else "regions command not filled")
    if why:
        return {"ok": False, "confirmed": False, "error": why}
    current = _find_fleet(g, fleet_name)
    actual = configured_regions(g, current) if current is not None else []
    confirmed = set(actual) == set(regions)
    out = {"ok": confirmed, "confirmed": confirmed, "fleet": fleet_name,
           "regions": actual, "requested_regions": regions}
    if not confirmed:
        out["error"] = "fleet region selection not confirmed after post; inspect before retrying"
    return out


def set_task_force_mission(g, tf_ptr, mission):
    if mission not in MISSIONS:
        return {"error": "unknown mission %s; one of %s" % (mission, ", ".join(MISSIONS))}
    p = g.p
    m = MISSIONS.index(mission)
    why = _post(g, SET_TYPE_CTOR, 0x48, [tf_ptr - 0x10, m],
                lambda c: None if p.i32(c + 0x34) == 1 and p.i32(c + 0x40) == m
                else "mission command not filled")
    if why:
        return {"error": why}
    now = p.i32(tf_ptr + TF_MISSION)
    actual = MISSIONS[now] if 0 <= now < len(MISSIONS) else now
    if now != m:
        return {"error": "mission %s was not confirmed after posting: still %s"
                         % (mission, actual), "mission": actual}
    return {"mission": actual}


def set_home_base(g, fleet_name, province):
    """Move a fleet's home base to our naval base in this province."""
    fp = _find_fleet(g, fleet_name)
    if fp is None:
        return {"error": "no fleet named %s" % fleet_name}
    p = g.p
    pr = B("mapdata").province_objects(g).get(int(province))
    if not pr:
        return {"error": "no province %s" % province}
    api = B("api")
    own = g._inj is None
    if own:
        g.attach()
    try:
        api.allow_call(NAVAL_BASE_OF)
        base = g.call(NAVAL_BASE_OF, [pr])
    finally:
        if own:
            g.detach()
    if not base or g.oi.class_of(base) != "CNavalBase":
        return {"error": "province %s has no naval base" % province}
    why = _post(g, HOME_CTOR, 0x38, [fp, base, 1],
                lambda c: None if p.i32(c + 0x2C) == int(province) else
                "home base command not filled")
    if why:
        return {"error": why}
    return {"fleet": fleet_name, "home": _prov_id(g, p.u64(fp + FL_HOME))}


def assign_admiral(g, fleet_name, admiral_name):
    fp = _find_fleet(g, fleet_name)
    if fp is None:
        return {"error": "no fleet named %s" % fleet_name}
    a = next((x for x in admirals(g) if x["name"].lower() == str(admiral_name).lower()), None)
    if a is None:
        return {"error": "no admiral named %s" % admiral_name}
    p = g.p
    why = _post(g, LEADER_CTOR, 0x38, [a["ptr"], fp],
                lambda c: None if struct.unpack("<ii", p.read(c + 0x24, 8)) ==
                struct.unpack("<ii", p.read(a["ptr"] + 8, 8)) and
                struct.unpack("<ii", p.read(c + 0x2C, 8)) ==
                struct.unpack("<ii", p.read(fp + 8, 8)) else "admiral command not filled")
    if why:
        return {"error": why}
    h = struct.unpack("<ii", p.read(fp + FL_ADMIRAL, 8))
    return {"fleet": fleet_name, "admiral": admiral_name,
            "assigned": h == struct.unpack("<ii", p.read(a["ptr"] + 8, 8))}


# ------------------------------------------------------------------ naval headquarters
# A naval HQ is a CCustomizableBuildingItem (handle {4713,id}; the resolver 0x2EE2D70 maps
# it) with a CNavyLeaderModule at +0x40 whose +0x08 is the assigned admiral's character
# handle {73,id}; the admiral's CNavyLeader +0xEA8 holds the HQ's handle. Verified 22.10:
#   CAssignAdmiralToNavyHeadquarter ctor 0x23394E0 (this, HQ item, CNavyLeader*)
#   CRemoveAdmiralFromNavyHeadquarter ctor 0x2339990 (this, CNavyLeader*)
# The HQ items of all countries live together; ours are those with our admiral, or where
# the game accepts one of ours (CanExecute, vtable slot 10).
HQ_ASSIGN_CTOR = 0x23394E0
HQ_REMOVE_CTOR = 0x2339990
HQ_MODULE = 0x40
AL_HQ = 0xEA8


def _can(g, ctor, args):
    api = B("api")
    p = g.p
    api.allow_call(ctor)
    cmd = g.malloc(0x40)
    p.write(cmd, b"\0" * 0x40)
    g.call(ctor, [cmd] + list(args))
    ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
    api.allow_call(ce)
    return cmd, bool(g.call(ce, [cmd, 0]) & 0xFF)


def _hq_owner(g, x):
    """The country (CCountry*) of most task forces within an HQ's range (+0x18; each
    entry points into a task force whose +0x8 or +0x118 is its CFleet or CCountry)."""
    import collections
    p = g.p
    own = collections.Counter()
    for e in B("game")._arr(p, x, 0x18):
        for o in (0x8, 0x118):
            q = p.u64(e + o)
            c = g.oi.class_of(q)
            if c == "CFleet":
                q, c = p.u64(q + FL_COUNTRY), "CCountry"
            if c == "CCountry":
                own[q] += 1
                break
    return own.most_common(1)[0][0] if own else None


def headquarters(g, tag=None):
    """[{id, ptr, admiral, task_forces_in_range}] of our naval HQs."""
    p = g.p
    me = g._c(tag).ptr
    names = {struct.unpack("<ii", p.read(ch + 8, 8)):
             B("game").item_name(p, ch + B("game").CH_NAME) for ch in g.all_characters(tag)}
    out = []
    for x in g.oi.instances("CCustomizableBuildingItem"):
        if p.i32(x + 8) != 4713 or not p.u64(x + HQ_MODULE) or _hq_owner(g, x) != me:
            continue
        h = struct.unpack("<ii", p.read(p.u64(x + HQ_MODULE) + 8, 8))
        out.append({"id": p.i32(x + 0xC), "ptr": x, "admiral": names.get(h),
                    "task_forces_in_range": p.i32(x + 0x24)})
    return out


def assign_to_hq(g, hq_id, admiral_name):
    a = next((x for x in admirals(g) if x["name"].lower() == str(admiral_name).lower()), None)
    if a is None:
        return {"error": "no admiral named %s" % admiral_name}
    hq = next((x for x in headquarters(g) if x["id"] == int(hq_id)), None)
    if hq is None:
        return {"error": "no naval HQ %s of ours" % hq_id}
    own = g._inj is None
    if own:
        g.attach()
    try:
        if hq["admiral"]:
            prev = next((x for x in admirals(g) if x["name"] == hq["admiral"]), None)
            if prev:
                cmd, ok = _can(g, HQ_REMOVE_CTOR, [prev["ptr"]])
                if ok:
                    g.cmd.post(cmd)
                    time.sleep(0.5)
        cmd, ok = _can(g, HQ_ASSIGN_CTOR, [hq["ptr"], a["ptr"]])
        if not ok:
            return {"error": "the game refused %s for this HQ (all admiral places in HQs "
                             "taken, or he cannot serve there)" % admiral_name}
        g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    time.sleep(0.5)
    return {"hq": int(hq_id), "admiral": admiral_name,
            "assigned": struct.unpack("<ii", g.p.read(a["ptr"] + AL_HQ, 8))[1] == int(hq_id)}


# ------------------------------------------------------------------ carriers and their air wings
# A carrier is an air base too (CAirBase, handle type 65): +0x60 the ship's handle {51,id},
# +0x78 its deck (planes). Land bases have their CState there instead (air.py). A wing's
# planes: +0x1D8 CPdxArray<CEquipmentType*> (carrier types start with cv_), +0x1F0
# CPdxArray<CEquipmentVariant*>. Found 22.10 on Japan 1936.
CV_SHIP = 0x60
CV_DECK = 0x78
W_TYPES = 0x1D8


def _wing_type(g, w):
    t = B("game")._arr(g.p, w, W_TYPES)
    return B("game").item_name(g.p, t[0] + 0x10) if t else ""


def carriers(g):
    """[{ship, task_force, fleet, deck, wings: [(id, type, planes)]}] of our carriers,
    and the carrier wings of ours that sit on land."""
    from . import air
    p = g.p
    ao = B("armyops")
    ships = {}
    for t in task_forces(g):
        for s in B("game")._arr(p, t["ptr"], TF_SHIPS):
            ships[p.i32(s + 0xC)] = t
    bases = ao.handle_objects(g, 65)
    on = {}
    for i, b in bases.items():
        try:
            ty, sid = struct.unpack("<ii", p.read(b + CV_SHIP, 8))
        except Exception:
            continue
        if ty == 51 and sid in ships:
            on[i] = {"ship": sid, "task_force": ships[sid]["name"],
                     "fleet": ships[sid]["fleet"], "deck": p.i32(b + CV_DECK),
                     "base_ptr": b, "wings": []}
    ashore = []
    for wid, w in air._wings(g).items():
        x = p.u64(w + air.W_HOME)
        bid = p.i32(x + air.HOME_BASE_HANDLE + 4) if x else None
        kind = _wing_type(g, w)
        row = (wid, kind, p.i32(w + air.W_PLANES))
        if bid in on:
            on[bid]["wings"].append(row)
        elif kind.startswith("cv_"):
            ashore.append(row + (air.base_state(g, bases.get(bid)),))
    return list(on.values()), ashore


def wings_afloat(g):
    """Ids of the wings that sit on our carriers: the admiralty's. A carrier wing ashore
    flies from land like any other (24.10: the cv wings at East Hebei were refused to the
    air staff by their type, and the sky over China stayed empty for three months)."""
    return {wid for c in carriers(g)[0] for wid, _, _ in c["wings"]}


def _kind(k):
    k = k.replace("cv_small_plane_", "").replace("_airframe", "")
    return {"": "fighter", "airframe": "fighter", "cas": "dive bomber",
            "naval_bomber": "torpedo bomber"}.get(k, k)


def carrier_lines(g, mv=None):
    """Briefing lines on the carriers' air groups."""
    cvs, ashore = carriers(g)
    out = []
    for c in cvs:
        used = sum(n for _, _, n in c["wings"])
        out.append("carrier %d in \"%s\" (%s): deck %d, %d planes: %s" % (
            c["ship"], c["task_force"], c["fleet"], c["deck"], used,
            ", ".join("wing %d %s %d" % (i, _kind(k), n) for i, k, n in c["wings"])
            or "EMPTY"))
    lines = ["CARRIERS AND THEIR AIR GROUPS (a wing short of planes fills itself from the "
             "stockpile; carrier planes are made on military factories):"] + \
        ["- " + x for x in out] if out else ["CARRIERS: none"]
    if ashore:
        lines.append("CARRIER WINGS ON LAND (wing, type, planes, state): " + "; ".join(
            "%d %s %d at state %s" % (i, _kind(k), n, st) for i, k, n, st in ashore))
    fits = []
    for c in cvs:
        free = c["deck"] - sum(n for _, _, n in c["wings"])
        ok = [r for r in ashore if 0 < r[2] <= free]
        if ok:
            fits.append("carrier %d has %d free places; fit now: %s" % (
                c["ship"], free, ", ".join("wing %d (%s %d)" % (i, _kind(k), n)
                                           for i, k, n, _ in ok)))
    if fits:
        lines.append("EMPTY DECK SPACE, put these wings on (wing_to_carrier): " + "; ".join(fits))
    stock = {s["equipment"]: s["count"] for s in g.equipment_stockpile(limit=500)["stockpile"]
             if s["equipment"].startswith("cv_")}
    lines.append("CARRIER PLANES IN STOCK: " + (", ".join("%s %d" % kv for kv in stock.items())
                                                or "none"))
    made = [(i, l.get("equipment"), l.get("factories")) for i, l in
            enumerate(g.production_lines()) if (l.get("equipment") or "").startswith("cv_")]
    lines.append("CARRIER PLANES IN PRODUCTION (line, type, military factories): " + (
        "; ".join("%d %s %s" % m for m in made) or "none"))
    return lines


def wing_to_carrier(g, wing_id, ship_id):
    """Put one of our carrier wings (on land or another carrier) on a carrier, as the
    player drags it there; it flies over as game time passes (verified 22.10)."""
    from . import air
    p = g.p
    w = air._wings(g).get(int(wing_id))
    if w is None:
        return {"error": "no wing %s of ours" % wing_id}
    if not _wing_type(g, w).startswith("cv_"):
        return {"error": "wing %s is not of carrier planes" % wing_id}
    cv = next((c for c in carriers(g)[0] if c["ship"] == int(ship_id)), None)
    if cv is None:
        return {"error": "no carrier %s of ours" % ship_id}
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd, ok = _can(g, air.TRANSFER_CTOR, [w, cv["base_ptr"], 0, 0, 0])
        if not ok:
            return {"error": "the game refused: the deck is too small for this wing, or the "
                             "carrier is out of reach"}
        g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    return {"wing": int(wing_id), "carrier": int(ship_id),
            "note": "it flies over in a few hours of game time"}


# ------------------------------------------------------------------ fleet organisation
# Verified 22.10 on Japan 1936 (task force object = table pointer - 0x10):
#   engagement: CSetNavyEngagementCommand ctor 0x2545C80 (this, TF object, level), level
#     at TF object +0x394: 0 do not engage, 1 low risk, 2 medium (default), 3 high, 4 always
#   repair: CNavyRepairModeCommand ctor 0x224F890 (this, TF object, priority, flag, 0);
#     priority at +0x464: 0 never, 1 low (severe damage), 2 medium, 3 high (default);
#     the flag at +0x4F4 is passed through unchanged
#   task forces to a fleet: CSetFleetCommand ctor 0x225D020 (this, CFleet*,
#     CPdxArray<TF object*>&), instant
#   ships to another task force: CNavyDetachShipsAndMergeCommand ctor 0x2250860 (this,
#     CShip** (table pointers), count, TF object); the ships sail over and merge
ENGAGE_CTOR, TF_ENGAGE = 0x2545C80, 0x394
REPAIR_CTOR, TF_REPAIR, TF_REPAIR_FLAG = 0x224F890, 0x464, 0x4F4
SET_FLEET_CTOR = 0x225D020
MERGE_CTOR = 0x2250860
ENGAGEMENT = ["do_not_engage", "low_risk", "medium_risk", "high_risk", "always"]
REPAIR = ["never", "low", "medium", "high"]


def _tf(g, name):
    return next((t for t in task_forces(g) if (t["name"] or "").lower() == str(name).lower()
                 and OURS(t["fleet"])),
                None)


def settings(g, tf_ptr):
    p = g.p
    r = tf_ptr - 0x10
    e, rp = p.i32(r + TF_ENGAGE), p.i32(r + TF_REPAIR)
    return (ENGAGEMENT[e] if 0 <= e < len(ENGAGEMENT) else e,
            REPAIR[rp] if 0 <= rp < len(REPAIR) else rp)


def _post_checked(g, ctor, size, args):
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd, ok = _can(g, ctor, args)
        if ok:
            g.cmd.post(cmd)
        return ok
    finally:
        if own:
            g.detach()


def set_engagement(g, tf_name, level):
    t = _tf(g, tf_name)
    if t is None:
        return {"error": "no task force named %s" % tf_name}
    if level not in ENGAGEMENT:
        return {"error": "level is one of %s" % ", ".join(ENGAGEMENT)}
    if not _post_checked(g, ENGAGE_CTOR, 0x48, [t["ptr"] - 0x10, ENGAGEMENT.index(level)]):
        return {"error": "the game refused (a strike force always engages)"}
    time.sleep(0.4)
    return {"task_force": tf_name, "engagement": settings(g, t["ptr"])[0]}


def set_repair(g, tf_name, priority):
    t = _tf(g, tf_name)
    if t is None:
        return {"error": "no task force named %s" % tf_name}
    if priority not in REPAIR:
        return {"error": "priority is one of %s" % ", ".join(REPAIR)}
    r = t["ptr"] - 0x10
    if not _post_checked(g, REPAIR_CTOR, 0x50, [r, REPAIR.index(priority),
                                                 g.p.u8(r + TF_REPAIR_FLAG), 0]):
        return {"error": "the game refused"}
    time.sleep(0.4)
    return {"task_force": tf_name, "repair": settings(g, t["ptr"])[1]}


def task_forces_to_fleet(g, tf_names, fleet_name):
    fp = _find_fleet(g, fleet_name)
    if fp is None:
        return {"error": "no fleet named %s" % fleet_name}
    tfs = [_tf(g, n) for n in tf_names]
    if not all(tfs):
        return {"error": "unknown task forces: %s" % [n for n, t in zip(tf_names, tfs) if not t]}
    p = g.p
    data = g.malloc(8 * len(tfs))
    p.write(data, struct.pack("<%dQ" % len(tfs), *[t["ptr"] - 0x10 for t in tfs]))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, len(tfs), len(tfs), p.base + B("armyops").ALLOC))
    if not _post_checked(g, SET_FLEET_CTOR, 0x58, [fp, hdr]):
        return {"error": "the game refused"}
    time.sleep(0.5)
    return {"fleet": fleet_name, "task_forces": [t["name"] for t in task_forces(g)
                                                 if t["fleet_ptr"] == fp]}


def ships_to_task_force(g, from_tf, to_tf, ship_type=None, count=None):
    """Send ships of one task force to join another: they sail over and merge."""
    src, dst = _tf(g, from_tf), _tf(g, to_tf)
    if src is None or dst is None:
        return {"error": "no task force named %s" % (from_tf if src is None else to_tf)}
    p = g.p
    ships = [s for s in B("game")._arr(p, src["ptr"], TF_SHIPS)
             if not ship_type or B("game").item_name(p, s + SH_TYPE) == ship_type]
    if count:
        ships = ships[:int(count)]
    if not ships:
        return {"error": "no such ships in %s" % from_tf}
    data = g.malloc(8 * len(ships))
    p.write(data, struct.pack("<%dQ" % len(ships), *ships))
    if not _post_checked(g, MERGE_CTOR, 0x50, [data, len(ships), dst["ptr"] - 0x10]):
        return {"error": "the game refused"}
    return {"ships": len(ships), "from": from_tf, "to": to_tf,
            "note": "they sail to the other task force and merge with it"}



# ------------------------------------------------------------------ convoys
# Country +0x1248 convoys owned (fixed 1e5), +0x1230 in use (trade, overseas supply),
# +0x1250 free: 400 / 53 / 347 for Japan 1936.06 (22.10; the sum checks, not yet seen move)
CO_CONVOYS, CO_CONVOYS_USED, CO_CONVOYS_FREE = 0x1248, 0x1230, 0x1250


CREATED = set()               # task forces our splits made
# CReorganizeShipsCommand (27.10, 1.19.3): ctor 0x2261930 (this, CFleet*, CPdxArray<CShip*>&,
# 0); it keeps one spec of 0x80 bytes (+0x58 the ships' handles, +0x70 the fleet's handle)
# and Execute makes a new task force of those ships in that fleet. A handle where a ship
# pointer belongs crashed the ctor (in our call, the game survived).
REORG_CTOR = 0x2261930


def _split_once(g, fleet_ptr, ships):
    api, p = B("api"), g.p
    api.allow_call(REORG_CTOR)
    data = g.malloc(8 * len(ships))
    p.write(data, struct.pack("<%dQ" % len(ships), *ships))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, len(ships), len(ships), p.base + B("armyops").ALLOC))
    cmd = g.malloc(0x100)
    p.write(cmd, b"\0" * 0x100)
    g.call(REORG_CTOR, [cmd, fleet_ptr, hdr, 0])
    spec = p.u64(cmd + 0x28)
    if p.i32(cmd + 0x34) != 1 or p.i32(spec + 0x64) != len(ships):
        return False
    ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
    api.allow_call(ce)
    if not g.call(ce, [cmd, 0]) & 0xFF:
        return False
    g.cmd.post(cmd)
    return True


def split_task_force(g, tf_name, size, ship_type=None, groups=None):
    """Split a task force into groups of `size` ships (of one type, or any), each a new task
    force in the same fleet; the old one keeps the rest. Up to 8 new groups a call."""
    t = _tf(g, tf_name)
    if t is None:
        return {"error": "no task force named %s" % tf_name}
    p = g.p
    size = int(size)
    ships = [s for s in B("game")._arr(p, t["ptr"], TF_SHIPS)
             if not ship_type or B("game").item_name(p, s + SH_TYPE) == ship_type]
    if size < 1 or len(ships) < size + 1:
        return {"error": "%s has %d %s: too few to split off a group of %d" % (
            tf_name, len(ships), ship_type or "ships", size)}
    most = len(ships) // size - (0 if ship_type and len(ships) < sum(t["ships"].values()) else 1)
    n = min(int(groups) if groups else most, most, 8)
    if n < 1:
        return {"error": "nothing to split: %d ships, groups of %d" % (len(ships), size)}
    before = {x["ptr"] for x in task_forces(g)}
    own = g._inj is None
    if own:
        g.attach()
    made = 0
    try:
        for i in range(n):
            if not _split_once(g, t["fleet_ptr"], ships[i * size:(i + 1) * size]):
                break
            made += 1
    finally:
        if own:
            g.detach()
    time.sleep(0.5)
    new = [x for x in task_forces(g) if x["ptr"] not in before]
    CREATED.update(x["ptr"] for x in new)
    if not made:
        return {"error": "the game refused the split"}
    return {"from": tf_name, "new_task_forces": [
        "%s (%s)" % (x["name"], ", ".join("%d %s" % (v, k) for k, v in x["ships"].items()))
        for x in new], "note": "each new group stands on hold with the fleet's regions: give "
                               "it its mission (task_force_mission) and engagement"}


def convoys(g, tag=None):
    p = g.p
    c = g._c(tag).ptr
    return {"owned": round(p.i64(c + CO_CONVOYS) / 1e5), "in_use": p.i32(c + CO_CONVOYS_USED),
            "free": p.i32(c + CO_CONVOYS_FREE)}


# auto-reinforcement: CSetTaskForceAutoReinforcementCommand ctor 0x225E7A0 (this,
# CPdxArray<TF object*>&, on); the flag at TF object +0x4F7 (off for all at the start;
# verified 22.10). A refit (CNavyDetachShipsAndRefitCommand, UI ctor 0x22516A0: ships,
# count, TF object, target variant, object with a handle) was always refused: not done.
AUTO_CTOR, TF_AUTO = 0x225E7A0, 0x4F7


def auto_reinforce(g, tf_names, on=True):
    tfs = [_tf(g, n) for n in tf_names]
    if not all(tfs):
        return {"error": "unknown task forces: %s" % [n for n, t in zip(tf_names, tfs) if not t]}
    p = g.p
    data = g.malloc(8 * len(tfs))
    p.write(data, struct.pack("<%dQ" % len(tfs), *[t["ptr"] - 0x10 for t in tfs]))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, len(tfs), len(tfs), p.base + B("armyops").ALLOC))
    if not _post_checked(g, AUTO_CTOR, 0x48, [hdr, int(bool(on))]):
        return {"error": "the game refused"}
    time.sleep(0.4)
    return {t["name"]: bool(p.u8(t["ptr"] - 0x10 + TF_AUTO)) for t in tfs}
