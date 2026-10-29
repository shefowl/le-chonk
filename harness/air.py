"""The air force: air wings, strategic regions and the commands that give wings a region
and a mission. Layouts and command constructors were found on 17.10, see docs/03-air.md.

A wing flies its mission over one STRATEGIC REGION (an air zone of a dozen states).
Models choose a region by id; the regions over our fronts are listed for them.
"""
import collections
import glob
import os
import re

from .mapview import _read

# mission bits (NAir defines order); a wing's allowed mask decides which it can fly
MISSIONS = {"air_superiority": 0, "cas": 1, "interception": 2, "strategic_bombing": 3,
            "naval_bombing": 4, "paradrop": 6, "port_strike": 8, "logistics_strike": 9,
            "air_supply": 10, "training": 11, "naval_patrol": 15}


def kind(allowed):
    """What a wing is, from the missions its planes can fly."""
    has = lambda m: allowed & (1 << MISSIONS[m])
    if has("air_superiority"):
        return "fighter"
    if has("paradrop") or has("air_supply"):
        return "transport"
    if has("strategic_bombing") and has("cas"):
        return "tactical bomber"
    if has("strategic_bombing"):
        return "strategic bomber"
    if has("cas"):
        return "close air support"
    if has("naval_bombing") or has("naval_patrol"):
        return "naval bomber"
    return "other"


def missions_of(mask):
    return [m for m, b in MISSIONS.items() if mask & (1 << b)]


class Regions:
    """Strategic regions from the game files: id -> name and provinces."""

    def __init__(self, game_dir, state_of):
        names = {int(m.group(1)): m.group(2) for m in re.finditer(
            r'^\s*STRATEGICREGION_(\d+):\d*\s*"([^"]*)"',
            _read(os.path.join(game_dir, "localisation", "english",
                               "strategic_region_names_l_english.yml")), re.M)}
        self.name, self.provinces, self.region_of = {}, {}, {}
        for path in glob.glob(os.path.join(game_dir, "map", "strategicregions", "*.txt")):
            txt = _read(path)
            m = re.search(r"\bid\s*=\s*(\d+)", txt)
            pm = re.search(r"provinces\s*=\s*\{([^}]*)\}", txt)
            if not m or not pm:
                continue
            rid = int(m.group(1))
            provs = [int(x) for x in re.sub(r"#[^\n]*", "", pm.group(1)).split()]
            self.name[rid] = names.get(rid, "region %d" % rid)
            self.provinces[rid] = provs
            for pr in provs:
                self.region_of[pr] = rid
        self.states = {rid: sorted({state_of[p] for p in ps if p in state_of})
                       for rid, ps in self.provinces.items()}

    def label(self, rid):
        return "%s (#%d)" % (self.name.get(rid, "?"), rid)

    def over(self, state_ids):
        """The regions that hold any province of these states, most states first."""
        want = set(state_ids)
        hits = {rid: len(want & set(sts)) for rid, sts in self.states.items()}
        return [rid for rid, n in sorted(hits.items(), key=lambda x: -x[1]) if n]


# ------------------------------------------------------------------ live (read)
WING_HANDLE_TYPE = 69
WING_FROM_TABLE = -0x10        # the handle table points 0x10 into the CAirWing
W_HANDLE = 0x18                # {type, id}, what commands take
W_ALLOWED = 0x28
W_PLANES = 0x74
W_MISSION = 0x98               # mission bits now flown
W_REGION = 0xB8                # CStrategicRegion* of that mission
SR_ID = 0x48                   # its id
W_TAG = 0x994                  # owner's tag index


def _wings(g):
    """{wing id: CAirWing*} of the player's country."""
    from .bridge import module
    ao = module("armyops")
    me = g._c(g.player_tag()).tag_id
    out = {}
    for i, h in ao.handle_objects(g, WING_HANDLE_TYPE).items():
        w = h + WING_FROM_TABLE
        if g.p.u32(w + W_TAG) == me:
            out[i] = w
    return out


def strength(g, tags):
    """{tag: {kind: planes}} in air wings, for us and the given countries (21.10: the
    plan gave air 5% of the factories with no idea how many planes the enemy flies)."""
    from .bridge import module
    ao = module("armyops")
    ids = {g._c(t).tag_id: t for t in tags}
    out = {t: {} for t in tags}
    for i, h in ao.handle_objects(g, WING_HANDLE_TYPE).items():
        w = h + WING_FROM_TABLE
        t = ids.get(g.p.u32(w + W_TAG))
        if t:
            k = kind(g.p.u32(w + W_ALLOWED))
            out[t][k] = out[t].get(k, 0) + g.p.i32(w + W_PLANES)
    return out


RAIDS = ("strategic_bombing", "cas", "port_strike", "logistics_strike", "naval_bombing")


def enemy_air(g, tags):
    """{region id: {mission: planes}} the air wings of these countries fly over (their
    raids and their fighters), read from their wings as from ours."""
    from .bridge import module
    ao = module("armyops")
    ids = set()
    for t in tags:
        try:
            ids.add(g._c(t).tag_id)
        except Exception:
            continue
    out = {}
    for i, h in ao.handle_objects(g, WING_HANDLE_TYPE).items():
        w = h + WING_FROM_TABLE
        if g.p.u32(w + W_TAG) not in ids:
            continue
        rid = _region(g, w)
        ms = missions_of(g.p.u32(w + W_MISSION))
        if rid is None or not ms:
            continue
        e = out.setdefault(rid, {})
        for m in ms:
            e[m] = e.get(m, 0) + g.p.i32(w + W_PLANES)
    return out


def _region(g, w):
    r = g.p.u64(w + W_REGION)
    return g.p.i32(r + SR_ID) if r else None


