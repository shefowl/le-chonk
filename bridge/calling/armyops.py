"""Armies, army groups and commanders, through the player command channel.

What this corresponds to in the UI:
  * Select divisions -> the "+" button     => COrderGroupCommand  (new ARMY)
  * Select armies    -> the "+" button     => CArmyGroupCommand  (new ARMY GROUP)
  * Drag a general onto the portrait       => CSetArmyLeaderCommand

Important: a command field always takes THAT OBJECT'S OWN handle.
  CArmy         +0x18  -> {51,  id}
  COrdersGroup  +0x08  -> {53,  id}
  CTheatre      +0x08  -> {67,  id}
  CCharacter    +0x08  -> {73,  id}
  CArmyLeader   +0x08  -> {4713,id}     <-- assigning a general wants THIS, not CCharacter
"""
import struct

ALLOC = 0x4505128

# --- COrderGroupCommand (new army) ----------------------------------------
OG_CTOR   = 0x23151c0   # (this, CPdxArray<CArmy*>*, CTheatre*, CArmyGroup*|0)
OG_CANEXE = 0x2315A70   # bool (this, std::string& err)
OG_SIZE   = 0x58
# --- CArmyGroupCommand (new army group) -----------------------------------
AG_CTOR   = 0x23167D0   # (this, CPdxArray<COrdersGroup*>*, CTheatre*)
AG_CANEXE = 0x23175B0   # bool (this)
AG_SIZE   = 0x80
# --- CSetArmyLeaderCommand ------------------------------------------------
SL_CTOR   = 0x2332940   # (this, CArmyLeader*, COrdersGroup*, u8 flag)
SL_CANEXE = 0x2332D80   # bool (this)
SL_SIZE   = 0x38

FN_RESOLVE = 0x2ee2d70  # handle* -> object*

# COrdersGroup fields
OG_HANDLE      = 0x08
OG_THEATRE     = 0x40
OG_DIVISIONS   = 0x50   # CPdxArray<CArmy*>
OG_LEADER      = 0x88   # CArmyLeader*
OG_ORDERS      = 0x98   # CPdxArray<COrderInstance*>
OG_NAME        = 0x160  # std::string
OG_IS_GROUP    = 0x39   # 0 = army, non-zero = army group
OG_PARENT      = 0x1b8  # parent army group (COrdersGroup*, 0 if unattached)
OG_MEMBERS     = 0x228  # armies inside the group: CPdxArray<COrdersGroup*>
OG_NOGROUP_CHK = 0x1b0  # the UI's check for whether a group can be created
CARMY_OG       = 0xC0   # CArmy -> COrdersGroup*


# ------------------------------------------------------------------ helpers
def _mkarray(g, ptrs):
    p = g.p
    data = g.malloc(max(1, len(ptrs)) * 8)
    p.write(data, b"".join(struct.pack("<Q", x) for x in ptrs))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, len(ptrs), len(ptrs), p.base + ALLOC))
    return hdr


def _mkstring(g):
    s = g.malloc(0x20)
    g.p.write(s, struct.pack("<QQ", s + 0x10, 0) + b"\0" * 16)
    return s


def _readstring(g, s):
    ln = g.p.u64(s + 8)
    return g.p.read(g.p.u64(s), ln).decode("utf8", "replace") if ln else ""


def resolve_handle(g, type_id, obj_id):
    h = g.malloc(8)
    g.p.write(h, struct.pack("<ii", type_id, obj_id))
    return g.call(FN_RESOLVE, [h])


def check_field_handle(g, cmd, off, expect_ptr, what):
    """Does the handle in the command resolve to the expected object? Crash guard."""
    t, i = struct.unpack("<ii", g.p.read(cmd + off, 8))
    got = resolve_handle(g, t, i)
    if got != expect_ptr:
        raise RuntimeError(
            "%s: +0x%x handle {%d,%d} -> 0x%x, beklenen 0x%x" % (what, off, t, i, got, expect_ptr))
    return t, i


# ------------------------------------------------------------------ reads
def orders_groups(g, country=None):
    """The country's armies (COrdersGroup), collected through its divisions."""
    from game import _arr, item_name
    p = g.p
    co = country.ptr if country is not None else g.player().ptr
    seen, out = set(), []
    for d in _arr(p, co, 0x290):
        og = p.u64(d + CARMY_OG)
        if og and og not in seen:
            seen.add(og)
            out.append(og)
    return out


def og_info(g, og):
    p = g.p
    ldr = p.u64(og + OG_LEADER)
    name = ""
    try:
        name = p.stdstring(og + OG_NAME)
    except Exception:
        pass
    return {
        "ptr": hex(og),
        "handle": list(struct.unpack("<ii", p.read(og + OG_HANDLE, 8))),
        "name": name,
        "is_army_group": bool(p.u8(og + OG_IS_GROUP)),
        "divisions": p.i32(og + OG_DIVISIONS + 0xC),
        "leader_ptr": hex(ldr) if ldr else None,
        "leader": (p.cstr(p.u64(ldr + 0x40)) if ldr else None),
        "parent_group": hex(p.u64(og + OG_PARENT)) if p.u64(og + OG_PARENT) else None,
        "member_armies": (p.i32(og + OG_MEMBERS + 0xC) if p.u8(og + OG_IS_GROUP) else None),
        "orders": p.i32(og + OG_ORDERS + 0xC),
    }


