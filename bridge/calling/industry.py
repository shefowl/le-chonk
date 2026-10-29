"""New production lines, division training, and resource trade.

All three are player commands built field by field (no constructor call), checked with
the command's own CanExecute (vtable slot 10) and posted only when the game says yes.
Layouts read from 1.19.3 by disassembly on 18.10.2026: the default constructor gives
the size and defaults, the serializer names the fields (token names in brackets).

  CAddProductionLineCommand (0x40)   +0x24 i32 tag   +0x28 handle variant (type 70)
                                     +0x30 handle, optional (zero = none)  +0x38 u8 0
      CanExecute: tag > 0, the variant resolves and is the tag's own.
  CCreateConveyorCommand (0x30)      +0x24 handle division template (type 52)
                                     [division_template_id]   +0x2c i32 [role]
  CAddConveyorLineCommand (0x30)     +0x24 handle conveyor [military_deployment_conveyor_index]
                                     +0x2c i32 [amount]
  CDeployConveyorCommand (0x30)      +0x24 handle conveyor
  CDeployConveyorLineCommand (0x30)  +0x24 handle deployment line (type 63), one division
  CDeploymentStatus (country +0xF48) +0x68 CPdxArray<CMilitaryDeploymentConveyor*>
                                     +0x80 CPdxArray<CMilitaryDeployment*>, one a division
  CMilitaryDeploymentConveyor        +0x08 handle (type 64)  +0x18 template
                                     +0x70 CPdxArray<CMilitaryDeploymentLine*>
  CMilitaryDeployment                +0x60 i64 training x1e5 (0.101 -> 0.115 in two days,
                                     18.01-03.02.1940)  +0x68 i64 equipment x1e5
                                     +0xB8 CMilitaryDeploymentLine*
  CCreateTradeCommand (0x40)         +0x24 i32 [importer]  +0x28 i32 [exporter]
                                     +0x30 CStrategicResource* [resource]  +0x38 i32 [amount]
"""
import struct

BASE_INIT = [
    (0x08, "<B", 0), (0x0C, "<i", -1), (0x10, "<i", 0),
    (0x14, "<H", 0), (0x16, "<H", 0xFFFF), (0x18, "<H", 0),
    (0x1C, "<i", 0), (0x20, "<i", 0),
]
VT_CANEXE = 10

CMD = {
    "AddProductionLine": (0x4413A48, 0x40),
    "CreateConveyor": (0x4462348, 0x30),
    "CreateConveyorExtended": (0x4462410, 0x40),
    "SetConveyorSeries": (0x44628C0, 0x30),
    "AddConveyorLine": (0x4462988, 0x30),
    "DeployConveyor": (0x4462A50, 0x30),
    "DeployConveyorLine": (0x4462D70, 0x30),
    # CSetConveyorLocationCommand: +0x24 conveyor handle (type 64), +0x2C province
    # (CanExecute 0x2205C20: 0 <= province < the province count; found 19.10)
    "SetConveyorLocation": (0x44625A0, 0x30),
    # CRemoveConveyorLineCommand: +0x24 deployment line handle (type 63), one division
    "RemoveConveyorLine": (0x4462F00, 0x30),
    "CreateTrade": (0x43A7CD8, 0x40),
}

CO_PRODUCTION = 0x0F40
CO_DEPLOYMENT = 0x0F48
PS_LINES = 0x58
PS_VARIANTS_ACTIVE = 0xB8
ML_VARIANT = 0x88
EV_HANDLE = 0x08
EV_TYPE_ID = 0x14
EV_NAME = 0x28          # std::string, the design's name ("" for the base model)
DT_HANDLE = 0x08
RESOURCE_DB = "CStrategicResourceDatabase"
RDB_ITEMS = 0x28        # CPdxArray<CStrategicResource*>, item 0 is the null object
SR_TOKEN = 0x08


def _build(g, name, fields):
    vt, size = CMD[name]
    p = g.p
    cmd = g.malloc(size)
    blob = bytearray(size)
    struct.pack_into("<Q", blob, 0, p.base + vt)
    for off, fmt, val in BASE_INIT:
        struct.pack_into(fmt, blob, off, val)
    for off, fmt, val in fields:
        struct.pack_into(fmt, blob, off, val)
    p.write(cmd, bytes(blob))
    return cmd


