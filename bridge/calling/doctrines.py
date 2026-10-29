"""Doctrines (the 1.19 system: folders land/naval/air/special forces, one grand doctrine
each, tracks holding subdoctrines). Found 19.10.

  CCountryDoctrineStatus: one per country (instances of its class; +0x08 tag id),
    +0x10 CPdxArray<CFolderStatus, 0x50 each>:
      +0x08 folder template, +0x10 grand doctrine template (0 = none),
      +0x18 CPdxArray<CTrackStatus, 0x60 each> (+0x08 subdoctrine template, 0 = none)
  CGrandDoctrineTemplate / CSubDoctrineTemplate: +0x20 name (the loc key, e.g.
    GRAND_DOCTRINE_GRAND_BATTLEPLAN); grand +0x2F8 CPdxArray<CTrackTemplate*> (its tracks)
  CUnlockGrandDoctrineCommand (0x38): ctor 0xB231B0 (this, tag*, grand)
  CUnlockSubDoctrineCommand (0x40): ctor 0xB23C80 (this, tag*, track index, sub, folder)
    -> +0x28 folder, +0x30 sub, +0x38 index in the grand doctrine's tracks, +0x3C tag
  Both CanExecute (vtable slot 10) write the reason into a std::string.
"""
import struct

import game as G

GRAND_CTOR, GRAND_SIZE = 0xB231B0, 0x40
SUB_CTOR, SUB_SIZE = 0xB23C80, 0x48
FOLDER_SIZE, TRACK_SIZE = 0x50, 0x60
FOLDERS = ("land", "naval", "air", "special_forces")     # the order of the folder array


def _inst(g, key):
    m = g.oi.build()
    out = set()
    for v in g.oi.rtti.get(key, []):
        out.update(m.get(g.p.va(v), []))
    return out


def templates(g):
    """({loc key: grand ptr}, {loc key: sub ptr})"""
    p = g.p
    gd = {G.item_name(p, x + 0x20): x for x in _inst(g, "N10NDoctrines22CGrandDoctrineTemplateE")}
    sd = {G.item_name(p, x + 0x20): x for x in _inst(g, "N10NDoctrines20CSubDoctrineTemplateE")}
    return gd, sd


def _status(g):
    tid = g.player_tag_id()
    for x in _inst(g, "N10NDoctrines22CCountryDoctrineStatusE"):
        if g.p.i32(x + 8) == tid:
            return x
    raise RuntimeError("no doctrine status for our country")


def status(g):
    """[{folder, folder_ptr, grand (loc key or None), tracks: [sub loc key or None]}]"""
    p = g.p
    st = _status(g)
    arr, n = p.u64(st + 0x10), p.i32(st + 0x1C)
    out = []
    for k in range(n):
        f = arr + FOLDER_SIZE * k
        gd = p.u64(f + 0x10)
        ta, tn = p.u64(f + 0x18), p.i32(f + 0x24)
        subs = []
        for i in range(tn):
            s = p.u64(ta + TRACK_SIZE * i + 0x08)
            subs.append(G.item_name(p, s + 0x20) if s else None)
        out.append({"folder": FOLDERS[k] if k < len(FOLDERS) else str(k),
                    "folder_ptr": p.u64(f + 0x08),
                    "grand": G.item_name(p, gd + 0x20) if gd else None, "grand_ptr": gd,
                    "tracks": subs})
    return out


def _cmd(g, ctor, size, args):
    import api
    p = g.p
    tag = g.malloc(8)
    p.write(tag, struct.pack("<i", g.player_tag_id()))
    cmd = g.malloc(size)
    p.write(cmd, b"\0" * size)
    s = g.malloc(0x40)
    p.write(s, struct.pack("<QQ", s + 0x10, 0) + b"\0" * 0x30)
    api.allow_call(ctor)
    g.call(ctor, [cmd, tag] + list(args))
    can = p.u64(p.u64(cmd) + 8 * 10) - p.base
    api.allow_call(can)
    ok = g.call(can, [cmd, s]) & 0xFF
    ptr, ln = struct.unpack("<QQ", p.read(s, 16))
    why = p.read(ptr, min(ln, 400)).decode("utf8", "replace").replace("#~", "").strip()
    return cmd, bool(ok), why


def _post(g, ctor, size, args, dry_run):
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd, ok, why = _cmd(g, ctor, size, args)
        if not ok:
            return {"ok": False, "error": why or "the game refused it"}
        if not dry_run:
            g.cmd.post(cmd)
        return {"ok": True}
    finally:
        if own:
            g.detach()


def unlock_grand(g, loc_key, dry_run=False):
    gd, _ = templates(g)
    if loc_key not in gd:
        return {"ok": False, "error": "no grand doctrine %s" % loc_key}
    return _post(g, GRAND_CTOR, GRAND_SIZE, [gd[loc_key]], dry_run)


def unlock_sub(g, folder, track_index, loc_key, dry_run=False):
    _, sd = templates(g)
    if loc_key not in sd:
        return {"ok": False, "error": "no subdoctrine %s" % loc_key}
    f = next((x for x in status(g) if x["folder"] == folder), None)
    if f is None or not f["grand"]:
        return {"ok": False, "error": "the %s folder has no grand doctrine yet" % folder}
    return _post(g, SUB_CTOR, SUB_SIZE, [int(track_index), sd[loc_key], f["folder_ptr"]], dry_run)


# What a doctrine costs us (20.10, from the unlock check 0xB0CB90): the template's xp cost
# (i32 +0x18) times 1 + the folder's cost modifier (its id at folder +0xF8, on the country)
# + the folder's one-off reductions with uses left (the doctrine status +0x40: 0x40-byte
# entries, +0x08 uses, +0x30 amount x1e5, +0x38 folder). France on 17.12.1939: land
# subdoctrines 100 x (1 + 0.5 victors of WWI - 0.65 a focus, one use) = 85.
FOLDER_COST_MODIFIER, ST_REDUCTIONS = 0xF8, 0x40
RED_SIZE, RED_USES, RED_AMOUNT, RED_FOLDER = 0x40, 0x08, 0x30, 0x38


def cost(g, folder, template):
    p = g.p
    f = next((x for x in status(g) if x["folder"] == folder), None)
    if f is None:
        return None
    fp = f["folder_ptr"]
    mid = p.i32(fp + FOLDER_COST_MODIFIER)
    factor = 1 + (g.player().modifier(mid) if mid > 0 else 0.0)
    st = _status(g)
    arr, n = p.u64(st + ST_REDUCTIONS), p.i32(st + ST_REDUCTIONS + 0xC)
    for i in range(n if 0 <= n < 256 else 0):
        e = arr + RED_SIZE * i
        if p.u64(e + RED_FOLDER) == fp and p.i32(e + RED_USES) > 0:
            factor += p.i64(e + RED_AMOUNT) / 1e5
    return round(p.i32(template + 0x18) * max(factor, 0.0), 1)