# ------------------------------------------------------------------ actions
def create_army(g, divisions, theatre=None):
    """Collect the given divisions (CArmy*) into a new army."""
    from game import _arr
    if theatre is None:
        theatre = _arr(g.p, g.player().ptr, 0x168)[0]
    own = g._inj is None
    if own: g.attach()
    try:
        arr = _mkarray(g, list(divisions))
        cmd = g.malloc(OG_SIZE); g.p.write(cmd, b"\0" * OG_SIZE)
        g.call(OG_CTOR, [cmd, arr, theatre, 0])
        check_field_handle(g, cmd, 0x40, theatre, "create_army/theatre")
        s = _mkstring(g)
        ok = g.call(OG_CANEXE, [cmd, s]) & 0xFF
        if not ok:
            return False, _readstring(g, s) or "command rejected"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def create_army_group(g, armies, theatre=None):
    """Collect the given armies (COrdersGroup*) into a new army group."""
    from game import _arr
    p = g.p
    if theatre is None:
        theatre = _arr(p, g.player().ptr, 0x168)[0]
    for og in armies:
        if p.u8(og + OG_IS_GROUP):
            return False, "0x%x is already an army group" % og
        if p.u64(og + OG_PARENT):
            return False, "0x%x is already inside an army group" % og
        if p.u64(og + OG_NOGROUP_CHK):
            return False, "0x%x gruplanamaz" % og
    own = g._inj is None
    if own: g.attach()
    try:
        arr = _mkarray(g, list(armies))
        cmd = g.malloc(AG_SIZE); g.p.write(cmd, b"\0" * AG_SIZE)
        g.call(AG_CTOR, [cmd, arr, theatre])
        check_field_handle(g, cmd, 0x24, theatre, "create_army_group/theatre")
        n = p.i32(cmd + 0x3C)
        if n != len(armies):
            return False, "array was not filled (%d/%d)" % (n, len(armies))
        base = p.u64(cmd + 0x30)
        for k, og in enumerate(armies):
            t, i = struct.unpack("<ii", p.read(base + k * 8, 8))
            got = resolve_handle(g, t, i)
            if got != og:
                return False, "army handle mismatch: {%d,%d}->0x%x != 0x%x" % (t, i, got, og)
        ok = g.call(AG_CANEXE, [cmd]) & 0xFF
        if not ok:
            return False, "CanExecute false"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def set_army_leader(g, orders_group, leader, flag=0):
    """Assign a commander to an army or army group.

    leader: CArmyLeader* (CCharacter+0xC0) or CCharacter*, converted automatically.
            Passing 0 removes the commander.
    """
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        lp = leader
        if lp:
            t = p.i32(lp + 8)
            if t == 73:                 # given a CCharacter, move to its CArmyLeader
                lp = p.u64(lp + 0xC0)
                if not lp:
                    return False, "this character has no CArmyLeader"
            if p.i32(lp + 8) != 4713:
                return False, "0x%x is not a CArmyLeader (handle type %d)" % (lp, p.i32(lp + 8))
        cmd = g.malloc(SL_SIZE); p.write(cmd, b"\0" * SL_SIZE)
        g.call(SL_CTOR, [cmd, lp, orders_group, flag])
        if lp:
            check_field_handle(g, cmd, 0x24, lp, "set_army_leader/leader")
        check_field_handle(g, cmd, 0x2C, orders_group, "set_army_leader/army")
        ok = g.call(SL_CANEXE, [cmd]) & 0xFF
        if not ok:
            return False, "CanExecute false (already assigned, or not eligible)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ general
AL_ATTACK    = 0xE58
AL_DEFENSE   = 0xE68
AL_PLANNING  = 0xE78
AL_LOGISTICS = 0xE88
AL_NAME      = 0x40
AL_TAG       = 0x120


def leader_skills(g, army_leader):
    """A CArmyLeader's effective skills, trait bonuses included."""
    p = g.p
    return {
        "attack":    p.i32(army_leader + AL_ATTACK),
        "defense":   p.i32(army_leader + AL_DEFENSE),
        "planning":  p.i32(army_leader + AL_PLANNING),
        "logistics": p.i32(army_leader + AL_LOGISTICS),
    }


# ================================================================== BATTLE PLANS
# COrderNewRootCommand  = give a front (CFront) a "line" order.
#   ctor(this, COrdersGroup* og, void* front_handle_ptr, i32 section,
#        i64 from, i64 to, CPdxArray<CArmy*>* divisions|0, u8 flag)
#   fields:  +0x24 og handle | +0x30 array | +0x48 root_front handle
#             +0x50 root_section | +0x58 from | +0x60 to | +0x6c flag
#   from/to  : 0..100000 fixed point (which slice of the front)
NR_CTOR   = 0x2326180
NR_CANEXE = 0x2326980
NR_SIZE   = 0x70
# COrderExecuteCommand
OE_CTOR   = 0x2332310
OE_CANEXE = 0x23326B0
OE_SIZE   = 0x38