def can_execute(g, cmd):
    """The command's own CanExecute (vtable slot 10): a pure check, no game change."""
    import api
    p = g.p
    can = p.u64(p.u64(cmd) + 8 * VT_CANEXE) - p.base
    api.allow_call(can)
    return bool(g.call(can, [cmd, 0]) & 0xFF)


def _send(g, name, fields, dry_run=False):
    """Build, ask the game whether it may run, post. Returns (ok, why)."""
    own = g._inj is None
    if own: g.attach()
    try:
        cmd = _build(g, name, fields)
        if not can_execute(g, cmd):
            return False, "the game does not allow it (CanExecute false)"
        if dry_run:
            return True, "allowed (dry run: not sent)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def _handle(p, ptr, off=0x08):
    return struct.unpack("<Q", p.read(ptr + off, 8))[0]


def _tag_index(g, tag=None):
    return g._c(tag).tag_id


# ------------------------------------------------------------------ production
def variants(g, tag=None):
    """Equipment the country can put on a line: [{equipment, design, id, ptr}]."""
    from game import _arr
    p = g.p
    g._load_equipment()
    ps = p.u64(g._c(tag).ptr + CO_PRODUCTION)
    out = []
    for v in _arr(p, ps, PS_VARIANTS_ACTIVE):
        eq = g._eq_by_id.get(p.i32(v + EV_TYPE_ID))
        if not eq:
            continue
        out.append({"equipment": eq, "design": p.stdstring(v + EV_NAME) or "",
                    "id": p.i32(v + EV_HANDLE + 4), "ptr": v,
                    "frame": bool(p.u8(v + 0x41E))})
    return out


def line_designs(g, tag=None):
    """The design name of each production line, in line order ("" = base model)."""
    from game import _arr
    p = g.p
    ps = p.u64(g._c(tag).ptr + CO_PRODUCTION)
    out = []
    for ln in _arr(p, ps, PS_LINES):
        v = p.u64(ln + ML_VARIANT)
        out.append((p.stdstring(v + EV_NAME) or "") if v else "")
    return out


def add_line(g, variant_id, tag=None, dry_run=False):
    """Open a new production line for one of the country's variants (by its id).
    The line starts without factories; give it some with set_production_amount."""
    from game import _arr
    p = g.p
    vs = {v["id"]: v for v in variants(g, tag)}
    if variant_id not in vs:
        return {"ok": False, "error": "no such variant of ours: %s" % variant_id}
    v = vs[variant_id]
    if not v["frame"]:
        miss = missing_required(g, v["ptr"])
        if miss:
            return {"ok": False, "error": "design %s (%s) leaves required slots empty: %s; "
                    "it is not a working design (make a new one with every required slot "
                    "filled)" % (variant_id, v["design"], ", ".join(miss))}
    ps = p.u64(g._c(tag).ptr + CO_PRODUCTION)
    before = len(_arr(p, ps, PS_LINES))
    ok, why = _send(g, "AddProductionLine", [
        (0x24, "<i", _tag_index(g, tag)), (0x28, "<Q", _handle(p, v["ptr"], EV_HANDLE)),
        (0x30, "<Q", 0), (0x38, "<B", 0)], dry_run)
    if not ok or dry_run:
        return {"ok": ok, "error" if not ok else "note": why}
    import time
    time.sleep(0.5)
    after = _arr(p, ps, PS_LINES)
    return {"ok": len(after) > before, "lines_before": before, "lines_after": len(after),
            "equipment": v["equipment"], "design": v["design"]}


# -------------------------------------------------------------------- training
def deployment(g, tag=None):
    return g.p.u64(g._c(tag).ptr + CO_DEPLOYMENT)


def train(g, template_ptr, tag=None, role=0, dry_run=False):
    """Start a training line for a division template (the Train button)."""
    p = g.p
    h = _handle(p, template_ptr, DT_HANDLE)
    if (h & 0xFFFFFFFF) != 52:
        return {"ok": False, "error": "not a division template handle: %#x" % h}
    ok, why = _send(g, "CreateConveyor", [(0x24, "<Q", h), (0x2C, "<i", int(role))], dry_run)
    return {"ok": ok, "error" if not ok else "note": why}