def wings(g):
    p = g.p
    out = []
    for i, w in sorted(_wings(g).items()):
        mask = p.u32(w + W_MISSION)
        out.append({"id": i, "planes": p.i32(w + W_PLANES), "kind": kind(p.u32(w + W_ALLOWED)),
                    "missions": missions_of(mask), "region": _region(g, w),
                    "army": attached_army(g, w),
                    "base": base_state(g, home_base(g, w)),
                    "transfer_to": base_state(g, transfer_base(g, w)),
                    "effective_base": base_state(g, effective_base(g, w)),
                    "can_fly": missions_of(p.u32(w + W_ALLOWED))})
    return out


# Air bases (handle type 65, docs/03-air.md) and which one a wing sits on: [wing+0x30]
# holds the base's handle at +0x14 (the game's own lookup, 0x1B2A310; found 19.10)
BASE_HANDLE_TYPE = 65
W_HOME = 0x30
W_TRANSFER = 0x40
W_TRANSFER_INACTIVE = 0x70
HOME_BASE_HANDLE = 0x14
AB_HANDLE = 0x08
AB_STATE = 0x68                 # CState*
AB_CAPACITY = 0x78             # planes: 200 a level (Champagne, level 10: 2000)
ST_ID = 0x48


def _bases(g):
    from .bridge import module
    return module("armyops").handle_objects(g, BASE_HANDLE_TYPE)


def base_state(g, b):
    if not b:
        return None
    try:
        st = g.p.u64(b + AB_STATE)
        return g.p.i32(st + ST_ID) if st else None
    except OSError:
        return None


def home_base(g, w):
    """The air base a wing sits on (CAirBase*), or None (a carrier wing)."""
    p = g.p
    x = p.u64(w + W_HOME)
    if not x:
        return None
    return _bases(g).get(p.i32(x + HOME_BASE_HANDLE + 4))


def transfer_base(g, w):
    """Pending land-base target, exactly as GetEffectiveBase 0x1B2C1F0 selects it.

    home_base remains the old base during a flight. A nonzero +70 disables use of
    +40; neither this reader nor the briefing changes that transfer.
    """
    p = g.p
    if p.u8(w + W_TRANSFER_INACTIVE) or p.u32(w + W_TRANSFER) != BASE_HANDLE_TYPE:
        return None
    return _bases(g).get(p.i32(w + W_TRANSFER + 4))


def effective_base(g, w):
    return transfer_base(g, w) or home_base(g, w)


def transfer_lines(g):
    rows = [w for w in wings(g) if w["transfer_to"] is not None]
    return (["AIR WINGS IN TRANSFER (current base -> ordered base; mission range is already "
             "checked from the ordered base): " + "; ".join(
                 "%d: #%s -> #%s" % (w["id"], w["base"], w["transfer_to"]) for w in rows)]
            if rows else [])


CAPACITY_PER_LEVEL = 200     # NDefines AIRBASE_CAPACITY_MULT


def bases(g, holders, own):
    """Our air bases and their capacity/load, including allied wings sharing them."""
    from .bridge import module
    p = g.p
    load = collections.Counter()
    for h in module("armyops").handle_objects(g, WING_HANDLE_TYPE).values():
        w = h + WING_FROM_TABLE
        b = home_base(g, w)
        if b:
            load[b] += p.i32(w + W_PLANES)
    out = []
    for b in _bases(g).values():
        st = base_state(g, b)
        if st is None or holders.get(st, (None, None))[1] != own:
            continue
        out.append({"state": st, "capacity": p.i32(b + AB_CAPACITY), "planes": load[b],
                    "ptr": b})
    return sorted(out, key=lambda x: x["state"])


# CDeployAirWingCommand, 1.19.4, statically recovered 28.10.2026. The constructor
# 0x1CE6910 accepts a GUI selector at +0x80; do NOT pass a country/variant there
# (that crashed 1.19.3). Its serialized form instead owns a CEquipmentStockpile at
# +0x38 with +0x78=true: Execute 0x1CE6CD0 then intersects it with production+0x200.
# Stockpile ctor 0xCFADF0 and add-variant 0xCFE290 define the two arrays below.
DEPLOY_VTABLE, DEPLOY_SIZE = 0x4415730, 0xA8
DEPLOY_CAN_EXECUTE = 0x1CE7500
STOCK_VTABLE, STOCK_ALLOCATOR = 0x438ECB8, 0x4505128


def _is_aircraft(equipment):
    return bool(equipment and any(k in equipment.lower() for k in (
        "plane", "airframe", "fighter", "bomber", "cas_equipment")))