FRONT_HANDLE   = 0x08
FRONT_PROVS    = 0x20     # CPdxArray<CProvince*>
FRONT_SECTIONS = 0x58     # CPdxArray<CFrontSection*>
SEC_PROVS      = 0x30
SEC_ID         = 0x08     # NOTE: the section ID, not its index in the array
PROV_ID        = 0xA4
PROV_STATE     = 0xE0
FULL = 100000


FRONT_TYPE = 66        # handle type of CFront


def handle_objects(g, type_id):
    """{id: object} of every live object with this handle type, read from the game's own
    handle table (what Resolve looks up). Replaces a heap scan for anything the game
    creates and frees during play: 1 ms instead of ~3 s of reading gigabytes, which
    stalled the game while it ran."""
    import offsets as O
    if not 0 <= type_id < 100:
        raise ValueError("only handle types below 100 live in the table array")
    p = g.p
    t = p.u64(g.va(O.G_HANDLE_TABLES) + type_id * 8)
    if not t:
        return {}
    n = p.i32(t + 0xC) + 1 + p.u8(t + 0x10)
    raw = p.read(p.u64(t), n * 24)
    out = {}
    for i in range(n):
        if raw[i * 24 + 4] == 0:
            continue
        ty, oid, obj = struct.unpack_from("<iiQ", raw, i * 24 + 8)
        if ty == type_id and obj:
            out[oid] = obj
    return out


def fronts_towards(g, enemy_tag, own_tag=None):
    """Front sections FACING enemy_tag.

    A front section can touch more than one neighbour (a Silesian section may have
    province bordering Czechoslovakia too), so a section is assigned to whichever
    assigned to whichever country the majority of them face; otherwise a line drawn
    against Poland would also appear on the Czechoslovak border.
    """
    from game import _arr
    from collections import Counter
    import mapdata
    p = g.p
    if own_tag is None:
        own_tag = g.player().tag
    # our subjects' land is our side too (23.10: Japan's front with Hebei-Chahar runs
    # through Manchukuo's Jehol; with JAP alone no section faced HBC at all)
    try:
        import combat
        ours = {own_tag} | set(combat.subject_tags(g, own_tag))
    except Exception:
        ours = {own_tag}
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()

    def faces(pid):
        """The FOREIGN countries neighbouring this province."""
        out = Counter()
        for n in adj.get(pid, ()):
            t = owners.get(n)
            if t and t not in ours and ptype.get(n) == "land":
                out[t] += 1
        return out

    out = []
    for f in handle_objects(g, FRONT_TYPE).values():    # live: fronts are re-created
        try:
            secs = _arr(p, f, FRONT_SECTIONS)
        except Exception:
            continue
        for s in secs:
            try:
                ids = [p.i32(x + PROV_ID) for x in _arr(p, s, SEC_PROVS)
                       if g.oi.class_of(x) == "CProvince"]
            except Exception:
                continue
            ids = [pid for pid in ids if owners.get(pid) in ours]   # our provinces only
            if not ids:
                continue
            tally = Counter()
            mine = []
            for pid in ids:
                fc = faces(pid)
                for t in fc:
                    tally[t] += 1
                if enemy_tag in fc:
                    mine.append(pid)
            if not mine:
                continue
            top = tally.most_common(1)[0]
            if top[0] != enemy_tag:      # this section faces a different country
                continue
            out.append({"front": f,
                        "front_handle": [p.i32(f + 8), p.i32(f + 0xC)],
                        "section": p.i32(s + SEC_ID),
                        "section_index": secs.index(s),
                        "border_provinces": sorted(mine),
                        "faces": dict(tally)})
    return out


def draw_front_line(g, orders_group, front, section, frm=0, to=FULL, divisions=None, flag=0):
    """Give an army or army group a line order along one front section."""
    import struct
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        hp = g.malloc(8)
        p.write(hp, p.read(front + FRONT_HANDLE, 8))
        arr = _mkarray(g, list(divisions)) if divisions else 0
        cmd = g.malloc(NR_SIZE); p.write(cmd, b"\0" * NR_SIZE)
        g.call(NR_CTOR, [cmd, orders_group, hp, section, frm, to, arr, flag])
        check_field_handle(g, cmd, 0x24, orders_group, "draw_front_line/og")
        check_field_handle(g, cmd, 0x48, front, "draw_front_line/front")
        if p.i32(cmd + 0x50) != section:
            return False, "section yazilmadi"
        ok = g.call(NR_CANEXE, [cmd]) & 0xFF
        if not ok:
            return False, "CanExecute false"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


OI_CHILDREN = 0x1F8   # CPdxArray<COrderInstance*>: offensive lines under a front line
OI_DIVISIONS = 0x210  # CPdxArray of CArmy+0xB8 SUB-OBJECTS, like COrdersGroup+0x50
CARMY_SUB    = 0xB8   # base = element - CARMY_SUB