DS_CONVEYORS, DS_DEPLOYMENTS = 0x68, 0x80
CV_TEMPLATE, CV_LINES = 0x18, 0x70
CV_SERIES, CV_LOCATION = 0x40, 0x44
LINE_STARTED, LINE_DEPLOYMENT = 0x20, 0x28
DP_TRAINING, DP_EQUIPMENT, DP_LINE = 0x60, 0x68, 0xB8

# 1.19.4, statically verified 28.10.2026. Extended Execute 0x2205270 always creates
# a NEW conveyor (0xFABC10), adds parallel lines, then sets series and location.
# Series zero means forever; one means a single division per parallel line.
TRAINING_CAN_EXECUTE = {"CreateConveyorExtended": 0x2205360,
                        "SetConveyorSeries": 0x22071E0,
                        "RemoveConveyorLine": 0x2208D30}


def _send_training(g, name, fields, dry_run=False):
    """Training commands with full field read-back and identified CanExecute only."""
    import api
    import commands
    p = g.p
    vt, size = CMD[name]
    own = g._inj is None
    if own:
        g.attach()
    cmd, handed_off = None, False
    try:
        cmd = g.malloc(size)
        blob = bytearray(size)
        struct.pack_into("<Q", blob, 0, p.base + vt)
        # Shared base layout: industry.BASE_INIT above predates the 1.19.4 port.
        for off, fmt, val in commands.BASE_INIT:
            struct.pack_into(fmt, blob, off, val)
        for off, fmt, val in fields:
            struct.pack_into(fmt, blob, off, val)
        p.write(cmd, bytes(blob))
        if p.read(cmd, size) != bytes(blob):
            return False, "training command fields failed read-back"
        can = p.u64(p.u64(cmd) + 8 * VT_CANEXE) - p.base
        if can != TRAINING_CAN_EXECUTE[name]:
            return False, "unrecognized %s CanExecute address" % name
        api.allow_call(can)
        if not g.call(can, [cmd, 0]) & 0xFF:
            return False, "the game does not allow it (CanExecute false)"
        if dry_run:
            return True, "allowed (dry run: not sent)"
        handed_off = True       # Post may succeed even if its result is interrupted.
        g.cmd.post(cmd)
        return True, ""
    finally:
        try:
            if cmd is not None and not handed_off:
                g.free(cmd)
        finally:
            if own:
                g.detach()


def set_training_series(g, conveyor_id, series, tag=None, dry_run=False):
    """Explicitly set total starts per line: zero repeats; one finishes current only.

    Already-started divisions remain. Lowering series below starts prevents future
    starts after the current division; it does not cancel current equipment/manpower.
    """
    import time
    if isinstance(series, bool) or not isinstance(series, int) or not 0 <= series <= 2147483647:
        return {"ok": False, "error": "series must be a nonnegative int32 (0 repeats)"}
    rows = conveyors(g, tag)
    cv = next((c for c in rows if c["id"] == int(conveyor_id)), None)
    if cv is None:
        return {"ok": False, "error": "no training conveyor of ours with id %s" % conveyor_id}
    before = cv["series"]
    fields = [(0x24, "<Q", _handle(g.p, cv["ptr"])), (0x2C, "<i", series)]
    ok, why = _send_training(g, "SetConveyorSeries", fields, dry_run)
    if not ok or dry_run:
        return {"ok": ok, "conveyor_id": int(conveyor_id), "series_before": before,
                "requested_series": series, "error" if not ok else "note": why}
    time.sleep(0.5)
    after = next((c["series"] for c in conveyors(g, tag)
                  if c["id"] == int(conveyor_id)), None)
    result = {"ok": after == series, "conveyor_id": int(conveyor_id),
              "series_before": before, "series_after": after, "repeat": after == 0}
    if not result["ok"]:
        result["error"] = "training series change not confirmed; inspect the queue before retrying"
    return result