def _reserve_rows(g):
    """Stock by exact variant, including captured/obsolete designs, without a row limit."""
    import struct
    from .bridge import module
    ind = module("industry")
    p = g.p
    g._load_equipment()
    ps = p.u64(g._c(None).ptr + ind.CO_PRODUCTION)
    stock = ps + g.PS_STOCKPILE
    n, data = p.i32(stock + 0xC), p.u64(stock)
    if n < 0 or n > 100000 or (n and not data):
        raise ValueError("invalid aircraft stockpile array")
    out = []
    for i in range(n):
        vp, fixed = struct.unpack("<Qq", p.read(data + i * 16, 16))
        if not vp or fixed < 100000:
            continue
        eq = g._eq_by_id.get(p.i32(vp + ind.EV_TYPE_ID))
        if not _is_aircraft(eq):
            continue
        if p.u32(vp + ind.EV_HANDLE) != 70:
            raise ValueError("invalid aircraft variant handle")
        eqp = p.u64(vp + 0x3F0)
        archetype = p.u64(eqp + 0x530) if eqp else 0
        if not archetype:
            raise ValueError("aircraft variant has no equipment archetype")
        out.append({"design_id": p.i32(vp + ind.EV_HANDLE + 4),
                    "equipment": eq, "design": p.stdstring(vp + ind.EV_NAME) or "",
                    "planes": fixed // 100000, "ptr": vp, "archetype": archetype})
    return sorted(out, key=lambda x: x["design_id"])


def reserves(g):
    """Deployable whole aircraft in reserve, by equipment and exact design id."""
    return [{k: v for k, v in r.items() if k not in ("ptr", "archetype")}
            for r in _reserve_rows(g)]


def reserve_lines(g):
    rows = reserves(g)
    return ["AIRCRAFT RESERVES (design_id: equipment, design, whole planes; not in wings): "
            + ("; ".join('%d: %s "%s", %d' % (
                r["design_id"], r["equipment"], r["design"], r["planes"]) for r in rows)
               or "none") + ". create_air_wing deploys one chosen design at a chosen "
            "air base; then give its new wing a mission or attach it to an army."]


def _build_deploy(g, target, variant, planes, allocations=None):
    """Build the serializable command directly, with an owned one-variant stockpile."""
    import struct
    from .bridge import module
    p = g.p
    def allocate(size):
        ptr = g.malloc(size)
        if allocations is not None:
            allocations.append(ptr)
        return ptr

    cmd = allocate(DEPLOY_SIZE)
    group, variants = allocate(24), allocate(16)
    count = planes * 100000
    group_blob = struct.pack("<Qiiq", variant["archetype"], 0, 1, count)
    variants_blob = struct.pack("<Qq", variant["ptr"], count)
    blob = bytearray(DEPLOY_SIZE)
    struct.pack_into("<Q", blob, 0, p.base + DEPLOY_VTABLE)
    for off, fmt, value in module("commands").BASE_INIT:
        struct.pack_into(fmt, blob, off, value)
    struct.pack_into("<i", blob, 0x24, g._c(None).tag_id)
    # +0x28 is the optional secondary country tag (zero); +0x2C target base handle.
    handle = p.read(target["ptr"] + AB_HANDLE, 8)
    if struct.unpack("<II", handle)[0] != BASE_HANDLE_TYPE:
        raise ValueError("invalid target air base handle")
    blob[0x2C:0x34] = handle
    struct.pack_into("<Q", blob, 0x38, p.base + STOCK_VTABLE)
    allocator = p.base + STOCK_ALLOCATOR
    # CPdxArray has capacity at +8 and size at +12 (both one here).
    struct.pack_into("<QiiQ", blob, 0x40, group, 1, 1, allocator)
    struct.pack_into("<QiiQ", blob, 0x58, variants, 1, 1, allocator)
    blob[0x78] = 1
    p.write(group, group_blob)
    p.write(variants, variants_blob)
    p.write(cmd, bytes(blob))
    if (p.read(cmd, DEPLOY_SIZE) != bytes(blob) or p.read(group, 24) != group_blob
            or p.read(variants, 16) != variants_blob):
        raise ValueError("air wing command fields failed read-back")
    return cmd


def _deploy_allowed(g, cmd):
    from .bridge import module
    p = g.p
    # Fail closed after a port: never call an unidentified vtable entry.
    can = p.u64(p.u64(cmd) + 10 * 8) - p.base
    if can != DEPLOY_CAN_EXECUTE:
        raise ValueError("unrecognized DeployAirWing CanExecute address")
    module("api").allow_call(can)
    return bool(g.call(can, [cmd, 0]) & 0xFF)


def create_wing(g, state, design_id, planes, holders, own, dry_run=False):
    """Create ONE wing from the model's chosen reserve design, count and land base.

    No mission, rebasing, design substitution or base selection happens here. Use only
    through Gateway; dry_run constructs/checks the command but never posts it.
    """
    import time
    if isinstance(planes, bool) or not isinstance(planes, int) or not 1 <= planes <= 100:
        return {"ok": False, "error": "planes must be an integer from 1 to 100"}
    state, design_id = int(state), int(design_id)
    target = next((b for b in bases(g, holders, own) if b["state"] == state), None)
    if target is None:
        return {"ok": False, "error": "we have no controlled air base in state #%d" % state}
    room = target["capacity"] - target["planes"]
    if room < planes:
        return {"ok": False, "error": "air base #%d has room for %d planes, requested %d"
                % (state, max(0, room), planes)}
    variant = next((v for v in _reserve_rows(g) if v["design_id"] == design_id), None)
    if variant is None or variant["planes"] < planes:
        return {"ok": False, "error": "design %d has %d whole planes in reserve, requested %d"
                % (design_id, variant["planes"] if variant else 0, planes)}
    before = {w["id"] for w in wings(g)}
    own_inj = g._inj is None
    if own_inj:
        g.attach()
    allocations, handed_off = [], False
    try:
        cmd = _build_deploy(g, target, variant, planes, allocations)
        if not _deploy_allowed(g, cmd):
            return {"ok": False, "error": "the game refuses this wing (CanExecute false)"}
        if dry_run:
            return {"ok": True, "dry_run": True, "state": state,
                    "design_id": design_id, "planes": planes}
        # Ownership crosses at Post. If the injected call raises, its outcome may
        # be uncertain: never free memory the game might already own.
        handed_off = True
        g.cmd.post(cmd)
    finally:
        try:
            if not handed_off:
                # Use only addresses returned by our allocations, never fields
                # that may have failed read-back, or borrowed variant pointers.
                for ptr in reversed(allocations):
                    g.free(ptr)
        finally:
            if own_inj:
                g.detach()
    time.sleep(0.5)
    created = [w for w in wings(g) if w["id"] not in before and w["base"] == state]
    actual = sum(w["planes"] for w in created)
    # A new wing alone does not establish which design equipped it. Confirm that
    # the selected variant left reserve too. Other wings may refill while the game
    # runs, so consumption can exceed the requested amount; never require equality.
    reserve_before = variant["planes"]
    reserve_after = next((v["planes"] for v in _reserve_rows(g)
                          if v["design_id"] == design_id), 0)
    consumed = reserve_before - reserve_after
    ok = len(created) == 1 and actual == planes and consumed >= planes
    result = {"ok": ok, "state": state, "design_id": design_id,
              "design": variant["design"], "created_wings": [w["id"] for w in created],
              "planes": actual, "requested_planes": planes,
              "reserve_before": reserve_before, "reserve_after": reserve_after,
              "consumed": consumed}
    if not ok:
        result["error"] = ("deployment not confirmed: read the wings again before retrying; "
                           "the game may have refused or not processed the command yet, "
                           "or the selected design's reserve consumption is not confirmed")
    return result


TRANSFER_CTOR, TRANSFER_SIZE = 0x241AF40, 0x40   # (this, wing*, base*, 0, region*, mask)


def rebase(g, wing_ids, state, holders, own):
    """Move wings to our air base in `state`, as the player does by dragging them there."""
    import time
    from .bridge import module
    api = module("api")
    wing_ids = list(dict.fromkeys(wing_ids))
    target = next((b for b in bases(g, holders, own) if b["state"] == int(state)), None)
    if target is None:
        return {"ok": False, "confirmed": False,
                "error": "we have no air base in state #%s" % state}
    mine = _wings(g)
    from . import navy
    afloat = navy.wings_afloat(g)
    p = g.p
    own_inj = g._inj is None
    if own_inj:
        g.attach()
    try:
        api.allow_call(TRANSFER_CTOR)
        for i in wing_ids:
            if i not in mine or i in afloat:
                continue
            cmd = g.malloc(TRANSFER_SIZE); p.write(cmd, b"\0" * TRANSFER_SIZE)
            g.call(TRANSFER_CTOR, [cmd, mine[i], target["ptr"], 0, 0, 0])
            g.cmd.post(cmd)
    finally:
        if own_inj:
            g.detach()
    time.sleep(0.5)
    # 25.10 (n3e): carrier wings came back "ok" with base_state None, three times
    fresh = _wings(g)
    out = {"to_state": int(state), "moved": [], "transferring": [], "not_moved": {}}
    for i in wing_ids:
        if i not in fresh:
            out["not_moved"][i] = "not our wing"
        elif i in afloat:
            out["not_moved"][i] = "on a carrier (the admiralty's)"
        else:
            st = base_state(g, home_base(g, fresh[i]))
            if st == int(state):
                out["moved"].append(i)
            elif base_state(g, transfer_base(g, fresh[i])) == int(state):
                out["transferring"].append(i)
            else:
                out["not_moved"][i] = "still at #%s; no transfer to #%s confirmed" % (st, state)
    out["refused"] = dict(out["not_moved"])
    out["pending"] = bool(out["transferring"])
    out["ok"] = out["confirmed"] = bool(wing_ids) and len(out["moved"]) == len(wing_ids)
    out["transfer_confirmed"] = bool(wing_ids) and (
        len(out["moved"]) + len(out["transferring"]) == len(wing_ids))
    out["requested_ids"] = wing_ids
    if not out["not_moved"]:
        del out["not_moved"]
    return out


# ------------------------------------------------------------------ live (orders)
# CPdxArray<handle> constructors (docs/03-air.md)
ENABLE_CTOR, ENABLE_SIZE = 0x2418C90, 0x60    # (this, wings&, region, other&)
MISSION_CTOR, MISSION_SIZE = 0x2419910, 0x48  # (this, wings&, mission, enable, clear)


def _post_air_order(g, w, vt, size, fields, can, second_array=False, dry_run=False):
    """Serialized forms of the identified air commands, with owned handle array.

    Enable ctor 0x2418C90, mission ctor 0x2419910: CPdxArray at +28, optional
    empty second array at +40. No GUI pointers or native predicates are guessed.
    """
    import struct
    from .bridge import module
    p, allocations, handed_off = g.p, [], False
    own = g._inj is None
    if own:
        g.attach()
    try:
        handle = p.read(w + W_HANDLE, 8)
        if struct.unpack('<I', handle[:4])[0] != WING_HANDLE_TYPE:
            return {"ok": False, "error": "not an air wing handle"}
        data = g.malloc(8)
        allocations.append(data)
        cmd = g.malloc(size)
        allocations.append(cmd)
        blob = bytearray(size)
        struct.pack_into('<Q', blob, 0, p.base + vt)
        for off, fmt, value in module('commands').BASE_INIT:
            struct.pack_into(fmt, blob, off, value)
        struct.pack_into('<QiiQ', blob, 0x28, data, 1, 1,
                         p.base + module('armyops').ALLOC)
        if second_array:
            struct.pack_into('<QiiQ', blob, 0x40, 0, 0, 0,
                             p.base + module('armyops').ALLOC)
        for off, fmt, value in fields:
            struct.pack_into(fmt, blob, off, value)
        p.write(data, handle)
        p.write(cmd, bytes(blob))
        if p.read(cmd, size) != bytes(blob) or p.read(data, 8) != handle:
            return {"ok": False, "error": "air command fields failed read-back"}
        actual = p.u64(p.u64(cmd) + 80) - p.base
        if actual != can:
            return {"ok": False, "error": "unrecognized air command CanExecute"}
        module('api').allow_call(can)
        if not g.call(can, [cmd, 0]) & 0xFF:
            return {"ok": False, "error": "game refused air command (CanExecute false)"}
        if dry_run:
            return {"ok": True, "dry_run": True}
        handed_off = True
        g.cmd.post(cmd)
        return {"ok": True}
    finally:
        try:
            if not handed_off:
                for ptr in reversed(allocations):
                    g.free(ptr)
        finally:
            if own:
                g.detach()


def _post_region(g, w, region, dry_run=False):
    return _post_air_order(g, w, 0x44701F0, ENABLE_SIZE,
                           [(0x58, '<i', int(region)), (0x5C, '<B', 0)],
                           0x2419200, second_array=True, dry_run=dry_run)


def _post_mission(g, w, mask, enable=True, clear=True, dry_run=False):
    # Exec 0x2419AB0 forwards +44 as enable. 0x1B036EC disables only mask bits;
    # +45 clears all missions first, so false preserves every other bit.
    return _post_air_order(g, w, 0x44702B8, MISSION_SIZE,
                           [(0x40, '<i', mask), (0x44, '<B', int(enable)),
                            (0x45, '<B', int(clear)), (0x46, '<B', 0)], 0x2419C90,
                           dry_run=dry_run)


def _handles(g, ws):
    import struct
    from .bridge import module
    p = g.p
    data = g.malloc(max(1, len(ws)) * 8)
    p.write(data, b"".join(p.read(w + W_HANDLE, 8) for w in ws))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, len(ws), len(ws),
                             p.base + module("armyops").ALLOC))
    return hdr