def order_divisions(g, order):
    """CArmy bases of the divisions assigned to one order (COrderInstance*)."""
    from game import _arr
    return [e - CARMY_SUB for e in _arr(g.p, order, OI_DIVISIONS)]


def orders_of(g, orders_group):
    """The army's current orders (COrderInstance), offensive lines nested under their
    front line in "children" (they are NOT in the army's own order array)."""
    from game import _arr
    p = g.p
    return [_order_info(g, o) for o in _arr(p, orders_group, OG_ORDERS)]


def _order_info(g, o, depth=0):
    from game import _arr
    p = g.p
    rec = {
        "ptr": hex(o),
        "id": p.i32(o + 0x244),
        "type": p.i32(o + 0x30),          # 1=offensive 2=front line 3=invasion 4=paradrop
        "assigned_divisions": p.i32(o + 0x21C),
        "root_front": [p.i32(o + 0x258), p.i32(o + 0x25C)],
        "root_section": p.i32(o + 0x260),
        "from": p.i64(o + 0x268),
        "to": p.i64(o + 0x270),
        "fallback": bool(p.u8(o + 0x298)),
        "executing": bool(p.u8(o + 0x248)),
    }
    if depth < 3:
        kids = [_order_info(g, c, depth + 1) for c in _arr(p, o, OI_CHILDREN)]
        if kids:
            rec["children"] = kids
    return rec


# COrderDeleteCommand(this, COrdersGroup*, i32 order_id) deletes an order
OD_CTOR   = 0x231c510
OD_CANEXE = 0x231a870
OD_SIZE   = 0x30


def delete_order(g, orders_group, order_id):
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        cmd = g.malloc(OD_SIZE); p.write(cmd, b"\0" * OD_SIZE)
        g.call(OD_CTOR, [cmd, orders_group, order_id])
        check_field_handle(g, cmd, 0x24, orders_group, "delete_order/og")
        if p.i32(cmd + 0x2C) != order_id:
            return False, "order id yazilmadi"
        ok = g.call(OD_CANEXE, [cmd]) & 0xFF
        if not ok:
            return False, "CanExecute false (no such order)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ armies in army groups
# Both take a CPdxArray<COrdersGroup*> of ARMIES (the ctor reads each element's handle
# at +0x8). Signatures read from the 1.19.3 disassembly on 17.10; vtables 0x44699D0 and
# 0x4469A98 match the RTTI of the two commands, CanExecute is vtable slot 10.
ATG_CTOR, ATG_CANEXE, ATG_SIZE = 0x2317F20, 0x2318530, 0x50   # (this, armies*, group*, i32)
RFG_CTOR, RFG_CANEXE, RFG_SIZE = 0x23189D0, 0x2318C60, 0x40   # (this, armies*)


def _post_armies(g, ctor, canexe, size, armies, *extra):
    import api
    api.allow_call(ctor)
    api.allow_call(canexe)
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        arr = _mkarray(g, list(armies))
        cmd = g.malloc(size); p.write(cmd, b"\0" * size)
        g.call(ctor, [cmd, arr] + list(extra))
        if p.i32(cmd + 0x34) != len(armies):
            return False, "army array was not filled (%d/%d)" % (p.i32(cmd + 0x34), len(armies))
        if not g.call(canexe, [cmd, 0]) & 0xFF:
            return False, "CanExecute false"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def assign_to_army_group(g, armies, group):
    """Move armies (COrdersGroup*) into an army group. The game deletes an army group
    left without armies (seen 17.10: the West group vanished when its only army left)."""
    return _post_armies(g, ATG_CTOR, ATG_CANEXE, ATG_SIZE, armies, group, 0)


def remove_from_army_group(g, armies):
    """Take armies out of their army group; they stay as independent armies."""
    return _post_armies(g, RFG_CTOR, RFG_CANEXE, RFG_SIZE, armies)


# ------------------------------------------------------------------ assigning divisions
# COrderAssignCommand(this, CPdxArray<handle>* divisions, COrdersGroup* og, i32 order_id)
OA_CTOR   = 0x231DAF0
OA_CANEXE = 0x231F4C0
OA_SIZE   = 0x50
CARMY_HANDLE = 0x18


def _mkhandlearray(g, divisions):
    """Build a CPdxArray<handle> from a list of CArmy*."""
    p = g.p
    n = len(divisions)
    data = g.malloc(max(1, n) * 8)
    p.write(data, b"".join(p.read(d + CARMY_HANDLE, 8) for d in divisions))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, p.base + ALLOC))
    return hdr