def train_batch(g, template_ptr, count, province, repeat=False, tag=None, dry_run=False):
    """Atomically create a NEW finite batch, preserving every existing conveyor.

    Extended CanExecute requires positive series and a valid deployment province.
    Explicit repeat is enabled separately after the finite creation is verified.
    The role stays zero, as before; location does not change training/deployment rules.
    """
    import time
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 24:
        return {"ok": False, "error": "count must be an integer from 1 to 24"}
    if not isinstance(repeat, bool):
        return {"ok": False, "error": "repeat must be a boolean"}
    if isinstance(province, bool) or not isinstance(province, int) or province <= 0:
        return {"ok": False, "error": "a positive deployment province is required"}
    h = _handle(g.p, template_ptr, DT_HANDLE)
    if h & 0xFFFFFFFF != 52:
        return {"ok": False, "error": "not a division template handle"}
    before = {c["id"]: c["series"] for c in conveyors(g, tag)}
    fields = [(0x24, "<Q", h), (0x2C, "<i", 1), (0x30, "<i", count),
              (0x34, "<i", province), (0x38, "<i", 0)]
    ok, why = _send_training(g, "CreateConveyorExtended", fields, dry_run)
    if not ok or dry_run:
        return {"ok": ok, "error" if not ok else "note": why}
    time.sleep(0.5)
    after = conveyors(g, tag)
    created = [c for c in after if c["id"] not in before]
    if (len(created) != 1 or created[0]["template_ptr"] != template_ptr
            or created[0]["divisions"] != count or created[0]["series"] != 1
            or created[0]["deployment_province"] != province
            or any(c["series"] != before[c["id"]] for c in after if c["id"] in before)):
        return {"ok": False, "created_conveyor_ids": [c["id"] for c in created],
                "error": "finite training batch not confirmed; inspect the queue before retrying"}
    cv = created[0]
    if repeat:
        r = set_training_series(g, cv["id"], 0, tag)
        if not r["ok"]:
            return {**r, "conveyor_id": cv["id"], "parallel": count,
                    "error": "finite batch created but repeat not confirmed: " + r.get("error", "")}
    return {"ok": True, "conveyor_id": cv["id"], "template": cv["template"],
            "parallel": count, "series": 0 if repeat else 1, "repeat": repeat,
            "deployment_province": province}


def training(g, tag=None):
    """Divisions in training: [{template, training, equipment, line, conveyor}] (training
    and equipment as fractions; line and conveyor are pointers)."""
    from game import _arr, item_name
    p = g.p
    ds = deployment(g, tag)
    conv_of, tmpl_of = {}, {}
    for c in _arr(p, ds, DS_CONVEYORS):
        t = p.u64(c + CV_TEMPLATE)
        for ln in _arr(p, c, CV_LINES):
            conv_of[ln] = c
            tmpl_of[ln] = item_name(p, t + 0x20) if t else None
    out = []
    for d in _arr(p, ds, DS_DEPLOYMENTS):
        ln = p.u64(d + DP_LINE)
        out.append({"template": tmpl_of.get(ln), "training": p.i64(d + DP_TRAINING) / 1e5,
                    "equipment": p.i64(d + DP_EQUIPMENT) / 1e5, "line": ln,
                    "conveyor": conv_of.get(ln)})
    return out


def conveyors(g, tag=None):
    """Training conveyors, with explicit repeat/series and per-line progress counters."""
    from game import _arr, item_name
    p = g.p
    out = []
    for c in _arr(p, deployment(g, tag), DS_CONVEYORS):
        t = p.u64(c + CV_TEMPLATE)
        series = p.i32(c + CV_SERIES)
        lines = []
        for ln in _arr(p, c, CV_LINES):
            started = max(0, p.i32(ln + LINE_STARTED))
            active = bool(p.u64(ln + LINE_DEPLOYMENT))
            lines.append({"id": p.i32(ln + 0xC), "started": started,
                          "completed": max(0, started - int(active)), "active": active,
                          "remaining": None if series == 0 else
                          int(active) + max(0, series - started)})
        out.append({"template": item_name(p, t + 0x20) if t else None,
                    "template_ptr": t, "id": p.i32(c + 0xC), "series": series,
                    "repeat": series == 0, "deployment_province": p.i32(c + CV_LOCATION),
                    "divisions": len(lines), "lines": lines, "ptr": c})
    return out


def deploy_line(g, line_ptr, dry_run=False):
    """Deploy one trained division (its deployment line)."""
    p = g.p
    ok, why = _send(g, "DeployConveyorLine", [(0x24, "<Q", _handle(p, line_ptr))], dry_run)
    return {"ok": ok, "error" if not ok else "note": why}