def _empty(g):
    import struct
    from .bridge import module
    hdr = g.malloc(0x18)
    g.p.write(hdr, struct.pack("<QiiQ", 0, 0, 0, g.p.base + module("armyops").ALLOC))
    return hdr


def fly(g, wing_ids, region, mission):
    """Send these wings to fly `mission` over strategic region `region`, as the player
    does by selecting the wings, clicking the region and picking the mission."""
    import time
    if mission not in MISSIONS:
        raise ValueError("unknown mission %s" % mission)
    wing_ids = list(dict.fromkeys(wing_ids))
    mine = _wings(g)
    from . import navy
    afloat = navy.wings_afloat(g)
    # 22.10: carrier wings are the admiralty's while they sit on their carriers
    p = g.p
    refused = {i: ("not our wing (no such wing)" if i not in mine else
                   "on a carrier (the admiralty's)") for i in wing_ids
               if i not in mine or i in afloat}
    for i in wing_ids:
        if i not in mine or i in afloat:
            continue
        w = mine[i]
        previous = p.u32(w + W_MISSION)
        if previous == 1 << MISSIONS[mission] and _region(g, w) == region:
            continue
        stopped = bool(previous & (1 << MISSIONS['training']))
        reason = None
        if stopped:
            r = _post_mission(g, w, 1 << MISSIONS['training'], False, False)
            time.sleep(0.5)
            if not r['ok'] or p.u32(w + W_MISSION) != previous & ~(1 << MISSIONS['training']):
                refused[i] = r.get('error') or 'stopping training not confirmed'
                continue
        r = _post_region(g, w, region)
        time.sleep(0.5)
        if not r['ok'] or _region(g, w) != region:
            reason = r.get('error') or 'region assignment not confirmed'
        else:
            r = _post_mission(g, w, 1 << MISSIONS[mission])
            time.sleep(0.5)
            if not r['ok'] or p.u32(w + W_MISSION) != 1 << MISSIONS[mission]:
                reason = r.get('error') or 'mission assignment not confirmed'
        if reason:
            if stopped:
                r = _post_mission(g, w, previous)
                time.sleep(0.5)
                reason += ('; previous training restored' if r['ok'] and
                           p.u32(w + W_MISSION) == previous else
                           '; restoring previous training not confirmed')
            refused[i] = reason
    actual = [{"id": i, "region": _region(g, mine[i]),
               "missions": missions_of(p.u32(mine[i] + W_MISSION))}
              for i in wing_ids if i in mine]
    confirmed = bool(wing_ids) and not refused and all(
        w["region"] == region and mission in w["missions"] for w in actual)
    return {"ok": confirmed, "confirmed": confirmed, "wings": actual,
            "requested_ids": wing_ids, "refused": refused}