def assign_to_order(g, orders_group, order_id, divisions):
    """Assign divisions to a battle plan order (dragging them onto it in the UI)."""
    p = g.p
    if not divisions:
        return False, "no divisions to assign"
    own = g._inj is None
    if own: g.attach()
    try:
        arr = _mkhandlearray(g, list(divisions))
        cmd = g.malloc(OA_SIZE); p.write(cmd, b"\0" * OA_SIZE)
        g.call(OA_CTOR, [cmd, arr, orders_group, order_id])
        check_field_handle(g, cmd, 0x40, orders_group, "assign_to_order/og")
        if p.i32(cmd + 0x48) != order_id:
            return False, "order id yazilmadi"
        if p.i32(cmd + 0x34) != len(divisions):
            return False, "division array was not filled (%d/%d)" % (p.i32(cmd + 0x34), len(divisions))
        ok = g.call(OA_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "CanExecute false"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def group_divisions(g, orders_group, tag=None):
    """Divisions of an army, or of every army in an army group. CArmy bases.

    The array at COrdersGroup+0x50 holds the sub-object at CArmy+0xB8, which is why
    divisions are gathered from the country's own list via the `CArmy+0xC0` link.
    """
    from game import _arr
    p = g.p
    members = {orders_group}
    if p.u8(orders_group + OG_IS_GROUP):
        members |= set(_arr(p, orders_group, OG_MEMBERS))
    co = (g.country(tag) if tag else g.player()).ptr
    out = []
    for d in _arr(p, co, 0x290):
        og = p.u64(d + CARMY_OG)
        if og in members or (og and p.u64(og + OG_PARENT) in members):
            out.append(d)
    return out


# ------------------------------------------------------------------ execute plan
# COrderExecuteCommand(this, COrdersGroup* og, i32 order_id, u8 flag)
#   order_id = 0  -> the group's ENTIRE plan
OE2_CTOR   = 0x2332310
OE2_CANEXE = 0x23326B0
OE2_SIZE   = 0x38


OI_EXECUTING = 0x248      # u8 on COrderInstance ("can_execute" in the serializer)


def execute_plan(g, orders_group, order_id=0, run=True, members=True):
    """Execute (run=True) or stop (run=False) the battle plan: the green button in the UI.

    COrderExecuteCommand(this, og, order_id, u8 run @+0x30, u8 members @+0x31) SETS the
    state, it does not toggle it. order_id=0 walks the army's orders (og+0x98) and writes
    COrderInstance+0x248; members=True carries it into the armies of an army group.
    Upstream passed 0 in the run byte, so every "execute" it sent was a STOP.
    The result is read back from the orders, not trusted from the post.
    """
    import time
    from game import _arr
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        cmd = g.malloc(OE2_SIZE); p.write(cmd, b"\0" * OE2_SIZE)
        g.call(OE2_CTOR, [cmd, orders_group, order_id, int(bool(run)), int(bool(members))])
        check_field_handle(g, cmd, 0x24, orders_group, "execute_plan/og")
        if p.i32(cmd + 0x2C) != order_id:
            return False, "order id was not written"
        if p.u8(cmd + 0x30) != int(bool(run)):
            return False, "run flag was not written"
        ok = g.call(OE2_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "CanExecute false (no plan, or it cannot be executed)"
        g.cmd.post(cmd)
    finally:
        if own: g.detach()
    orders = _arr(p, orders_group, OG_ORDERS)
    if order_id:
        orders = [o for o in orders if p.i32(o + 0x244) == order_id]
    if not orders:
        return True, "posted; no direct orders to read back (army group?)"
    want = int(bool(run))
    for _ in range(20):                     # applied on the game's next frame
        time.sleep(0.05)
        flags = [p.u8(o + OI_EXECUTING) for o in orders]
        if all(f == want for f in flags):
            return True, ""
    return False, "posted, but %d/%d orders show executing=%d" % (
        sum(f == want for f in flags), len(orders), want)


# ================================================================== OFFENSIVE LINE
# COrderNewFrontCommand = a CHILD FRONT (kind 1) under a root front: an offensive line.
#   ctor(this, COrdersGroup* og, CPdxArray<i32>* province_yolu, COrderInstance* ust_emir,
#        CPdxArray<CArmy*>* divisions|0, u8 split_from, u8 blitz, u8 withdraw, void*|0)
#   the u8s land at +0x6C split_from, +0x6D blitz, +0x6E withdraw (serializer tokens,
#   18.10). blitz is the SPEARHEAD line of the UI (UNITS_VIEW_BTN_DRAW_BLITZ): its
#   divisions push ahead as fast as they can, taking what they find.
#   +0x24 og | +0x38 division handle array | +0x50 province id array
#   +0x68 parent order's instance_id | +0x6C/+0x6D/+0x6E flags
NF_CTOR   = 0x232CC20
NF_CANEXE = 0x232E3C0
NF_SIZE   = 0x70


def _mkintarray(g, ids):
    p = g.p
    n = len(ids)
    data = g.malloc(max(1, n) * 4)
    p.write(data, b"".join(struct.pack("<i", int(x)) for x in ids))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, p.base + ALLOC))
    return hdr