def remove_line(g, line_ptr, dry_run=False):
    """Take one division out of training (its deployment line)."""
    p = g.p
    ok, why = _send_training(g, "RemoveConveyorLine", [(0x24, "<Q", _handle(p, line_ptr))], dry_run)
    return {"ok": ok, "error" if not ok else "note": why}


def set_location(g, conveyor_ptr, province, dry_run=False):
    """Where a training line deploys its divisions (19.10: without one, trained divisions
    waited at 100% and DeployConveyorLine did nothing)."""
    p = g.p
    ok, why = _send(g, "SetConveyorLocation", [(0x24, "<Q", _handle(p, conveyor_ptr)),
                                                (0x2C, "<i", int(province))], dry_run)
    return {"ok": ok, "error" if not ok else "note": why}


def add_to_line(g, conveyor_ptr, amount, dry_run=False):
    p = g.p
    ok, why = _send(g, "AddConveyorLine", [(0x24, "<Q", _handle(p, conveyor_ptr)),
                                           (0x2C, "<i", int(amount))], dry_run)
    return {"ok": ok, "error" if not ok else "note": why}


def deploy(g, conveyor_ptr, dry_run=False):
    p = g.p
    ok, why = _send(g, "DeployConveyor", [(0x24, "<Q", _handle(p, conveyor_ptr))], dry_run)
    return {"ok": ok, "error" if not ok else "note": why}


# ----------------------------------------------------------------------- trade
def strategic_resources(g):
    """{name: CStrategicResource*} from the game's resource database."""
    from game import _arr
    import fieldmap
    p = g.p
    db = g.oi.singleton(RESOURCE_DB)
    out = {}
    for r in _arr(p, db, RDB_ITEMS)[1:]:
        nm = fieldmap.token_name(g, p.i32(r + SR_TOKEN))
        if nm:
            out[nm] = r
    return out


def trade(g, exporter_tag, resource, amount, tag=None, dry_run=False):
    """Buy `amount` of a resource from exporter_tag (paid in civilian factories)."""
    rs = strategic_resources(g)
    if resource not in rs:
        return {"ok": False, "error": "unknown resource %r; one of %s" % (resource, sorted(rs))}
    ok, why = _send(g, "CreateTrade", [
        (0x24, "<i", _tag_index(g, tag)), (0x28, "<i", _tag_index(g, exporter_tag)),
        (0x30, "<Q", rs[resource]), (0x38, "<i", int(amount))], dry_run)
    return {"ok": ok, "error" if not ok else "note": why}


# CCountryResources (country +0x11C0): blocks of 7 {i64 amount x1e5, i32 token} 16 bytes
# apart, 0xB0 between blocks, in the order oil, aluminium, rubber, tungsten, steel,
# chromium, coal. Read 18.10 against GER, FRA, ENG and checked against new deals:
# +0x60 our own (after exports), +0x1C0 imported, +0x270 total at hand (own + imported),
# +0x3D0 used by production (negative). (+0x60 alone made the minister buy the same
# tungsten three times: deals did not show in it.)
CO_RESOURCES = 0x11C0
RES_NAMES = ["oil", "aluminium", "rubber", "tungsten", "steel", "chromium", "coal"]
RES_OWN, RES_IMPORTED, RES_AVAILABLE, RES_USED = 0x60, 0x1C0, 0x270, 0x3D0


def resource_balance(g, tag=None):
    """{resource: (available, imported, used, balance)} per day; available includes
    imports; balance < 0 is a shortage that slows production."""
    p = g.p
    r = p.u64(g._c(tag).ptr + CO_RESOURCES)
    out = {}
    for i, nm in enumerate(RES_NAMES):
        av = p.i64(r + RES_AVAILABLE + 16 * i) / 1e5
        imp = p.i64(r + RES_IMPORTED + 16 * i) / 1e5
        used = -p.i64(r + RES_USED + 16 * i) / 1e5
        out[nm] = (round(av, 1), round(imp, 1), round(used, 1), round(av - used, 1))
    return out


def sellers(g, resource, exclude=(), top=4):
    """Countries with a surplus of `resource`: [(tag, surplus)], largest first."""
    i = RES_NAMES.index(resource)
    p = g.p
    out = []
    for c in g.countries():
        if c.tag in exclude:
            continue
        try:
            r = p.u64(c.ptr + CO_RESOURCES)
            if not r:
                continue
            spare = (p.i64(r + RES_OWN + 16 * i) + p.i64(r + RES_USED + 16 * i)) / 1e5
        except OSError:
            continue
        if spare >= 4:
            out.append((c.tag, round(spare)))
    return sorted(out, key=lambda x: -x[1])[:top]