def _validate_wing_count(count, optional=True):
    if optional and count is None:
        return
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("wing count must be a positive integer")


def assign(g, region, mission, wing_ids=None, kind_=None, count=None, exclude=()):
    """fly() for a list of wing ids, or for `count` wings of a kind (idle ones first).
    Says which wings did not take the region (out of range, or the game refused).
    exclude: wings another air staff ordered (never taken from it)."""
    _validate_wing_count(count)
    from . import navy
    mine = _wings(g)
    afloat = navy.wings_afloat(g)
    # carrier wings are the admiralty's while on a carrier: not in the pool (23.10)
    ws = [w for w in wings(g) if w["id"] not in set(exclude) and w["id"] in mine
          and w["id"] not in afloat]
    taken = sorted(set(wing_ids or ()) & set(exclude))
    if taken:
        raise ValueError("wings %s fly for the other air staff; not yours to take" % taken)
    if wing_ids:
        pick = [w for w in ws if w["id"] in set(wing_ids)]
    else:
        pool = [w for w in ws if w["kind"] == kind_]
        if not pool:
            raise ValueError("we have no %s wings" % kind_)
        pool.sort(key=lambda w: (bool(w["missions"]), -w["planes"]))
        pick = pool[:count or len(pool)]
    bad = [w["id"] for w in pick if mission not in w["can_fly"]]
    if bad:
        raise ValueError("wings %s cannot fly %s" % (bad, mission))
    if not pick and not wing_ids:
        raise ValueError("no wings chosen")
    requested_ids = list(dict.fromkeys(wing_ids)) if wing_ids else [w["id"] for w in pick]
    requested_count = len(requested_ids) if wing_ids or count is None else int(count)
    out = fly(g, requested_ids, region, mission)
    if "error" in out:
        raise ValueError(out["error"])
    refused = dict(out.get("refused", {}))
    ok = [w for w in out["wings"] if w["id"] not in refused and
          w["region"] == region and mission in w["missions"]]
    confirmed = bool(requested_count) and len(ok) == requested_count and not refused
    res = {"ok": confirmed, "confirmed": confirmed,
           "region": region, "mission": mission, "wings_flying_it": [w["id"] for w in ok],
           "wings": out["wings"], "refused": refused,
           "requested_ids": requested_ids, "requested_wings": requested_count,
           "planes": sum(w["planes"] for w in pick if w["id"] in {x["id"] for x in ok})}
    if len(requested_ids) < requested_count:
        res["unfilled_wings"] = requested_count - len(requested_ids)
    miss = [i for i in requested_ids if i not in {w["id"] for w in ok}]
    if miss:
        armed = [w["id"] for w in pick if w["id"] in miss and w["army"]]
        res["not_taken"] = {"wings": miss, "why": (
            "wings %s are attached to an army; the others are probably out of range of "
            "their air base: rebase them (rebase_air) to a base nearer the region"
            % armed) if armed else "probably out of range of their air base: rebase them "
            "(rebase_air) to a base nearer the region"}
        res["not_taken"]["details"] = out.get("refused", {})
        res["not_taken"]["bases"] = [{k: w.get(k) for k in
            ("id", "base", "transfer_to", "effective_base")} for w in pick if w["id"] in miss]
    return res


