"""The intelligence agency: create it, take its upgrades. Verified 22.10 on Japan 1936.

country +0xF98 CIntelligenceAgency: +0x30 CPdxArray<COperativeLeader*> (our operatives),
+0x80 its name, +0xC0 1 when built (256 while being created), +0xD0 the CAgencyUpgrade
being taken (+0xC8 its progress).
  CIntelligenceAgencyCreationCommand ctor 0x22CEA20 (this, &our tag index)
  CIntelligenceAgencyUpgradeCommand ctor 0x22CEF80 (this, &our tag index, CAgencyUpgrade*)
Operative missions are not done: the mission command is built by per-mission factories
(0x22D1180.. 0x22D1D70: out, &tag, {begin,end} of operatives, CState* or target, &target
tag index); a build-network command passed CanExecute and was sent, but no field of the
operative or the agency changed, and the recruit command changed nothing either.
"""
import struct

from .bridge import module as B

CO_AGENCY = 0xF98
AG_BUILT, AG_UPGRADE = 0xC0, 0xD0
AGENCY_CREATE_CTOR, UPGRADE_CTOR = 0x22CEA20, 0x22CEF80


def _tag(g):
    t = g.malloc(8)
    g.p.write(t, struct.pack("<ii", g._c(None).tag_id, 0))
    return t


def _check(g, ctor, args, send):
    api, ao = B("api"), B("armyops")
    p = g.p
    api.allow_call(ctor)
    cmd = g.malloc(0x40)
    p.write(cmd, b"\0" * 0x40)
    g.call(ctor, [cmd] + args)
    ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
    api.allow_call(ce)
    st = ao._mkstring(g)
    ok = bool(g.call(ce, [cmd, st]) & 0xFF)
    if ok and send:
        g.cmd.post(cmd)
    return ok, ao._readstring(g, st)


def _upgrades(g):
    return {B("game").item_name(g.p, u + 0x10): u for u in g.oi.instances("CAgencyUpgrade")}


def status(g):
    """{exists, built, upgrading, operatives, can_take: [...]}"""
    p = g.p
    a = p.u64(g.player().ptr + CO_AGENCY)
    # the object is there from the start; +0xC0 is 0 until it is founded (23.10: a Japan
    # of 1938 had none, and "being created" kept the minister from founding it for years)
    if not a or p.i32(a + AG_BUILT) == 0:
        return {"exists": False}
    up = p.u64(a + AG_UPGRADE)
    out = {"exists": True, "name": B("game").item_name(p, a + 0x80),
           "built": p.i32(a + AG_BUILT) == 1,
           "upgrading": B("game").item_name(p, up + 0x10) if up else None,
           "operatives": len(B("game")._arr(p, a, 0x30)), "can_take": []}
    if out["built"] and not up:
        own = g._inj is None
        if own:
            g.attach()
        try:
            tag = _tag(g)
            out["can_take"] = [n for n, u in sorted(_upgrades(g).items())
                               if _check(g, UPGRADE_CTOR, [tag, u], False)[0]]
        finally:
            if own:
                g.detach()
    return out


def create(g):
    own = g._inj is None
    if own:
        g.attach()
    try:
        ok, why = _check(g, AGENCY_CREATE_CTOR, [_tag(g)], True)
    finally:
        if own:
            g.detach()
    if not ok:
        return {"error": why or "the game does not allow it now"}
    g._settle(0.5)
    agency = g.p.u64(g.player().ptr + CO_AGENCY)
    if not agency or g.p.i32(agency + AG_BUILT) == 0:
        return {"posted": True, "error": "agency creation not confirmed; inspect its status before retrying"}
    return {"created": True, "verified": True}


_INFO = None