# CResourceExchange: one deal, listed on the EXPORTER's resources +0x728 (one CPdxArray
# per resource, 24 bytes apart, indexed by CStrategicResource +0xC0). +0x80 i32 importer
# tag, +0x90 i64 amount x1e5, +0x48 i64 share delivered x1e5 (a deal with Portugal fell
# to 0.05 once France was at war: the route runs through the blockade).
CR_DEALS, SR_INDEX = 0x728, 0xC0
EX_IMPORTER, EX_AMOUNT, EX_DELIVERED = 0x80, 0x90, 0x48


def deals(g, tag=None):
    """Our import deals: [(seller, resource, amount, delivered share)]."""
    p = g.p
    me = g._c(tag).tag_id
    rs = strategic_resources(g)
    out = []
    for c in g.countries():
        try:
            r = p.u64(c.ptr + CO_RESOURCES)
            if not r:
                continue
            base = p.u64(r + CR_DEALS)
            if not base:
                continue
            for nm, rp in rs.items():
                i = p.i32(rp + SR_INDEX)
                d, n = p.u64(base + 24 * i), p.i32(base + 24 * i + 0xC)
                if not d or not 0 < n < 500:
                    continue
                for k in range(n):
                    x = p.u64(d + 8 * k)
                    if p.i32(x + EX_IMPORTER) == me:
                        out.append((c.tag, nm, round(p.i64(x + EX_AMOUNT) / 1e5, 1),
                                    round(p.i64(x + EX_DELIVERED) / 1e5, 2)))
        except OSError:
            continue
    return out


# ================================================================== designs
# CCreateEquipmentVariantCommand (0x190), built by its constructor 0xCE5900
# (this, CEquipmentVariant* working copy, parent variant*, cost, std::string* name, 0, 0).
# The working copy is an existing design with the frame's type (+0x14 type id, +0x3F0
# type object): the same modules on a newer frame. Its XP cost is the game's own
# (0xD1DEE0: working, parent, working, 0), checked by the command's CanExecute against
# our experience; the game itself deducts what it charges (19.10: D.520 cost 5 by the
# formula, the game took 1.64).
DESIGN_CTOR, DESIGN_SIZE, DESIGN_COST = 0xCE5900, 0x190, 0xD1DEE0
EV_MODULES, EV_MODULES_N, EV_IS_FRAME, EV_TYPE_OBJ = 0xB8, 0xC4, 0x41E, 0x3F0
XP_STATUS, XP_ARMY, XP_NAVY, XP_AIR = 0x1528, 0x10, 0x28, 0x40


# Experience is fixed point in 1/32768 (2^15), not 1/100000 like most of the game's values:
# read as /1e5 the army had 66 and the air force 84 while the game showed 202 and 257
# (20.10; an air doctrine then took exactly 100 off, 257.6 -> 157.6). Every XP figure
# the staffs saw before was a third of the real one.
XP_SCALE = 32768


def experience(g, tag=None):
    p = g.p
    x = p.u64(g._c(tag).ptr + XP_STATUS)
    return {k: round(p.i64(x + o) / XP_SCALE, 1) for k, o in
            (("army", XP_ARMY), ("navy", XP_NAVY), ("air", XP_AIR))}


def modules(g, v):
    import game as G
    p = g.p
    arr, n = p.u64(v + EV_MODULES), p.i32(v + EV_MODULES_N)
    return [G.item_name(p, p.u64(arr + 16 * k + 8) + 0x10) for k in range(n)]


def _family(eq):
    import re
    m = re.match(r"(.*?)_(\d+)$", eq)
    return (m.group(1), int(m.group(2))) if m else (eq, 0)