def summary(g, regions):
    """The air force for a briefing: what flies where, and what sits idle."""
    groups, idle, armies = {}, {}, {}
    for w in wings(g):
        if w["army"]:
            e = armies.setdefault(w["army"], {"wings": [], "planes": 0, "kinds": {}})
            e["wings"].append(w["id"]); e["planes"] += w["planes"]
            e["kinds"][w["kind"]] = e["kinds"].get(w["kind"], 0) + 1
        elif w["missions"] and w["region"]:
            k = (regions.label(w["region"]), ",".join(w["missions"]), w["kind"])
            e = groups.setdefault(k, {"wings": [], "planes": 0})
            e["wings"].append(w["id"]); e["planes"] += w["planes"]
        else:
            e = idle.setdefault(w["kind"], {"wings": [], "planes": 0})
            e["wings"].append(w["id"]); e["planes"] += w["planes"]
    return {"flying": [{"region": r, "mission": m, "kind": k, **v}
                       for (r, m, k), v in sorted(groups.items())],
            "attached_to_armies (rebase and support them by themselves)": armies,
            "idle_by_kind": idle}


ATTACH_CTOR, ATTACH_SIZE = 0x241F840, 0x48     # (this, wing*, army COrdersGroup*)
W_ARMY = 0x988                                 # COrdersGroup* of the army it supports


def attached_army(g, w):
    og = g.p.u64(w + W_ARMY)
    return g.p.i32(og + 0xC) if og else None   # the army's handle {53, id} is at +0x8


def attach(g, wing_ids, og):
    """Attach wings to an army: they rebase with it and support it on their own, as the
    player does by dragging wings onto an army."""
    import time
    from .bridge import module
    api = module("api")
    wing_ids = list(dict.fromkeys(wing_ids))
    mine = _wings(g)
    from . import navy
    afloat = navy.wings_afloat(g)
    # 22.10: carrier wings are the admiralty's while they sit on their carriers
    ws = [mine[i] for i in wing_ids if i in mine and i not in afloat]
    refused = {i: ("not our wing (no such wing)" if i not in mine else
                   "on a carrier (the admiralty's)") for i in wing_ids
               if i not in mine or i in afloat}
    if not ws:
        return {"ok": False, "confirmed": False,
                "wings": {i: attached_army(g, mine[i]) for i in wing_ids if i in mine},
                "requested_ids": wing_ids, "refused": refused}
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        api.allow_call(ATTACH_CTOR)
        for w in ws:
            cmd = g.malloc(ATTACH_SIZE); p.write(cmd, b"\0" * ATTACH_SIZE)
            g.call(ATTACH_CTOR, [cmd, w, og])
            g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    time.sleep(0.5)
    fresh = _wings(g)
    actual = {i: attached_army(g, fresh[i]) for i in wing_ids if i in fresh}
    target = p.i32(og + 0xC) if og else None
    for i in wing_ids:
        if i not in actual:
            refused[i] = "not our wing (no such wing on readback)"
        elif i not in refused and actual[i] != target:
            refused[i] = "army attachment not confirmed (actual army %s)" % actual[i]
    confirmed = bool(wing_ids) and not refused
    return {"ok": confirmed, "confirmed": confirmed, "wings": actual,
            "requested_ids": wing_ids, "refused": refused}


def attach_to_army(g, army_id, wing_ids=None, kind_=None, count=None, exclude=()):
    """attach() by kind and count (unattached, idle wings first) or by ids."""
    _validate_wing_count(count)
    from . import ops
    og = ops.army(g, army_id)
    if g.p.u8(og + ops.OG_IS_GROUP):
        raise ValueError("%d is an army group; attach wings to one of its armies" % army_id)
    taken = sorted(set(wing_ids or ()) & set(exclude))
    if taken:
        raise ValueError("wings %s fly for the other air staff; not yours to take" % taken)
    mine = _wings(g)
    from . import navy
    afloat = navy.wings_afloat(g)
    # 23.10: carrier wings went first by kind (idle, unattached) and were then refused
    # as the admiralty's, so no wing was ever attached
    ws = [w for w in wings(g) if w["id"] not in set(exclude) and w["id"] in mine
          and w["id"] not in afloat]
    if wing_ids:
        pick = [w["id"] for w in ws if w["id"] in set(wing_ids)]
    else:
        pool = [w for w in ws if w["kind"] == kind_]
        if not pool:
            raise ValueError("we have no %s wings" % kind_)
        pool.sort(key=lambda w: (attached_army(g, mine[w["id"]]) is not None,
                                 bool(w["missions"]), -w["planes"]))
        pick = [w["id"] for w in pool[:count or len(pool)]]
    requested_ids = list(dict.fromkeys(wing_ids)) if wing_ids else pick
    requested_count = len(requested_ids) if wing_ids or count is None else int(count)
    out = attach(g, requested_ids, og)
    if "error" in out:
        raise ValueError(out["error"])
    refused = out.get("refused", {})
    ok = [i for i, a in out["wings"].items() if a == army_id and i not in refused]
    confirmed = bool(requested_count) and len(ok) == requested_count and not refused
    res = {"ok": confirmed, "confirmed": confirmed, "army": army_id,
           "wings_attached": ok, "wings": out["wings"], "refused": refused,
           "requested_ids": requested_ids, "requested_wings": requested_count}
    if len(requested_ids) < requested_count:
        res["unfilled_wings"] = requested_count - len(requested_ids)
    if len(ok) < len(requested_ids):
        res["not_attached"] = [i for i in requested_ids if i not in ok]
    return res


COMBAT = ("air_superiority", "cas", "interception", "strategic_bombing",
          "logistics_strike")