def upgrade_info():
    """{upgrade: (civilian factories taken while it is built, levels, what it gives)} from
    common/intelligence_agency_upgrades (cached). Each level takes AGENCY_UPGRADE_DAYS."""
    global _INFO
    if _INFO is None:
        import os, re
        f = os.path.join(B("config").game_dir(), "common", "intelligence_agency_upgrades",
                         "intelligence_agency_upgrades.txt")
        txt = re.sub(r"#[^\n]*", "", open(f, encoding="utf-8-sig").read())
        _INFO, depth, key, start = {}, 0, None, 0
        for m in re.finditer(r"([A-Za-z0-9_]+)\s*=\s*\{|\{|\}", txt):
            if m.group(0).endswith("{"):
                if depth == 1 and m.group(1):
                    key, start = m.group(1), m.end()
                depth += 1
                continue
            depth -= 1
            if depth == 1 and key:
                b = txt[start:m.start()]
                fac = re.findall(r"civilian_factory_use\s*=\s*([\d.]+)", b)
                lv = max(1, len(re.findall(r"\blevel\s*=\s*\{", b)))
                lvl = b[b.find("level"):] if "level" in b else ""
                mods = re.findall(r"\b([a-z_]+)\s*=\s*(-?[\d.]+)", lvl)
                _INFO[key] = (int(float(fac[0])) if fac else 5, lv,
                              ", ".join("%s %s" % kv for kv in mods[:3]))
                key = None
    return _INFO


def _days(kind):
    try:
        import os, re
        f = os.path.join(B("config").game_dir(), "common", "defines", "00_defines.lua")
        m = re.search(r"AGENCY_%s_DAYS\s*=\s*(\d+)" % kind, open(f, encoding="utf-8").read())
        return int(m.group(1))
    except Exception:
        return 30


def cost_text(name):
    i = upgrade_info().get(name)
    if not i:
        return name
    return "%s (%d civilian factories for about %d days a level, %d level%s%s)" % (
        name, i[0], _days("UPGRADE"), i[1], "s" if i[1] > 1 else "",
        ": " + i[2] if i[2] else "")


def upgrade(g, name):
    u = _upgrades(g).get(name)
    if u is None:
        return {"error": "no agency upgrade %s" % name}
    own = g._inj is None
    if own:
        g.attach()
    try:
        ok, why = _check(g, UPGRADE_CTOR, [_tag(g), u], True)
    finally:
        if own:
            g.detach()
    if not ok:
        return {"error": why or "not allowed now (one at a time)"}
    g._settle(0.5)
    agency = g.p.u64(g.player().ptr + CO_AGENCY)
    if not agency or g.p.u64(agency + AG_UPGRADE) != u:
        return {"posted": True, "error": "agency upgrade not confirmed; inspect its status before retrying"}
    return {"upgrading": name, "verified": True}


def lines(g):
    s = status(g)
    if not s["exists"]:
        return ["INTELLIGENCE AGENCY: none (create_agency: it takes 5 civilian factories for "
                "about %d days; it decrypts the enemy's codes, guards ours and runs "
                "operatives)" % _days("CREATION")]
    if not s["built"]:
        return ["INTELLIGENCE AGENCY %s: being created (5 civilian factories taken until it "
                "is)" % s["name"]]
    up = s["upgrading"]
    try:
        o = operatives(g)
        ops_line = ["OPERATIVES: ours %s%s%s" % (
            "; ".join("%s (%s)" % (n, o["orders"].get(n, "no mission ordered by us yet"))
                      for n in o["ours"]) or "none",
            "; CANDIDATES TO RECRUIT NOW (recruit_operative): " + ", ".join(o["candidates"])
            if o["candidates"] else "",
            ". The first job of an operative is an intelligence network (operative_network) "
            "in the main enemy's land, a state near our border or his capital: it shows his "
            "divisions and opens operations; one operative a country, the next on the next "
            "enemy")]
    except Exception:
        ops_line = []
    ops_line += operation_lines(g)
    return ops_line + ["INTELLIGENCE AGENCY %s: %d operatives; upgrading: %s%s. Every upgrade takes "
            "civilian factories off construction while it is built: weigh it against the "
            "building plan" % (
                s["name"], len(_names(g, AG_OPERATIVES)),
                "%s, taking %d civilian factories now" % (up, upgrade_info().get(up, (5,))[0])
                if up else "nothing",
                ("; can take now: " + "; ".join(map(cost_text, s["can_take"])))
                if s["can_take"] else "")]


