"""Army headquarters (Thunder at our Gates; 26.10 on 1.19.3).

An HQ is a division of an HQ template (CDivisionTemplate+0x1CC set; every country has
one, "Army Headquarters" for the UK) deployed by an army's general (rank 1). It sits in
its army; a division's template is CArmy+0x3B8. Without one the army's planning cap and
planning speed are x0.8 (NMilitary PLANNING_CAP/SPEED_NO_HQ_SCALING). Deploying takes some
equipment and manpower from the army's divisions (never below 10%) and 1-3 days; the
general withdraws by himself after 7 days with no front or landing order.

CDeployArmyHqCommand (0x38 bytes, default ctor 0x253F0E0): +0x24 the army's handle
(COrdersGroup, type 53), +0x2C the HQ template's handle (type 52). CWithdrawArmyHqCommand
(0x30 bytes, ctor 0x253FA30): +0x24 the army's handle. CanExecute is vtable slot 10.
"""
import struct

from . import bridge as B

DEPLOY_CTOR, DEPLOY_SIZE = 0x253F0E0, 0x38
WITHDRAW_CTOR, WITHDRAW_SIZE = 0x253FA30, 0x30
CMD_ARMY, CMD_TEMPLATE, VT_CANEXE = 0x24, 0x2C, 10
T_IS_HQ, T_HANDLE, A_TEMPLATE = 0x1CC, 0x08, 0x3B8
CO_DIVISIONS, RANK = 0x290, 0xD7C
NO_HQ_PLANNING = 0.8


def template(g):
    """Our HQ template (CDivisionTemplate*), or None."""
    p = g.p
    for t in g.division_templates():
        ptr = int(t["ptr"], 16)
        if p.u8(ptr + T_IS_HQ):
            return ptr
    return None


def deployed(g):
    """{army (COrdersGroup*): HQ division (CArmy*)} of ours: pure reads."""
    from game import _arr
    A = B.module("armyops")
    p = g.p
    out = {}
    for d in _arr(p, g.player().ptr, CO_DIVISIONS):
        t = p.u64(d + A_TEMPLATE)
        if t > 0x10000 and p.u8(t + T_IS_HQ):
            og = p.u64(d + A.CARMY_OG)
            if og:
                out[og] = d
    return out


def _cmd(g, ctor, size, og, tmpl=None):
    import api
    p = g.p
    A = B.module("armyops")
    api.allow_call(ctor)
    cmd = g.malloc(size)
    p.write(cmd, b"\0" * size)
    g.call(ctor, [cmd])
    p.write(cmd + CMD_ARMY, p.read(og + A.OG_HANDLE, 8))
    if tmpl:
        p.write(cmd + CMD_TEMPLATE, p.read(tmpl + T_HANDLE, 8))
    fn = p.u64(p.u64(cmd) + 8 * VT_CANEXE) - p.base
    api.allow_call(fn)
    return cmd, bool(g.call(fn, [cmd, 0]) & 0xFF)


def deploy(g, og, dry_run=False):
    """Deploy the army's general with an HQ. (ok, why)."""
    A = B.module("armyops")
    p = g.p
    ldr = p.u64(og + A.OG_LEADER)
    if not ldr:
        return False, "the army has no general"
    if p.i32(ldr + RANK) != 1:
        return False, "only a general (not a field marshal) deploys an army HQ"
    if og in deployed(g):
        return False, "its HQ is deployed already"
    tmpl = template(g)
    if not tmpl:
        return False, "we have no HQ template"
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd, ok = _cmd(g, DEPLOY_CTOR, DEPLOY_SIZE, og, tmpl)
        if not ok:
            return False, ("the game refuses (the general may be deploying or "
                           "withdrawing, or the army cannot spare the equipment now)")
        if not dry_run:
            g.cmd.post(cmd)
        return True, ""
    finally:
        if own:
            g.detach()


def withdraw(g, og, dry_run=False):
    if og not in deployed(g):
        return False, "the army has no HQ deployed"
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd, ok = _cmd(g, WITHDRAW_CTOR, WITHDRAW_SIZE, og)
        if not ok:
            return False, "the game refuses to withdraw it now"
        if not dry_run:
            g.cmd.post(cmd)
        return True, ""
    finally:
        if own:
            g.detach()


def status(g, og, hqs=None):
    """A short line for an army: its HQ, or what it lacks."""
    A = B.module("armyops")
    p = g.p
    hqs = deployed(g) if hqs is None else hqs
    if og in hqs:
        return "HQ deployed"
    ldr = p.u64(og + A.OG_LEADER)
    if not ldr:
        return "no HQ (no general: planning x0.8)"
    if p.i32(ldr + RANK) != 1:
        return "no HQ (led by a field marshal)"
    return "NO HQ: planning cap and speed x%.1f (deploy_hq)" % NO_HQ_PLANNING