def newer_frames(g, tag=None):
    """[(design id, design name, its equipment, frame id, frame equipment)]: a newer frame
    of the same family exists with no design of ours on it."""
    vs = variants(g, tag)
    p = g.p
    frames = [v for v in vs if p.u8(v["ptr"] + EV_IS_FRAME)]
    designed = {v["equipment"] for v in vs if not p.u8(v["ptr"] + EV_IS_FRAME)}
    out = []
    for d in vs:
        if p.u8(d["ptr"] + EV_IS_FRAME) or not d["design"]:
            continue
        fam, n = _family(d["equipment"])
        for f in frames:
            ff, fn = _family(f["equipment"])
            if ff == fam and fn > n and f["equipment"] not in designed:
                out.append((d["id"], d["design"], d["equipment"], f["id"], f["equipment"]))
    return out


def new_design(g, design_id, frame_id, name, tag=None):
    """Create a design: the modules of `design_id` on the newer frame `frame_id`."""
    import api
    p = g.p
    vs = {v["id"]: v for v in variants(g, tag)}
    if design_id not in vs or frame_id not in vs:
        return {"ok": False, "error": "unknown design or frame id"}
    src, frame = vs[design_id]["ptr"], vs[frame_id]["ptr"]
    if not p.u8(frame + EV_IS_FRAME):
        return {"ok": False, "error": "%d is not a frame (an empty base model)" % frame_id}
    s = g.malloc(0x600)
    p.write(s, p.read(src, 0x600))
    p.write(s + EV_TYPE_ID, struct.pack("<i", p.i32(frame + EV_TYPE_ID)))
    p.write(s + EV_TYPE_OBJ, struct.pack("<Q", p.u64(frame + EV_TYPE_OBJ)))
    b = name.encode("utf-8")[:15]
    nm = g.malloc(0x20)
    p.write(nm, struct.pack("<QQ", nm + 0x10, len(b)) + b + b"\0" * (16 - len(b)))
    cmd = g.malloc(DESIGN_SIZE)
    p.write(cmd, b"\0" * DESIGN_SIZE)
    before = {v["id"] for v in vs.values()}
    xp0 = experience(g, tag)
    own = g._inj is None
    if own:
        g.attach()
    try:
        for f in (DESIGN_COST, DESIGN_CTOR):
            api.allow_call(f)
        cost = g.call(DESIGN_COST, [s, frame, s, 0], signed=True)
        g.call(DESIGN_CTOR, [cmd, s, frame, cost, nm, 0, 0])
        if not can_execute(g, cmd):
            return {"ok": False, "error": "the game refused the design (cost %.1f experience; "
                    "we have %s; or a module does not fit the new frame)" % (cost / 1e5, xp0)}
        g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    import time
    time.sleep(0.5)
    new = [v for v in variants(g, tag) if v["id"] not in before]
    if not new:
        return {"ok": False, "error": "no design appeared"}
    return {"ok": True, "id": new[0]["id"], "equipment": new[0]["equipment"],
            "design": new[0]["design"], "modules": modules(g, new[0]["ptr"]),
            "experience_before": xp0, "experience_after": experience(g, tag)}


def _module_ptrs(g):
    import game as G
    out = {}
    for m in g.oi.instances("CEquipmentModule"):
        try:
            out[G.item_name(g.p, m + 0x10)] = m
        except Exception:
            pass
    return out


def frame_slots(g, frame_ptr):
    """[(slot name, token)] of a frame's type, in the type's order."""
    import fieldmap
    p = g.p
    ty = p.u64(frame_ptr + EV_TYPE_OBJ)
    arr, n = p.u64(ty + 0x68), p.i32(ty + 0x74)
    return [(fieldmap.token_name(g, p.i32(arr + 0x48 * k + 8)), p.i32(arr + 0x48 * k + 8))
            for k in range(n)]


SLOT_REQUIRED = 0xC      # u8 in the type's slot entry (0x48 bytes): 'required = yes' (26.10)


def required_slots(g, variant_ptr):
    """Slot names the variant's type marks required (inherited from its archetype)."""
    import fieldmap
    p = g.p
    ty = p.u64(variant_ptr + EV_TYPE_OBJ)
    arr, n = p.u64(ty + 0x68), p.i32(ty + 0x74)
    return [fieldmap.token_name(g, p.i32(arr + 0x48 * k + 8)) for k in range(n)
            if p.u8(arr + 0x48 * k + SLOT_REQUIRED)]