# ---------------------------------------------------------------- operatives
# Found 23.10 on Japan 1936 (the user watched the game): agency +0x18 CPdxArray of the
# recruitable candidates, +0xD8 of our operatives (COperativeLeader*: +0x08 handle, +0x40
# std::string name); +0x30 holds named operatives that are NOT ours (a pool).
#   CRecruitOperativeCommand ctor 0x22D3CD0 (this, &our tag index, COperativeLeader*)
#   mission factories (out**, &our tag, {begin,end} of COperativeLeader*, ...): build
#   intelligence network = 0x22D1760 (..., CState*, &target tag index), mission type 1.
# Where an operative keeps its mission was not found: what we ordered is kept here.
AG_CANDIDATES, AG_OPERATIVES = 0x18, 0xD8
RECRUIT_CTOR = 0x22D3CD0
NETWORK_FACTORY = 0x22D1760
ORDERS = {}          # operative name -> "build network in X (state)" as ordered in this run


def _names(g, off):
    p = g.p
    a = p.u64(g.player().ptr + CO_AGENCY)
    if not a or p.i32(a + AG_BUILT) != 1:
        return {}
    out = {}
    for o in B("game")._arr(p, a, off):
        try:
            out[p.stdstring(o + 0x40)] = o
        except Exception:
            continue
    return out


def operatives(g):
    """{'ours': [names], 'candidates': [names], 'orders': {name: mission}}"""
    ours = _names(g, AG_OPERATIVES)
    return {"ours": sorted(ours), "candidates": sorted(_names(g, AG_CANDIDATES)),
            "orders": {k: v for k, v in list(ORDERS.items()) + list(OP_ORDERS.items())
                       if k in ours}}


def recruit(g, name):
    c = _names(g, AG_CANDIDATES)
    if name not in c:
        return {"error": "no candidate %s (candidates: %s)" % (name, ", ".join(c) or "none now")}
    own = g._inj is None
    if own:
        g.attach()
    try:
        ok, why = _check(g, RECRUIT_CTOR, [_tag(g), c[name]], True)
    finally:
        if own:
            g.detach()
    if not ok:
        return {"error": why or "the game refused (no free slot?)"}
    g._settle(0.5)
    if _names(g, AG_OPERATIVES).get(name) != c[name]:
        return {"posted": True, "error": "operative recruitment not confirmed; inspect the roster before retrying"}
    return {"recruited": name, "verified": True}


def build_network(g, name, target_tag, state_id):
    """Send one of our operatives to build an intelligence network in a state of
    target_tag's (the game checks the state)."""
    api, ao = B("api"), B("armyops")
    p = g.p
    ours = _names(g, AG_OPERATIVES)
    if name not in ours:
        return {"error": "no operative of ours named %s (ours: %s)" % (name, ", ".join(ours) or "none")}
    try:
        tgt = g._c(target_tag)
    except Exception:
        return {"error": "no country %s" % target_tag}
    st = next((s["ptr"] for s in g.states(target_tag) if s["id"] == int(state_id)), None)
    if st is None:
        return {"error": "state %s is not %s's" % (state_id, target_tag)}
    own = g._inj is None
    if own:
        g.attach()
    try:
        api.allow_call(NETWORK_FACTORY)
        arr = g.malloc(8)
        p.write(arr, struct.pack("<Q", ours[name]))
        rng = g.malloc(16)
        p.write(rng, struct.pack("<QQ", arr, arr + 8))
        out = g.malloc(8)
        p.write(out, b"\0" * 8)
        tt = g.malloc(8)
        p.write(tt, struct.pack("<ii", tgt.tag_id, 0))
        g.call(NETWORK_FACTORY, [out, _tag(g), rng, st, tt])
        cmd = p.u64(out)
        ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
        api.allow_call(ce)
        s = ao._mkstring(g)
        ok = bool(g.call(ce, [cmd, s]) & 0xFF)
        why = ao._readstring(g, s)
        if ok:
            g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    if not ok:
        return {"error": why or "the game refused this target"}
    ORDERS[name] = "building an intelligence network in %s (state %s)" % (target_tag, state_id)
    OP_ORDERS.pop(name, None)
    return {"operative": name, "mission": ORDERS[name]}