def target_line(g, enemy_tag, depth="full", own_tag=None):
    """Province list for an offensive line inside enemy territory.

    depth="full" -> the enemy provinces FURTHEST from us, i.e. all the way through.
    depth=<n> -> that many provinces in from the border.
    """
    import mapdata
    from collections import deque
    if own_tag is None:
        own_tag = g.player().tag
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    enemy = {pid for pid, t in owners.items() if t == enemy_tag and ptype.get(pid) == "land"}
    if not enemy:
        return []
    start = [pid for pid in enemy
             if any(owners.get(n) == own_tag and ptype.get(n) == "land" for n in adj.get(pid, ()))]
    if not start:
        start = sorted(enemy)[:1]
    dist = {p0: 0 for p0 in start}
    q = deque(start)
    while q:
        cur = q.popleft()
        for n in adj.get(cur, ()):
            if n in enemy and n not in dist:
                dist[n] = dist[cur] + 1
                q.append(n)
    if depth == "full":
        top = max(dist.values())
        return sorted(p0 for p0, d in dist.items() if d == top)
    depth = int(depth)
    return sorted(p0 for p0, d in dist.items() if d == depth) or sorted(dist, key=dist.get)[-1:]


def offensive_line(g, orders_group, parent_order_id, provinces, divisions=None, blitz=False):
    """Add an OFFENSIVE LINE (a child front) to a root front order; blitz=True draws a
    spearhead line instead."""
    p = g.p
    from game import _arr
    if not provinces:
        return False, "target province list is empty"
    own = g._inj is None
    if own: g.attach()
    try:
        # the parent may be a front, or an arrow (a chained arrow continues from the tip
        # of the one before): search the order tree
        parent, todo = None, list(_arr(p, orders_group, OG_ORDERS))
        while todo:
            o = todo.pop()
            if p.i32(o + 0x244) == parent_order_id:
                parent = o
                break
            todo += _arr(p, o, 0x1F8)
        if parent is None:
            return False, "parent order not found: %d" % parent_order_id
        path = _mkintarray(g, provinces)
        if divisions is None:
            # The UI moves the front line's divisions onto a new arrow; the command does
            # not, and an arrow posted without them is an empty order.
            divisions = order_divisions(g, parent)
        darr = _mkarray(g, list(divisions)) if divisions else 0
        cmd = g.malloc(NF_SIZE); p.write(cmd, b"\0" * NF_SIZE)
        g.call(NF_CTOR, [cmd, orders_group, path, parent, darr, 0, 1 if blitz else 0, 0, 0])
        if blitz and not p.u8(cmd + 0x6D):
            return False, "the blitz flag was not written"
        check_field_handle(g, cmd, 0x24, orders_group, "offensive_line/og")
        if p.i32(cmd + 0x68) != parent_order_id:
            return False, "parent order id was not written (%d)" % p.i32(cmd + 0x68)
        if p.i32(cmd + 0x5C) != len(provinces):
            return False, "province path was not filled (%d/%d)" % (p.i32(cmd + 0x5C), len(provinces))
        ok = g.call(NF_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "CanExecute false (not at war, or the line is not valid)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ fallback line
# COrderNewFallbackCommand(this, og, CPdxArray<i32>* province_path, CPdxArray<CArmy*>* divisions|0)
FB_CTOR   = 0x23247A0
FB_CANEXE = 0x2324EC0
FB_SIZE   = 0x70


def fallback_line(g, orders_group, provinces, divisions=None):
    """Issue a fallback line order."""
    p = g.p
    if not provinces:
        return False, "province list is empty"
    own = g._inj is None
    if own: g.attach()
    try:
        path = _mkintarray(g, provinces)
        darr = _mkarray(g, list(divisions)) if divisions else 0
        cmd = g.malloc(FB_SIZE); p.write(cmd, b"\0" * FB_SIZE)
        g.call(FB_CTOR, [cmd, orders_group, path, darr])
        check_field_handle(g, cmd, 0x24, orders_group, "fallback_line/og")
        ok = g.call(FB_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "CanExecute false"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# --------------------------------------------------- naval invasion and paradrop
# Signatures read from the AI's own calls (22.10, 1.19.3) and verified live on Japan
# 1936.01.01 at peace: the source order and the invasion target were accepted, divisions
# assigned with assign_to_order; the paradrop target was refused at peace (no war).
#   COrderSetInvasionSourceCommand(this, og, i32 port_province, u8 0,
#       CPdxArray<CArmy*>* divisions or 0, i32 order_id (0: new), i32 0), new 0x58
#   CAddNavalInvasionTargetCommand(this, og, i32 order_id, i32 province), new 0x38
#   COrderSetParadropSourceCommand(this, og, i32 airbase_province, i32 order_id (0: new),
#       CPdxArray<CArmy*>* or 0), new 0x50
#   COrderSetParadropTargetCommand(this, og, i32 order_id, i32 province), new 0x38
# CanExecute is vtable slot 10 of each (this, 0). The invasion targets of an order are a
# CPdxArray<i32> at COrderInstance+0x70.
INV_CTOR, INV_SIZE = 0x2333900, 0x60
INV_TGT_CTOR, INV_TGT_SIZE = 0x2334AE0, 0x40
PD_CTOR, PD_SIZE = 0x2336080, 0x58
PDT_CTOR, PDT_SIZE = 0x2336C40, 0x40
OI_TARGETS = 0x70
OI_SOURCE = 0xB8      # i32, the port (invasion) or air base (paradrop) province


def _build_and_check(g, ctor, size, args):
    """Build a command, ask the game's own CanExecute (vtable slot 10). (cmd, ok)"""
    import api
    p = g.p
    api.allow_call(ctor)
    cmd = g.malloc(size)
    p.write(cmd, b"\0" * size)
    g.call(ctor, [cmd] + list(args))
    ce = p.u64(p.u64(cmd) + 10 * 8) - p.base
    api.allow_call(ce)
    return cmd, bool(g.call(ce, [cmd, 0]) & 0xFF)


def _new_order(g, orders_group, kind, before):
    from game import _arr
    p = g.p
    for o in _arr(p, orders_group, OG_ORDERS):
        if p.i32(o + 0x30) == kind and p.i32(o + 0x244) not in before:
            return p.i32(o + 0x244)
    return None


def _add_targets(g, ctor, size, orders_group, order_id, provinces):
    added, refused = [], []
    for q in provinces:
        cmd, ok = _build_and_check(g, ctor, size, [orders_group, int(order_id), int(q)])
        if ok:
            g.cmd.post(cmd)
            added.append(int(q))
        else:
            refused.append(int(q))
    return added, refused


def naval_invasion(g, orders_group, port_province, target_provinces, divisions=()):
    """A naval invasion order for an army: from our port to coastal provinces, with these
    divisions (CArmy*). It can be drawn at peace and launched at war (execute_plan).
    Returns (order_id or None, message)."""
    import time
    from game import _arr
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        before = {p.i32(o + 0x244) for o in _arr(p, orders_group, OG_ORDERS)}
        cmd, ok = _build_and_check(g, INV_CTOR, INV_SIZE,
                                   [orders_group, int(port_province), 0, 0, 0, 0])
        if not ok:
            return None, "the game refused an invasion from province %s" % port_province
        g.cmd.post(cmd)
    finally:
        if own: g.detach()
    time.sleep(0.6)
    oid = _new_order(g, orders_group, 3, before)
    if oid is None:
        return None, "the invasion order did not appear"
    if own: g.attach()
    try:
        added, refused = _add_targets(g, INV_TGT_CTOR, INV_TGT_SIZE, orders_group, oid,
                                      target_provinces)
    finally:
        if own: g.detach()
    if divisions:
        assign_to_order(g, orders_group, oid, list(divisions))
    msg = "targets %s" % added + (", refused %s" % refused if refused else "")
    return oid, msg


def paradrop(g, orders_group, airbase_province, target_provinces, divisions=()):
    """A paradrop order: from our air base to provinces. Returns (order_id, message)."""
    import time
    from game import _arr
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        before = {p.i32(o + 0x244) for o in _arr(p, orders_group, OG_ORDERS)}
        cmd, ok = _build_and_check(g, PD_CTOR, PD_SIZE,
                                   [orders_group, int(airbase_province), 0, 0])
        if not ok:
            return None, "the game refused a paradrop from province %s" % airbase_province
        g.cmd.post(cmd)
    finally:
        if own: g.detach()
    time.sleep(0.6)
    oid = _new_order(g, orders_group, 4, before)
    if oid is None:
        return None, "the paradrop order did not appear"
    if own: g.attach()
    try:
        added, refused = _add_targets(g, PDT_CTOR, PDT_SIZE, orders_group, oid,
                                      target_provinces)
    finally:
        if own: g.detach()
    if divisions:
        assign_to_order(g, orders_group, oid, list(divisions))
    msg = "targets %s" % added + (", refused %s" % refused if refused else "")
    return oid, msg


def order_source(g, order):
    return g.p.i32(order + OI_SOURCE)


def order_targets(g, order):
    """The target provinces of an invasion or paradrop order (COrderInstance*)."""
    from game import _arr
    try:
        d = g.p.u64(order + OI_TARGETS)
        n = g.p.i32(order + OI_TARGETS + 0xC)
        return list(struct.unpack("<%di" % n, g.p.read(d, 4 * n))) if 0 < n < 200 else []
    except Exception:
        return []


# ------------------------------------------------------------------ player events
# Events waiting for the player are the game GUI's event windows (1.19.4.1):
# In 1.19.4.1, CGameGui+0x7C0 is a CPdxArray of child windows (shifted from 0x768);
# the active CGameGui is *(CInGameIdler + 0x4F8). For an event window the element is the
# CEventWindow's +0x28 sub-object, and the window holds CEvent* at +0x68 and the event's
# CEventScope (by value) at +0x78; the scope's +0x8 is the instance id (1, random).
GUI_WINDOWS = 0x7C0
EW_SUB, EW_ID, EW_EVENT, EW_SCOPE = 0x28, 0x60, 0x68, 0x78
EW_AWAITING = 0x338   # u8; the window's own click handler clears it, and the GUI then
                      # drops the window (an answer posted from outside left it on screen)
# CSelectEventOptionCommand(this, i32 pending_id, CEvent*, CCountryTag*, CEventScope*,
# i32 option) from the disassembly; vtable 0x44BCC68, CanExecute is vtable slot 10. The
# pending id (window +0x60) keys the game's registry of open events, *(0x459E9F0)+0x558:
# 0x38-byte entries {i32 id, CEvent*, ...}; CanExecute checks that entry's event.
SEO_CTOR, SEO_SIZE = 0x2CD9D10, 0xF0
EVENT_REGISTRY = 0x459E9F0


def event_windows(g):
    """[(window, CEvent*, CEventScope*, pending id)] for the events waiting on the player."""
    from game import _arr
    p = g.p
    from commands import IDLER_GLOBAL
    idl = p.u64(g.va(IDLER_GLOBAL))
    gui = p.u64(idl + 0x4F8) if idl else 0
    if not gui:
        return []
    db = p.u64(g.va(EVENT_REGISTRY))
    open_ids = {struct.unpack_from("<i", p.read(p.u64(db + 0x558) + i * 0x38, 4))[0]
                for i in range(p.i32(db + 0x564))}
    out = []
    for e in _arr(p, gui, GUI_WINDOWS):
        w = e - EW_SUB
        if g.oi.class_of(w) != "CEventWindow":
            continue
        ev = p.u64(w + EW_EVENT)
        if ev and g.oi.class_of(ev) == "CEvent" and p.i32(w + EW_ID) in open_ids:
            # (a window answered from outside the GUI stays on screen, but its entry
            # leaves the registry)
            out.append((w, ev, w + EW_SCOPE, p.i32(w + EW_ID)))
    return out


def select_event_option(g, pending_id, event, scope, option, window=None):
    import api
    p = g.p
    vt = p.u64(g.va(0x44BCC68) + 10 * 8)
    canexe = vt - g.va(0)
    api.allow_call(SEO_CTOR)
    api.allow_call(canexe)
    own = g._inj is None
    if own: g.attach()
    try:
        tag = g.malloc(8)
        p.write(tag, struct.pack("<i", g.player().tag_id))
        cmd = g.malloc(SEO_SIZE); p.write(cmd, b"\0" * SEO_SIZE)
        g.call(SEO_CTOR, [cmd, int(pending_id), event, tag, scope, int(option)])
        if p.i32(cmd + 0x24) != int(pending_id) or p.i32(cmd + 0xE8) != int(option):
            return False, "command fields were not written"
        if not g.call(canexe, [cmd, 0]) & 0xFF:
            return False, "CanExecute false"
        g.cmd.post(cmd)
        if window:
            p.write(window + EW_AWAITING, b"\0")
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ exercises
# COrderSetTrainingCommand(this, COrdersGroup* og, u8 train @+0x2C, u8 stop_at_max @+0x2D):
# the exercise button of an army or army group (21.10: ctor 0x23372F0, CanExecute
# 0x23374D0, the game's handler 0x234E9F0 writes og+0x19D train and og+0x1A0 stop-at-max).
# Exercises raise divisions to Regular at most (NDefines TRAINING_MAX_LEVEL 2, 0.30 xp),
# hold their organisation at 20% (TRAINING_ORG) and wear equipment (+5% attrition).
TR_CTOR, TR_CANEXE, TR_SIZE = 0x23372F0, 0x23374D0, 0x30
OG_TRAINING, OG_TRAIN_STOP_AT_MAX = 0x19D, 0x1A0


def training_state(g, og):
    return {"exercising": bool(g.p.u8(og + OG_TRAINING)),
            "stop_at_max": bool(g.p.u8(og + OG_TRAIN_STOP_AT_MAX))}


def set_training(g, og, on=True, stop_at_max=True):
    """Start (on) or stop the exercises of an army or army group. Read back from the
    group, not trusted from the post."""
    import time
    import api
    api.allow_call(TR_CTOR)      # verified 21.10 (see above); 23.10 every call was refused
    api.allow_call(TR_CANEXE)
    p = g.p
    # CanExecute (0x23374D0, read 24.10) is false only when the group already has the
    # asked train / stop-at-max flags (og+0x19D, og+0x1A0), so an army already drilling
    # read as a failure (23.10: "army 484: CanExecute false" again and again)
    st = training_state(g, og)
    if st["exercising"] == bool(on) and (not on or st["stop_at_max"] == bool(stop_at_max)):
        return True, "already " + ("exercising" if on else "not exercising")
    own = g._inj is None
    if own: g.attach()
    try:
        cmd = g.malloc(TR_SIZE); p.write(cmd, b"\0" * TR_SIZE)
        g.call(TR_CTOR, [cmd, og, int(bool(on)), int(bool(on and stop_at_max))])
        check_field_handle(g, cmd, 0x24, og, "set_training/og")
        if p.u8(cmd + 0x2C) != int(bool(on)):
            return False, "train flag was not written"
        if not g.call(TR_CANEXE, [cmd, 0]) & 0xFF:
            return False, "CanExecute false"
        g.cmd.post(cmd)
    finally:
        if own: g.detach()
    for _ in range(20):
        time.sleep(0.05)
        if training_state(g, og)["exercising"] == bool(on):
            return True, ""
    return False, "posted, but the army shows exercising=%d" % training_state(g, og)["exercising"]
