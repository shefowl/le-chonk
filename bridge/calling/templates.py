"""Division templates: create one or change one, as the division designer does (19.10).

  CReferencedDivisionTemplate: +0x08 handle, +0x18 CDivisionTemplateData (0x248):
    +0x08 std::string name
    +0x50 CPdxArray<CSubUnitDefinition*> the battalion grid, 25 slots, column-major
          (slot = column * 5 + row), size = the last used slot + 1
    +0x70 CPdxArray<CSubUnitDefinition*> support companies, 5 slots
    +0xA8/+0xC0/+0xD8 CPdxArray<{CSubUnitDefinition*, i32 count, pad}>: every type, the
          combat types, the support types (caches, rebuilt here)
  CUpdateDivisionTemplateCommand (0x288): ctor 0x1FDAE70 (this, data*, xp cost, tag*, template)
  CCreateDivisionTemplateCommand (0x288): ctor 0x1FDC190 (this, data*, xp cost, tag*, 0)
  The XP cost is the designer's own sum (0x1FDFDC0): grid 0x1FE1180(old+0x48, new+0x48, tag)
  + support 0x1FE1740(old+0x68, new+0x68, tag) + 0x1FE1810(0x1FD47D0(old), 0x1FD47D0(new),
  tag); CanExecute checks it against army experience.
"""
import collections
import struct

import game as G

GRID_COST, SUP_COST, EXTRA, EXTRA_COST = 0x1FE1180, 0x1FE1740, 0x1FD47D0, 0x1FE1810
UPD_CTOR, NEW_CTOR, CMD_SIZE, DATA_SIZE = 0x1FDAE70, 0x1FDC190, 0x288, 0x248
T_DATA = 0x18
D_NAME, D_GRID, D_SUPPORT, D_ALL, D_COMBAT, D_SUP_TYPES = 0x08, 0x50, 0x70, 0xA8, 0xC0, 0xD8


def subunit_defs(g):
    """{battalion key: our country's own CSubUnitDefinition*} (each country has its copies;
    a template holds them, not the database's)."""
    out = {}
    c = g._c(None).ptr
    for s in G._arr(g.p, c, G.subunit_off(g.p, c)):
        k = G.item_name(g.p, s + G.SU_KEY)
        if k:
            out[k] = s
    return out


def _null(g):
    """The empty slot: a TNullObject<CSubUnitDefinition>, as the grid holds it."""
    for t in g.division_templates():
        d = int(t["ptr"], 16) + T_DATA
        data, n = g.p.u64(d + D_GRID), g.p.i32(d + D_GRID + 0xC)
        for s in struct.unpack("<%dQ" % n, g.p.read(data, 8 * n)) if n else ():
            if g.oi.class_of(s) and g.oi.class_of(s).startswith("TNullObject"):
                return s
    raise RuntimeError("no empty grid slot found to copy")


def _template(g, name):
    for t in g.division_templates():
        if t["name"] == name:
            return int(t["ptr"], 16)
    return None