# Not done yet, noted for later (the user, 23.10, from the agency screen): OPERATIONS
# (Infiltrate Civilian Administration / Army / Navy / Air Force, and the rest) each need
# operatives (2 for the infiltrations), an intelligence network of 35-50% in the target
# country and 75-90 days of preparation ("Prepare"); the command is
# CReserveOperativeForOperationCommand (ctor 0x22D4310, cmdctors.pkl) and
# CLaunchOperationCommand. CRYPTOLOGY (decryption of a country's codes, our encryption)
# opens with the decryption upgrades. Both wait for a network and two free operatives.


# ---------------------------------------------------------------- cryptology
# Found 24.10 on Japan 1936.11 (the user: the minister formed the department and nobody
# used it). Agency (+0xF98) +0x120 CCryptology: +0x28 array of CCountryDecryptionState
# (0x38 bytes, count +0x34; +0x40.. a hash of country -> index): state +0x08 the target's
# tag id, +0x12 1 while decrypting, +0x18 progress (fixed x1e5).
# CStartStopDecryptionCommand: ctor 0x22D2A50 (this), then +0x24 our tag id, +0x28 the
# target's, +0x2C 1 start / 0 stop; CanExecute is the vtable's +80 as for the others.
# The cipher to break: NDefines CRYPTO_BASE_CRYPTO_LEVEL 12000 + 4250 per encryption
# upgrade of the target. Measured: about 13 a day with only the department formed, so
# decryption upgrades decide whether a cipher breaks before a war. Activating the
# bonuses of a broken cipher (30 days, progress halves) was not found as a command of
# its own; the game's AI does it (CCryptologyAI).
CRYPTO = 0x120
DECRYPT_CTOR = 0x22D2A50
CIPHER_BASE, CIPHER_PER_UPGRADE = 12000, 4250
_SEEN = {}            # target -> [(game hours, progress)] seen in this run


def decryption(g):
    """[{target, active, progress, per_day}] for every country we have worked on."""
    p = g.p
    a = p.u64(g.player().ptr + CO_AGENCY)
    if not a or p.i32(a + AG_BUILT) != 1:
        return []
    o = p.u64(a + CRYPTO)
    if not o:
        return []
    arr, n = p.u64(o + 0x28), p.i32(o + 0x34)
    ids = {c.tag_id: c.tag for c in g.countries()}
    hours = g.date()["raw"]
    out = []
    for k in range(n if 0 < n < 500 else 0):
        r = arr + 0x38 * k
        tag = ids.get(p.i32(r + 8))
        prog = struct.unpack("<q", p.read(r + 0x18, 8))[0] / 1e5
        seen = _SEEN.setdefault(tag, [])
        if not seen or seen[-1][1] != prog:
            seen.append((hours, prog))
        del seen[:-6]
        rate = None
        if len(seen) >= 2 and seen[-1][0] - seen[0][0] >= 24 * 7:
            rate = (seen[-1][1] - seen[0][1]) / ((seen[-1][0] - seen[0][0]) / 24.0)
        out.append({"target": tag, "active": p.read(r + 0x12, 1)[0] == 1,
                    "progress": prog, "per_day": rate})
    return out


def set_decryption(g, target, start=True):
    """Start (or stop) decrypting a country's ciphers."""
    api = B("api")
    own = g._inj is None
    if own:
        g.attach()
    try:
        api.allow_call(DECRYPT_CTOR)
        p = g.p
        cmd = g.malloc(0x40)
        p.write(cmd, b"\0" * 0x40)
        g.call(DECRYPT_CTOR, [cmd])
        p.write(cmd + 0x24, struct.pack("<iiB", g._c(None).tag_id, g._c(target).tag_id,
                                        1 if start else 0))
        ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
        api.allow_call(ce)
        ao = B("armyops")
        st = ao._mkstring(g)
        ok = bool(g.call(ce, [cmd, st]) & 0xFF)
        why = ao._readstring(g, st)
        if ok:
            g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    if not ok:
        return {"error": why or "the game does not allow it now"}
    return {"decrypting" if start else "stopped": target}