def strategic_alarm(g, regions, at_war, war_states=(), our_states=(), foes=()):
    """Why the air staff must act now, or None: combat wings idle in a war, or flying
    over skies where no enemy we fight is (24.10, the user: the sky over China empty two
    months into the war, the fighters over Eastern Manchuria facing a Soviet Union at
    peace). war_states: the states the enemies we fight hold, and ours at the front."""
    from . import doctrine_files
    d = doctrine_files.affordable(g, "air")
    # a newer airframe with no design and no experience to make one, and no wing
    # training (21.10: three years of it, the 1934 fighter still the line in 1939)
    from .bridge import module as B_module
    ind = B_module("industry")
    xp = ind.experience(g)["air"]
    waiting = [v["equipment"] for v in ind.variants(g)
               if v["frame"] and "plane" in (v["equipment"] or "")]
    training = any("training" in f["mission"] for f in summary(g, regions)["flying"])
    if waiting and xp < 25 and not training:
        t = ("newer airframes wait for a design (%s) and air experience is %.0f: "
             "wings on training bank it" % (", ".join(sorted(waiting)), xp))
        d = "; ".join(x for x in (d, t) if x)
    if not at_war:
        return d
    theirs = enemy_air(g, foes) if foes else {}
    home = set(regions.over(our_states)) if our_states else set()
    if our_states and theirs:
        # raids over our land, and what intercepts them (the user, 24.10: defend our own
        # sky against raids, the interception mission)
        cover = {}
        for f in summary(g, regions)["flying"]:
            if "interception" in f["mission"] or "air_superiority" in f["mission"]:
                cover[f["region"]] = cover.get(f["region"], 0) + f["planes"]
        raided, contested = [], []
        for rid, ms in theirs.items():
            if rid not in home:
                continue
            bombs = sum(n for m, n in ms.items() if m in RAIDS)
            ours = cover.get(regions.label(rid), 0)
            # 27.10, the user: the Luftwaffe contests the sky over the home islands
            fighters = ms.get("air_superiority", 0)
            if fighters and ours < fighters:
                contested.append("%s: his fighters %d on air superiority, ours %d" % (
                    regions.label(rid), fighters, ours))
            if bombs and ours < bombs:
                raided.append("%s: %s, our interceptors and fighters %d planes" % (
                    regions.label(rid), ", ".join("%d %s" % (n, m) for m, n in ms.items()),
                    ours))
        if raided or contested:
            r = "; ".join(x for x in (
                "enemy raids over our land (interception there: fighters on interception "
                "bring the bombers down before they hit; air superiority fights his "
                "fighters): " + "; ".join(raided) if raided else "",
                "the enemy contests our own sky: " + "; ".join(contested) if contested else "")
                if x)
            r += (". Against it: more fighter wings there; radar stations (they find his "
                  "raids for our interceptors) and anti-air in the bombed states "
                  "(request_air_defence); fighters from the arms market if the factories "
                  "cannot make them fast enough (order_aircraft: ask the industry to buy them, "
                  "PLANES ON THE ARMS MARKET)")
            d = "; ".join(x for x in (r, d) if x)
    if war_states or theirs:
        # 27.10: a region is hot when planes fly or armies fight under it, not when the
        # enemy holds a state there (every desert region counted, and the wings over
        # Africa never showed as idle while the Luftwaffe filled the sky over Britain)
        hot = {rid for rid, e in sky(g, regions, foes).items() if _need(e)}
        quiet = {}
        for f in summary(g, regions)["flying"]:
            rid = next((r for r, n in regions.name.items()
                        if regions.label(r) == f["region"]), None)
            # interceptors over our own land stand guard there, raids or not
            guard = rid in home and f["mission"] == "interception"
            if rid is not None and rid not in hot and not guard and any(
                    m in f["mission"] for m in COMBAT):
                quiet.setdefault(f["region"], []).extend(f["wings"])
        if quiet:
            q = ("flying where no enemy we fight is: " + "; ".join(
                "wings %s over %s" % (ws, r) for r, ws in quiet.items()) +
                 " (the skies of the war: %s)" % ", ".join(
                     regions.label(r) for r in sorted(hot))[:300])
            d = "; ".join(x for x in (q, d) if x)
    idle = summary(g, regions)["idle_by_kind"]
    k = {x: v for x, v in idle.items() if x in ("fighter", "close air support",
                                                 "tactical bomber", "strategic bomber",
                                                 "naval bomber")}
    if k:
        i = "idle: " + ", ".join("%d %s wings" % (len(v["wings"]), x) for x, v in k.items())
        return "; ".join(x for x in (i, d) if x)
    return d


# ------------------------------------------------------------------ the sky by region (27.10)
# The user, 27.10: wings sat in Africa guarding empty skies while the Luftwaffe filled the
# sky over Britain. strategic_alarm called a region "hot" when the enemy held any state
# under it, so every desert region counted. The sky is read by what flies and fights in it.
FIGHTERS = ("fighter", "heavy fighter", "carrier fighter")


def sky(g, regions, foes):
    """{region: {his_fighters, his_raids, our_fighters, our_strike, battles, our_attacks,
    wings: {kind: [ids]}}} for every region where anybody flies or we fight."""
    from .bridge import module
    out = collections.defaultdict(lambda: {"his_fighters": 0, "his_raids": 0,
                                           "our_fighters": 0, "our_strike": 0, "battles": 0,
                                           "our_attacks": 0, "wings": {}})
    for rid, ms in (enemy_air(g, foes) if foes else {}).items():
        out[rid]["his_fighters"] += ms.get("air_superiority", 0) + ms.get("interception", 0)
        out[rid]["his_raids"] += sum(n for m, n in ms.items() if m in RAIDS)
    for w in wings(g):
        if not (w["region"] and w["missions"]) or w["army"]:
            continue
        e = out[w["region"]]
        if w["kind"] in FIGHTERS and set(w["missions"]) & {"air_superiority", "interception"}:
            e["our_fighters"] += w["planes"]
        elif set(w["missions"]) & set(COMBAT):
            e["our_strike"] += w["planes"]
        e["wings"].setdefault(w["kind"], []).append(w["id"])
    cb = module("combat")
    for rec in cb.combats(g):
        rid = regions.region_of.get(rec.get("province"))
        if rid is None:
            continue
        out[rid]["battles"] += 1
        try:
            if cb.we_attack(g, rec):
                out[rid]["our_attacks"] += 1
        except Exception:
            pass
    return dict(out)