def read(g, name):
    """{grid: [[battalion keys of column 0], ...], support: [keys]}"""
    p = g.p
    t = _template(g, name)
    if t is None:
        return None
    d = t + T_DATA
    data, n = p.u64(d + D_GRID), p.i32(d + D_GRID + 0xC)
    slots = struct.unpack("<%dQ" % n, p.read(data, 8 * n)) if n else ()
    cols = [[] for _ in range(5)]
    for i, s in enumerate(slots):
        k = G.item_name(p, s + G.SU_KEY) if s else None
        if k:
            cols[i // 5].append(k)
    sdata, sn = p.u64(d + D_SUPPORT), p.i32(d + D_SUPPORT + 0xC)
    sup = [G.item_name(p, s + G.SU_KEY) for s in
           (struct.unpack("<%dQ" % sn, p.read(sdata, 8 * sn)) if sn else ()) if s]
    return {"grid": [c for c in cols if c], "support": sup}


def _array(g, ptrs, cap):
    data = g.malloc(8 * max(cap, 1))
    g.p.write(data, struct.pack("<%dQ" % cap, *(list(ptrs) + [0] * (cap - len(ptrs)))))
    return data


def _counts(g, d, off, items):
    c = collections.Counter(items)
    order = list(dict.fromkeys(items))
    data = g.malloc(16 * max(len(order), 1))
    g.p.write(data, b"".join(struct.pack("<QiI", s, c[s], 0) for s in order))
    g.p.write(d + off, struct.pack("<Qii", data, len(order), len(order)))


def build(g, base_name, grid, support, name=None):
    """A new CDivisionTemplateData: a copy of base_name's with this grid and support
    (lists of battalion keys; grid as columns of up to 5)."""
    p = g.p
    t = _template(g, base_name)
    if t is None:
        raise KeyError("no template %r" % base_name)
    defs = subunit_defs(g)
    if len(grid) > 5 or any(len(c) > 5 for c in grid) or len(support) > 5:
        raise ValueError("at most 5 columns of 5 battalions, and 5 support companies")
    unknown = [k for k in [x for c in grid for x in c] + list(support) if k not in defs]
    if unknown:
        raise KeyError("unknown battalion types: %s" % ", ".join(unknown))
    new = g.malloc(DATA_SIZE)
    p.write(new, p.read(t + T_DATA, DATA_SIZE))
    null = _null(g)
    slots = [null] * 25
    for ci, col in enumerate(grid):
        for ri, k in enumerate(col):
            slots[ci * 5 + ri] = defs[k]
    n = max([i + 1 for i, s in enumerate(slots) if s != null] or [0])
    p.write(new + D_GRID, struct.pack("<Qii", _array(g, slots, 25), 25, n))
    sp = [defs[k] for k in support]
    p.write(new + D_SUPPORT, struct.pack("<Qii", _array(g, sp, 5), 5, len(sp)))
    combat = [s for s in slots[:n] if s != null]
    _counts(g, new, D_ALL, combat + sp)
    _counts(g, new, D_COMBAT, combat)
    _counts(g, new, D_SUP_TYPES, sp)
    if name:
        b = name.encode("utf-8")
        buf = g.malloc(len(b) + 1)
        p.write(buf, b + b"\0")
        p.write(new + D_NAME, struct.pack("<QQ", buf, len(b)))
    return t, new


def _send(g, ctor, args, dry_run):
    import api
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        for f in (GRID_COST, SUP_COST, EXTRA, EXTRA_COST, ctor):
            api.allow_call(f)
        old, new = args["old"], args["new"]
        tag = g.malloc(8)
        p.write(tag, struct.pack("<i", g.player_tag_id()))
        cost = (g.call(GRID_COST, [old + 0x48, new + 0x48, tag], signed=True)
                + g.call(SUP_COST, [old + 0x68, new + 0x68, tag], signed=True)
                + g.call(EXTRA_COST, [g.call(EXTRA, [old]), g.call(EXTRA, [new]), tag], signed=True))
        cmd = g.malloc(CMD_SIZE)
        p.write(cmd, b"\0" * CMD_SIZE)
        g.call(ctor, [cmd, new, cost, tag] + args.get("extra", []))
        can = p.u64(p.u64(cmd) + 8 * 10) - p.base
        api.allow_call(can)
        ok = g.call(can, [cmd, 0]) & 0xFF
        xp = p.i64(p.u64(g.player().ptr + 0x1528) + 0x10) / 1e5
        if not ok:
            return {"ok": False, "cost": cost / 1e5, "army_experience": round(xp, 1),
                    "error": "the game refused it (cost %.0f army experience, we have %.0f; or "
                             "the template is not valid)" % (cost / 1e5, xp)}
        if not dry_run:
            g.cmd.post(cmd)
        return {"ok": True, "cost": cost / 1e5, "army_experience_before": round(xp, 1)}
    finally:
        if own:
            g.detach()


def update(g, name, grid, support, dry_run=False):
    t, new = build(g, name, grid, support)
    return _send(g, UPD_CTOR, {"old": t + T_DATA, "new": new, "extra": [t]}, dry_run)


def create(g, base_name, new_name, grid, support, dry_run=False):
    if _template(g, new_name) is not None:
        return {"ok": False, "error": "a template called %r exists" % new_name}
    t, new = build(g, base_name, grid, support, new_name)
    # 1.19.4 added an optional 4th argument (an object whose handle goes to +0x274; NULL
    # takes the default): pass 0, or r8 keeps whatever the hijacked thread had there
    return _send(g, NEW_CTOR, {"old": t + T_DATA, "new": new, "extra": [0]}, dry_run)


REMOVE_CTOR = 0x1FDD4A0     # CRemoveDivisionTemplateCommand (this, template): +0x24 its handle


def remove(g, name):
    import api
    p = g.p
    t = _template(g, name)
    if t is None:
        return {"ok": False, "error": "no template %r" % name}
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd = g.malloc(0x40)
        p.write(cmd, b"\0" * 0x40)
        api.allow_call(REMOVE_CTOR)
        g.call(REMOVE_CTOR, [cmd, t])
        can = p.u64(p.u64(cmd) + 8 * 10) - p.base
        api.allow_call(can)
        if not g.call(can, [cmd, 0]) & 0xFF:
            return {"ok": False, "error": "the game will not remove it (divisions still use it?)"}
        g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    return {"ok": True}