def crypto_lines(g):
    s = status(g)
    if not s.get("exists") or not s.get("built"):
        return []
    try:
        d = decryption(g)
    except Exception:
        return []
    a = g.p.u64(g.player().ptr + CO_AGENCY)
    if not g.p.u64(a + CRYPTO):
        return []
    parts = []
    for x in d:
        if not (x["active"] or x["progress"]):
            continue
        left = max(0.0, CIPHER_BASE - x["progress"])
        eta = (" about %d days to break at %.0f a day" % (left / x["per_day"], x["per_day"])
               if x["per_day"] else " (its speed is measured after a week)")
        parts.append("%s %s, %.0f of at least %d%s" % (
            x["target"], "DECRYPTING" if x["active"] else "paused", x["progress"],
            CIPHER_BASE, eta if x["active"] and left else " (BROKEN)" if not left else ""))
    return ["CRYPTOLOGY (decrypt, one country at a time): " + ("; ".join(parts) or
            "decrypting nobody. Start on the enemy of the next war") +
            ". A cipher is at least %d (+%d per encryption upgrade of its owner); a broken "
            "one shows his army to our generals and its bonuses can be used in battle. The "
            "decryption upgrades make it faster: without them a cipher takes years"
            % (CIPHER_BASE, CIPHER_PER_UPGRADE)]


# "Reveal Intel 30 Days" (the user's screen, 24.10): CActivateActiveDecryptionBonuses (a
# command without "Command" in its name), ctor 0x22D3240 (this, &our tag, &target tag);
# allowed only on a broken cipher. For 30 days against that country: breakthrough and
# defence +15%, planning and invasion preparation +50%, invasion penalty -15%; then the
# enemy resets his cipher and our progress halves.
ACTIVATE_CTOR = 0x22D3240


def activate_decryption(g, target):
    api = B("api")
    own = g._inj is None
    if own:
        g.attach()
    try:
        p = g.p
        api.allow_call(ACTIVATE_CTOR)
        cmd = g.malloc(0x40)
        p.write(cmd, b"\0" * 0x40)
        me, tt = g.malloc(8), g.malloc(8)
        p.write(me, struct.pack("<ii", g._c(None).tag_id, 0))
        p.write(tt, struct.pack("<ii", g._c(target).tag_id, 0))
        g.call(ACTIVATE_CTOR, [cmd, me, tt])
        ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
        api.allow_call(ce)
        ao = B("armyops")
        st = ao._mkstring(g)
        ok = bool(g.call(ce, [cmd, st]) & 0xFF)
        why = ao._readstring(g, st)
        if ok:
            g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    if not ok:
        return {"error": why or "the cipher of %s is not broken yet" % target}
    return {"revealed": target, "days": 30}


def broken(g):
    """Countries whose cipher we have broken (progress at the cipher's strength)."""
    return [x["target"] for x in decryption(g) if x["progress"] >= CIPHER_BASE]


# ---------------------------------------------------------------- operations
# Found 26.10 on 1.19.3 (the UK's network in Germany reached 35%, the first operation
# opened). CCreateOperationCommand (0x58 bytes) is what "Prepare" sends; ctor 0x22D5520
# (this, operation id, &our tag, &target tag, location (0), auto-commence byte, auto-repeat
# byte, CPdxArray* of {operative handle (8), int} entries). Its Execute calls the
# country's operation manager (CCountry+0x1548), which picks the phases itself
# (GetSeededPhases) and puts the operatives in their slots. CanExecute (vtable +80) is
# (id, &our tag, &target tag, std::string* why). Operations by id: the hash at
# base+0x45356F0 (0x38-byte entries: +4 used, +8 id, +0x10 COperation*); a name from its
# id with 0x35F3B60 (id) -> *(char**).
OPS_HASH, OP_NAME_FN = 0x45356F0, 0x35F3B60
OPERATION_CREATE_CTOR, OPERATION_CREATE_SIZE = 0x22D5520, 0x58
_OPS = {}