def _need(e):
    """How much a region's sky calls for our wings: his planes, and our battles."""
    return e["his_fighters"] + e["his_raids"] + 100 * e["battles"]


def sky_lines(g, regions, foes, home=()):
    """The sky as the air staff reads it: where he flies and we fight, what we put there,
    and our wings over skies where nothing happens."""
    s = sky(g, regions, foes)
    if not s:
        return []
    rows = sorted(s.items(), key=lambda x: -_need(x[1]))
    out = ["THE SKY BY REGION (his fighters / his raiders against our fighters / our strike "
           "planes; land battles there, ours attacking; * our own land):"]
    for rid, e in rows[:14]:
        if not (_need(e) or e["our_fighters"] or e["our_strike"]):
            continue
        tag = []
        ours = rid in home or e["battles"] or e["our_fighters"] or e["our_strike"]
        if ours and e["his_fighters"] > e["our_fighters"]:
            tag.append("OUTNUMBERED %d to %d" % (e["his_fighters"], e["our_fighters"]))
        if ours and e["his_raids"] and not e["our_fighters"]:
            tag.append("HIS RAIDS UNOPPOSED")
        out.append("  %s%s: %d/%d vs %d/%d; battles %d (%d ours attacking)%s" % (
            regions.label(rid), "*" if rid in home else "", e["his_fighters"], e["his_raids"],
            e["our_fighters"], e["our_strike"], e["battles"], e["our_attacks"],
            " - " + "; ".join(tag) if tag else ""))
    empty = [(rid, e) for rid, e in s.items() if not _need(e) and e["wings"]]
    if empty:
        out.append("  EMPTY SKY (no enemy plane, no battle of ours): " + "; ".join(
            "%s: %s" % (regions.label(rid), ", ".join(
                "%s wings %s" % (k, ids) for k, ids in e["wings"].items()))
            for rid, e in empty) + ". They wait there for nothing unless you know why "
            "(a blow coming there): concentrate_air takes wings from such skies first.")
    return out


def concentrate(g, regions, region, kind_, count, mission, holders, own, foes=(),
                exclude=(), take_from=None):
    """`count` wings of a kind to fly `mission` over `region`, taken first from idle wings,
    then from the skies that need them least (no enemy, no battle), never from an army
    they support; rebased to our air base in the region with room if out of range. The
    air staff names where; this does the moving (27.10, the user: the model keeps the
    air war, the tool saves it twenty calls)."""
    _validate_wing_count(count, optional=False)
    from . import navy
    if mission not in MISSIONS:
        raise ValueError("unknown mission %r" % mission)
    s = sky(g, regions, list(foes))
    afloat = navy.wings_afloat(g)
    pool = []
    for w in wings(g):
        if (w["kind"] != kind_ or w["army"] or w["id"] in set(exclude) or w["id"] in afloat
                or mission not in w["can_fly"]):
            continue
        if w["region"] == region and mission in w["missions"]:
            continue                    # already there
        if take_from and w["region"] not in take_from and w["missions"]:
            continue
        need = -1 if not w["missions"] else _need(s.get(w["region"], {"his_fighters": 0,
                                                                     "his_raids": 0,
                                                                     "battles": 0}))
        pool.append((need, -w["planes"], w["id"], w["region"]))
    if not pool:
        raise ValueError("no %s wing free to send (attached to armies, on carriers, or "
                         "already there)" % kind_)
    pool.sort()
    pick = pool[:int(count)]
    ids = [x[2] for x in pick]
    res = assign(g, region, mission, ids)
    out = {"region": regions.label(region), "mission": mission,
           "ok": False, "confirmed": False, "requested_wings": int(count),
           "requested_ids": ids, "refused": res.get("refused", {}),
           "wings": res.get("wings", []), "transferring": [], "pending": False,
           "taken_from": {i: (regions.label(r) if r else "idle") for _, _, i, r in pick},
           "flying_it": res.get("wings_flying_it", [])}
    if len(ids) < int(count):
        out["unfilled_wings"] = int(count) - len(ids)
    left = [i for i in ids if i not in out["flying_it"]]
    if left:
        here = set(regions.states.get(region, ()))
        cand = sorted((b for b in bases(g, holders, own) if b["state"] in here),
                      key=lambda b: -(b["capacity"] - b["planes"]))
        if not cand:
            out["not_flying"] = {"wings": left, "why": "out of range, and we have no air base "
                                 "in the region: rebase_air them to a base near it"}
            return out
        rb = rebase(g, left, cand[0]["state"], holders, own)
        out["rebased_to"] = cand[0]["state"]
        out["rebase"] = rb
        out["transferring"] = rb.get("transferring", [])
        out["pending"] = bool(out["transferring"])
        moved = rb.get("moved", [])
        if moved:
            res2 = assign(g, region, mission, moved)
            out["flying_it"] += res2.get("wings_flying_it", [])
            out["refused"].update(res2.get("refused", {}))
        out["wings"] = [w for w in wings(g) if w["id"] in ids]
        out["refused"].update(rb.get("refused", {}))
        out["refused"] = {i: why for i, why in out["refused"].items()
                          if i not in out["flying_it"] and i not in out["transferring"]}
        still = [i for i in left if i not in out["flying_it"]]
        if still:
            out["not_flying"] = {"wings": still, "why": rb.get("not_moved") or
                                 ("transfer accepted; awaiting arrival before assigning the mission"
                                  if out["pending"] else "the game did not take the region for them")}
    out["ok"] = out["confirmed"] = len(out["flying_it"]) == int(count) and int(count) > 0
    return out