def slot_modules(g, v):
    """{slot name: module name} of a design."""
    import fieldmap
    import game as G
    p = g.p
    arr, n = p.u64(v + EV_MODULES), p.i32(v + EV_MODULES_N)
    return {fieldmap.token_name(g, p.i32(arr + 16 * k)):
            G.item_name(p, p.u64(arr + 16 * k + 8) + 0x10) for k in range(max(0, min(n, 64)))}


def missing_required(g, v):
    """Required slots a design leaves empty (26.10: a 'Hurricane' with an engine and no
    gun was made and built; the game's own checks let both pass)."""
    have = slot_modules(g, v)
    return [s for s in required_slots(g, v) if not have.get(s)]


def design_from_modules(g, frame_id, mods, name, tag=None, dry_run=False,
                        check_required=True):
    """Create a design on a frame with the given modules {slot name: module name}
    (a preset from common/ai_equipment, or any). The modules array of the working copy
    is CPdxArray<{i32 slot token; CEquipmentModule*}> at +0xB8 (19.10)."""
    import api
    p = g.p
    vs = {v["id"]: v for v in variants(g, tag)}
    if frame_id not in vs or not p.u8(vs[frame_id]["ptr"] + EV_IS_FRAME):
        return {"ok": False, "error": "%s is not one of our frames" % frame_id}
    frame = vs[frame_id]["ptr"]
    slots = dict(frame_slots(g, frame))
    ptrs = _module_ptrs(g)
    ents = []
    for slot, mod in mods.items():
        if not mod or mod == "empty":
            continue
        if slot not in slots:
            return {"ok": False, "error": "the frame has no slot %s (it has %s)" % (slot, ", ".join(slots))}
        if mod not in ptrs:
            return {"ok": False, "error": "no module called %s" % mod}
        ents.append(struct.pack("<iiQ", slots[slot], 0, ptrs[mod]))
    filled = {sl for sl, md in mods.items() if md and md != "empty"}
    miss = [sl for sl in required_slots(g, frame) if sl not in filled] if check_required else []
    if miss:
        return {"ok": False, "error": "required slots left empty: %s" % ", ".join(miss)}
    s = g.malloc(0x600)
    p.write(s, p.read(frame, 0x600))
    data = g.malloc(max(1, len(ents)) * 16)
    p.write(data, b"".join(ents))
    p.write(s + EV_MODULES, struct.pack("<Qii", data, len(ents), len(ents)))
    b = name.encode("utf-8")[:15]
    nm = g.malloc(0x20)
    p.write(nm, struct.pack("<QQ", nm + 0x10, len(b)) + b + b"\0" * (16 - len(b)))
    cmd = g.malloc(DESIGN_SIZE)
    p.write(cmd, b"\0" * DESIGN_SIZE)
    before = set(vs)
    xp0 = experience(g, tag)
    own = g._inj is None
    if own:
        g.attach()
    try:
        for f in (DESIGN_COST, DESIGN_CTOR):
            api.allow_call(f)
        cost = g.call(DESIGN_COST, [s, frame, s, 0], signed=True)
        g.call(DESIGN_CTOR, [cmd, s, frame, cost, nm, 0, 0])
        if not can_execute(g, cmd):
            return {"ok": False, "error": "the game refused the design (cost %.1f experience, "
                    "we have %s; or a module is not researched yet or does not fit)"
                    % (cost / 1e5, xp0)}
        if dry_run:
            return {"ok": True, "dry_run": True, "cost": cost / 1e5}
        g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    import time
    time.sleep(0.5)
    new = [v for v in variants(g, tag) if v["id"] not in before]
    if not new:
        return {"ok": False, "error": "no design appeared"}
    return {"ok": True, "id": new[0]["id"], "equipment": new[0]["equipment"],
            "design": new[0]["design"], "modules": modules(g, new[0]["ptr"]),
            "experience_before": xp0, "experience_after": experience(g, tag)}


def outdated(g, tag=None):
    """{design id: the newer design of the same family} for designs on an older frame."""
    vs = [v for v in variants(g, tag) if not v["frame"] and v["design"]]
    out = {}
    for v in vs:
        fam, n = _family(v["equipment"])
        newer = [w for w in vs if _family(w["equipment"])[0] == fam and _family(w["equipment"])[1] > n]
        if newer:
            out[v["id"]] = max(newer, key=lambda w: _family(w["equipment"])[1])["design"]
    return out