def operations_db(g):
    """{operation key: id}."""
    p = g.p
    if p.pid in _OPS:
        return _OPS[p.pid]
    h = p.u64(p.base + OPS_HASH)
    tab, mask, extra = p.u64(h + 0x40), p.i32(h + 0x4c), p.u8(h + 0x50)
    ids = [p.i32(tab + 0x38 * i + 8) for i in range(mask + extra + 2)
           if p.u8(tab + 0x38 * i + 4)]
    api = B("api")
    own = g._inj is None
    if own:
        g.attach()
    out = {}
    try:
        api.allow_call(OP_NAME_FN)
        for k in ids:
            r = g.call(OP_NAME_FN, [k])
            if r:
                out[p.cstr(p.u64(r))] = k
    finally:
        if own:
            g.detach()
    _OPS[p.pid] = out
    return out


def prepare_operation(g, operation, target_tag, names, commence=True, dry_run=False):
    """Prepare an operation against target_tag with our operatives `names` (the game
    starts it by itself when ready if commence). {'prepared': ...} or {'error': why}."""
    api, ao = B("api"), B("armyops")
    p = g.p
    db = operations_db(g)
    key = operation if operation in db else "operation_" + operation
    if key not in db:
        return {"error": "no operation %s (the game has: %s)" % (
            operation, ", ".join(sorted(k for k in db if k.startswith("operation_"))))}
    ours = _names(g, AG_OPERATIVES)
    bad = [n for n in names if n not in ours]
    if bad:
        return {"error": "not operatives of ours: %s (ours: %s)" % (", ".join(bad), ", ".join(ours))}
    try:
        tgt = g._c(target_tag)
    except Exception:
        return {"error": "no country %s" % target_tag}
    own = g._inj is None
    if own:
        g.attach()
    try:
        api.allow_call(OPERATION_CREATE_CTOR)
        ent = g.malloc(12 * max(1, len(names)))
        for i, n in enumerate(names):
            p.write(ent + 12 * i, p.read(ours[n] + 0x08, 8) + struct.pack("<i", 0))
        arr = g.malloc(0x18)
        p.write(arr, struct.pack("<Qii", ent, len(names), len(names)) + b"\0" * 8)
        cmd = g.malloc(OPERATION_CREATE_SIZE)
        p.write(cmd, b"\0" * OPERATION_CREATE_SIZE)
        tt = g.malloc(8)
        p.write(tt, struct.pack("<ii", tgt.tag_id, 0))
        g.call(OPERATION_CREATE_CTOR, [cmd, db[key], _tag(g), tt, 0, 1 if commence else 0, 0, arr])
        ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
        api.allow_call(ce)
        s = ao._mkstring(g)
        ok = bool(g.call(ce, [cmd, s]) & 0xFF)
        why = ao._readstring(g, s)
        if ok and not dry_run:
            g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    if not ok:
        return {"error": (why or "the game refused it").strip()}
    return {"prepared": key, "against": target_tag, "operatives": names,
            "dry_run": dry_run}


OP_ORDERS = {}       # operative name -> "preparing <operation> against TAG", as ordered
_OPFILES = None
SKIP_OPS = ("rescue", "warsaw", "SIA_")


def op_files():
    """{operation key: {days, network, operatives, name, desc}} from common/operations."""
    global _OPFILES
    if _OPFILES is None:
        import glob
        import os
        import re
        from .bridge import game_dir
        from .events import loc
        _OPFILES = {}
        for f in glob.glob(os.path.join(game_dir(), "common", "operations", "*.txt")):
            txt = re.sub(r"#[^\n]*", "", open(f, encoding="utf-8-sig", errors="replace").read())
            heads = list(re.finditer(r"(?m)^(\w+)\s*=\s*\{", txt))
            for i, m in enumerate(heads):
                body = txt[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(txt)]

                def num(k):
                    x = re.search(r"(?m)^\t%s\s*=\s*([\d.]+)" % k, body)
                    return float(x.group(1)) if x else None
                k = m.group(1)
                _OPFILES[k] = {"days": num("days"), "network": num("network_strength"),
                               "operatives": num("operatives"), "name": loc(k) or k,
                               "desc": re.sub(r"\s+", " ", re.sub(r"[§£$][^ ]*", "", loc(k + "_desc")
                                                                  or ""))[:160]}
    return _OPFILES


def _can_prepare(g, key, tag_id):
    """The game's own check of an operation against a country (no operatives given)."""
    api, ao = B("api"), B("armyops")
    p = g.p
    cmd = g.malloc(OPERATION_CREATE_SIZE)
    p.write(cmd, b"\0" * OPERATION_CREATE_SIZE)
    arr = g.malloc(0x18)
    p.write(arr, b"\0" * 0x18)
    tt = g.malloc(8)
    p.write(tt, struct.pack("<ii", tag_id, 0))
    g.call(OPERATION_CREATE_CTOR, [cmd, operations_db(g)[key], _tag(g), tt, 0, 1, 0, arr])
    ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
    api.allow_call(ce)
    return bool(g.call(ce, [cmd, ao._mkstring(g)]) & 0xFF)


def open_operations(g, tags):
    """{tag: [operation keys the game allows now]}."""
    import re as _re
    db = operations_db(g)
    F = op_files()
    keys = [k for k in db if k.startswith("operation_") and k in F
            and not any(s in k for s in SKIP_OPS)]
    B("api").allow_call(OPERATION_CREATE_CTOR)
    own = g._inj is None
    if own:
        g.attach()
    out = {}
    try:
        for t in tags:
            try:
                tid = g._c(t).tag_id
            except Exception:
                continue
            ok = [k for k in keys if _can_prepare(g, k, tid)]
            if ok:
                out[t] = ok
    finally:
        if own:
            g.detach()
    return out


def operation_lines(g):
    import re as _re
    tags = sorted({m for v in list(ORDERS.values()) + list(OP_ORDERS.values())
                   for m in _re.findall(r"\b(?:in|against) ([A-Z]{3})\b", v)})
    if not tags:
        return []
    F = op_files()
    try:
        opn = open_operations(g, tags)
    except Exception as e:
        return ["OPERATIONS: could not be read (%s)" % e]
    rows = []
    busy = set(OP_ORDERS.values())
    for t in tags:
        ks = [k for k in opn.get(t, []) if "preparing %s against %s" % (k, t) not in busy]
        if ks:
            rows.append("%s: %s" % (t, "; ".join(
                "%s [%s] (%d operatives, %d days: %s)" % (
                    F[k]["name"], k, F[k]["operatives"] or 1, F[k]["days"] or 0, F[k]["desc"])
                for k in ks)))
        else:
            rows.append("%s: none yet (the network is too weak; most need 35-70%%)" % t)
    return ["OPERATIONS THE GAME ALLOWS NOW against countries where we build a network "
            "(prepare_operation: the operation, the country and as many free operatives as it "
            "needs; it starts by itself when prepared. Operatives on an operation leave their "
            "network, which then decays slowly; one operation a country at a time is plenty): "
            + " | ".join(rows)]


def prepare(g, operation, target_tag, names):
    """prepare_operation with the bookkeeping of who is busy."""
    key = operation if operation.startswith("operation_") else "operation_" + operation
    if "preparing %s against %s" % (key, target_tag) in OP_ORDERS.values():
        return {"error": "%s against %s is being prepared already" % (key, target_tag)}
    r = prepare_operation(g, operation, target_tag, names)
    if "prepared" in r:
        for n in names:
            ORDERS.pop(n, None)
            OP_ORDERS[n] = "preparing %s against %s" % (r["prepared"], target_tag)
    return r
