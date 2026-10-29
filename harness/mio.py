"""Military industrial organisations (MIOs): which one works each production line, the
traits they unlock with their points, the policies attached to them.

Found 24.10 on Japan 1937 (the user: the arsenals stood untouched, Kure with 2 trait
points). Country +0xF40 CProductionStatus: +0x58 production lines, +0x130 our
COrganisation*, +0x148 CPolicy*. COrganisation: +0x08 handle {79, id}, +0x38 key token,
+0x128 trait points earned, +0x12C not yet spent, +0x1A0 the attached
policy's token, +0x188 i32 tokens of the policies it may take (count +0x194),
+0x1CC the line of its definition. Production line: +0x08 handle {56|57, id}, +0x90 the
organisation's handle (0 = none).
Commands (CanExecute is the vtable's +80 for all):
  CSetIndustrialManufacturerCommand ctor 0xF56F50 (this, &our tag, &line handle, COrg*)
  CUnlockIndustrialOrganisationTrait (no ctor of its own): +0x24 our tag, +0x28 a token
    holder (vtable 0x4399E50), +0x30 trait token, +0x38 organisation handle
  CAttachPolicyToIndustrialOrgCommand: +0x24 our tag, +0x28 policy token, +0x2C org handle
The trait trees come from the game's files (common/military_industrial_organization):
an organisation includes a generic one and adds or overrides traits.
"""
import glob
import os
import re
import struct

from .bridge import module as B

PS = 0xF40
PS_LINES, PS_ORGS = 0x58, 0x130
ORG_HANDLE, ORG_TOKEN, ORG_POINTS, ORG_POLICIES = 0x08, 0x38, 0x12C, 0x188
ORG_POLICY = 0x1A0          # the attached policy's token ("undefined" = none)
LINE_HANDLE, LINE_MIO = 0x08, 0x90
SET_CTOR = 0xF56F50
UNLOCK_VT, TOKEN_VT, ATTACH_VT = 0x4398FF8, 0x4399E50, 0x43990D8
POLICY_COST, POLICY_COOLDOWN = 25, 180       # NDefines DEFAULT_INITIAL_POLICY_ATTACH_*


def _arr(p, base, off):
    data, n = p.u64(base + off), p.i32(base + off + 0xC)
    return [p.u64(data + 8 * k) for k in range(n if 0 < n < 5000 else 0)]


def _tok(g, t):
    return B("fieldmap").token_name(g, t)


def organisations(g):
    """[{key, ptr, handle, points, policies: [tokens]}]"""
    p = g.p
    ps = p.u64(g.player().ptr + PS)
    out = []
    for o in _arr(p, ps, PS_ORGS):
        n = p.i32(o + ORG_POLICIES + 0xC)
        data = p.u64(o + ORG_POLICIES)
        pol = [p.i32(data + 4 * k) for k in range(n if 0 < n < 100 else 0)]
        pt = _tok(g, p.i32(o + ORG_POLICY))
        out.append({"key": _tok(g, p.i32(o + ORG_TOKEN)), "ptr": o,
                    "policy": pt if pt and pt != "undefined" else None,
                    "handle": p.read(o + ORG_HANDLE, 8), "points": p.i32(o + ORG_POINTS),
                    "policies": [(t, _tok(g, t)) for t in pol]})
    return out


def lines(g):
    """[{index, equipment, ptr, handle, org (key or None)}] in the order of the game's lines."""
    p = g.p
    ps = p.u64(g.player().ptr + PS)
    by_handle = {o["handle"]: o["key"] for o in organisations(g)}
    pl = g.production_lines()
    out = []
    for i, L in enumerate(_arr(p, ps, PS_LINES)):
        h = p.read(L + LINE_MIO, 8)
        out.append({"index": i, "equipment": pl[i]["equipment"] if i < len(pl) else "?",
                    "ptr": L, "handle": p.read(L + LINE_HANDLE, 8),
                    "org": by_handle.get(h) if h != b"\0" * 8 else None})
    return out


# ------------------------------------------------------------------ commands
def _base(g, size, vt):
    p = g.p
    cmd = g.malloc(size)
    p.write(cmd, b"\0" * size)
    p.write(cmd, struct.pack("<Q", p.base + vt))
    p.write(cmd + 0x0C, struct.pack("<IIIH", 0xFFFFFFFF, 0, 0xFFFF0000, 0))
    return cmd


def _run(g, build, send):
    api, ao = B("api"), B("armyops")
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd = build()
        p = g.p
        ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
        api.allow_call(ce)
        st = ao._mkstring(g)
        ok = bool(g.call(ce, [cmd, st]) & 0xFF)
        why = ao._readstring(g, st)
        if ok and send:
            g.cmd.post(cmd)
        return ok, why
    finally:
        if own:
            g.detach()


def _tag(g):
    t = g.malloc(8)
    g.p.write(t, struct.pack("<ii", g._c(None).tag_id, 0))
    return t


def assign(g, line_ptr, line_handle, org_ptr, send=True):
    """Put an organisation on a production line (org_ptr 0: take it off)."""
    def build():
        api = B("api")
        api.allow_call(SET_CTOR)
        cmd = g.malloc(0x40)
        g.p.write(cmd, b"\0" * 0x40)
        lh = g.malloc(8)
        g.p.write(lh, line_handle)
        g.call(SET_CTOR, [cmd, _tag(g), lh, org_ptr])
        return cmd
    return _run(g, build, send)


def unlock(g, org_handle, trait_token, send=True):
    def build():
        cmd = _base(g, 0x40, UNLOCK_VT)
        p = g.p
        p.write(cmd + 0x24, struct.pack("<i", g._c(None).tag_id))
        p.write(cmd + 0x28, struct.pack("<Qi", p.base + TOKEN_VT, trait_token))
        p.write(cmd + 0x38, org_handle)
        return cmd
    return _run(g, build, send)


def attach_policy(g, org_handle, policy_token, send=True):
    def build():
        cmd = _base(g, 0x38, ATTACH_VT)
        p = g.p
        p.write(cmd + 0x24, struct.pack("<ii", g._c(None).tag_id, policy_token))
        p.write(cmd + 0x2C, org_handle)
        return cmd
    return _run(g, build, send)


# ------------------------------------------------------------------ the files
_DEFS = None
_POLICIES = None


def _block(txt, start):
    from .spirits import _block_end
    return txt[start:_block_end(txt, start)]


def _bonus(body):
    """'stat +x, stat -y' from a trait's or policy's bonus blocks."""
    out = []
    for k in ("equipment_bonus", "production_bonus", "organization_modifier"):
        for m in re.finditer(r"\b%s\s*=\s*\{" % k, body):
            b = _block(body, m.end() - 1)
            for s, v in re.findall(r"\b([a-z_]+)\s*=\s*(-?[\d.]+)", b):
                out.append("%s %+g" % (s, float(v)))
    return ", ".join(out)


def _traits(body):
    out = {}
    for m in re.finditer(r"(?m)^\s*(initial_trait|trait|add_trait|override_trait)\s*=\s*\{",
                         body):
        b = _block(body, m.end() - 1)
        t = re.search(r"\btoken\s*=\s*([\w.\-]+)", b)
        if not t:
            continue
        name = re.search(r"\bname\s*=\s*([\w.\-]+)", b)
        parents = re.findall(r"\b(?:all_parents|any_parent)\s*=\s*\{([^}]*)\}", b)
        excl = re.findall(r"\bmutually_exclusive\s*=\s*\{([^}]*)\}", b)
        out[t.group(1)] = {"name": name.group(1) if name else t.group(1),
                           "initial": m.group(1) == "initial_trait",
                           "parents": " ".join(parents).split(),
                           "excl": " ".join(excl).split(), "bonus": _bonus(b)}
    return out


def definitions():
    """{organisation key: {token: trait}} with the generic one included."""
    global _DEFS
    if _DEFS is None:
        raw = {}
        d = os.path.join(B("config").game_dir(), "common", "military_industrial_organization",
                         "organizations")
        for path in glob.glob(os.path.join(d, "*.txt")):
            txt = open(path, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^([\w.\-]+)\s*=\s*\{", txt):
                raw[m.group(1)] = _block(txt, m.end() - 1)
        _DEFS = {}
        for key, body in raw.items():
            tr = {}
            inc = re.search(r"\binclude\s*=\s*([\w.\-]+)", body)
            if inc and inc.group(1) in raw:
                tr.update(_traits(raw[inc.group(1)]))
            tr.update(_traits(body))
            for r in re.findall(r"\bremove_trait\s*=\s*([\w.\-]+)", body):
                tr.pop(r, None)
            _DEFS[key] = tr
    return _DEFS


def policies():
    """{policy key: bonus text}"""
    global _POLICIES
    if _POLICIES is None:
        _POLICIES = {}
        d = os.path.join(B("config").game_dir(), "common", "military_industrial_organization",
                         "policies")
        for path in glob.glob(os.path.join(d, "*.txt")):
            txt = open(path, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^([\w.\-]+)\s*=\s*\{", txt):
                _POLICIES[m.group(1)] = _bonus(_block(txt, m.end() - 1))
    return _POLICIES


_TOKID = {}


def token_id(g, name):
    """A token's id from its name (the token table read once, by length)."""
    if name in _TOKID:
        return _TOKID[name]
    FM = B("fieldmap")
    p = g.p
    n = p.i32(p.base + FM.G_TOKEN_COUNT)
    base = p.u64(p.base + FM.G_TOKEN_TABLE)
    want = {name}
    raw = p.read(base, 0x20 * n)
    for i in range(n):
        ptr, ln = struct.unpack_from("<QQ", raw, 0x20 * i)
        if ln != len(name):
            continue
        s = raw[0x20 * i + 0x10:0x20 * i + 0x10 + ln] if ln < 16 else p.read(ptr, ln)
        if s.decode(errors="replace") in want:
            _TOKID[name] = i
            return i
    return None


# ------------------------------------------------------------------ what can be done now
def open_traits(g, org):
    """[(token, name, bonus)] the game lets this organisation unlock now."""
    from .events import loc
    out = []
    for tok, t in definitions().get(org["key"], {}).items():
        if t["initial"]:
            continue
        tid = token_id(g, tok)
        if tid is None:
            continue
        ok, _ = unlock(g, org["handle"], tid, send=False)
        if ok:
            out.append((tok, loc(t["name"]) or t["name"], t["bonus"]))
    return out


def eligible(g, line, orgs):
    """The organisations the game lets work this line."""
    return [o for o in orgs if assign(g, line["ptr"], line["handle"], o["ptr"], send=False)[0]]


def short(key):
    """'JAP_kure_naval_arsenal_organization' -> 'kure_naval_arsenal'"""
    return re.sub(r"_organization$", "", re.sub(r"^[A-Z]{3}_", "", key or ""))


def find(orgs, name):
    name = (name or "").strip().lower()
    for o in orgs:
        if name in (o["key"].lower(), short(o["key"]).lower()):
            return o
    return None


# ------------------------------------------------------------------ which lines an organisation may work
# CanExecute of the assignment lets any organisation on any line (24.10: all 24 on a
# train line); the game's UI filters by the organisation's equipment_type, matched here:
# an archetype (ship_hull_submarine), an equipment type (capital_ship, armor) or a group
# of common/equipment_groups/mio_equipment_groups.txt (mio_cat_eq_all_cruiser).
_EQ = None
_GROUPS = None
_ORG_EQ = None


def _equipment():
    """{equipment: (archetype, {types})}"""
    global _EQ
    if _EQ is None:
        _EQ = {}
        d = os.path.join(B("config").game_dir(), "common", "units", "equipment")
        for path in glob.glob(os.path.join(d, "*.txt")):
            txt = open(path, encoding="utf-8-sig", errors="replace").read()
            m0 = re.search(r"\bequipments\s*=\s*\{", txt)
            if not m0:
                continue
            body = _block(txt, m0.end() - 1)
            for m in re.finditer(r"(?m)^\t([\w.\-]+)\s*=\s*\{", body):
                b = _block(body, m.end() - 1)
                a = re.search(r"\barchetype\s*=\s*([\w.\-]+)", b)
                ts = set(re.findall(r"\btype\s*=\s*([\w.\-]+)", b))
                for blk in re.findall(r"\btype\s*=\s*\{([^}]*)\}", b):
                    ts |= set(blk.split())
                _EQ[m.group(1)] = (a.group(1) if a else None, ts)
    return _EQ


def _groups():
    global _GROUPS
    if _GROUPS is None:
        _GROUPS = {}
        f = os.path.join(B("config").game_dir(), "common", "equipment_groups",
                         "mio_equipment_groups.txt")
        txt = open(f, encoding="utf-8-sig", errors="replace").read()
        for m in re.finditer(r"(?m)^([\w.\-]+)\s*=\s*\{", txt):
            b = _block(txt, m.end() - 1)
            e = re.search(r"\bequipment_type\s*=\s*\{([^}]*)\}", b)
            _GROUPS[m.group(1)] = set(re.sub(r"#[^\n]*", "", e.group(1)).split()) if e else set()
    return _GROUPS


def tags_of(equipment):
    """What an organisation's equipment_type may name for this equipment."""
    eq = _equipment()
    a, ts = eq.get(equipment, (None, set()))
    out = {equipment} | ts
    base = re.sub(r"_\d+$", "", equipment)     # cv_small_plane_naval_bomber_airframe_1
    if base != equipment:
        out |= {base} | eq.get(base, (None, set()))[1]
    if a:
        out |= {a} | eq.get(a, (None, set()))[1]
    out |= {g for g, members in _groups().items() if members & out}
    return out


def org_equipment(key):
    global _ORG_EQ
    if _ORG_EQ is None:
        _ORG_EQ = {}
        raw = {}
        d = os.path.join(B("config").game_dir(), "common", "military_industrial_organization",
                         "organizations")
        for path in glob.glob(os.path.join(d, "*.txt")):
            txt = open(path, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^([\w.\-]+)\s*=\s*\{", txt):
                raw[m.group(1)] = _block(txt, m.end() - 1)

        def own(b):
            # equipment_type at the organisation's top level, not a trait's limit
            m = re.search(r"(?m)^\tequipment_type\s*=\s*\{([^}]*)\}", b)
            return set(re.sub(r"#[^\n]*", "", m.group(1)).split()) if m else None
        for k, b in raw.items():
            e = own(b)
            if e is None:
                inc = re.search(r"\binclude\s*=\s*([\w.\-]+)", b)
                e = own(raw.get(inc.group(1), "")) if inc else None
            _ORG_EQ[k] = e or set()
    return _ORG_EQ.get(key, set())


def fits(org_key, equipment):
    return bool(org_equipment(org_key) & tags_of(equipment))


# ------------------------------------------------------------------ for the agents
def industry_lines(g):
    """The minister's picture: lines without an organisation, trait points to spend."""
    orgs = organisations(g)
    out = []
    free = []
    for l in lines(g):
        if l["org"]:
            continue
        fit = [short(o["key"]) for o in orgs if fits(o["key"], l["equipment"])]
        if fit:
            free.append("%d %s: %s" % (l["index"], l["equipment"], ", ".join(fit)))
    if free:
        out.append("LINES WITHOUT AN ORGANISATION (assign_organisation; the organisation's "
                   "traits then add their bonuses to the line, it costs nothing): "
                   + "; ".join(free))
    work = {}
    for l in lines(g):
        if l["org"]:
            work[l["org"]] = work.get(l["org"], 0) + 1
    pts = []
    for o in orgs:
        if o["points"] <= 0:
            continue
        tr = open_traits(g, o)
        if tr:
            pts.append("%s, %d point%s, works %d line%s: %s" % (
                short(o["key"]), o["points"], "s" if o["points"] > 1 else "",
                work.get(o["key"], 0), "s" if work.get(o["key"], 0) != 1 else "",
                "; ".join("%s '%s' [%s]" % (t, n, b or "-") for t, n, b in tr)))
    if pts:
        out.append("TRAIT POINTS TO SPEND (unlock_trait, organisation and trait token):\n- "
                   + "\n- ".join(pts))
    return out


def politics_lines(g):
    """Policies for the organisations that work our lines."""
    orgs = organisations(g)
    work = {}
    for l in lines(g):
        if l["org"]:
            work[l["org"]] = work.get(l["org"], 0) + 1
    pol = policies()
    rows = []
    for o in sorted(orgs, key=lambda o: -work.get(o["key"], 0)):
        n = work.get(o["key"], 0)
        if not n:
            continue
        rows.append("%s (%d line%s), now %s; may take: %s" % (
            short(o["key"]), n, "s" if n > 1 else "", o["policy"] or "NONE",
            ", ".join("%s [%s]" % (k, pol.get(k) or "-") for _, k in o["policies"]
                      if k != o["policy"])))
    if not rows:
        return []
    return ["ORGANISATION POLICIES (attach_policy: %d political power, then %d days before it "
            "may change; the bonus works on the organisation's lines):\n- " % (
                POLICY_COST, POLICY_COOLDOWN) + "\n- ".join(rows)]


def alarm(g):
    """Why the industry minister must act: a line with no organisation though one fits,
    an organisation with a trait point and a trait open."""
    orgs = organisations(g)
    why = []
    n = sum(1 for l in lines(g) if not l["org"]
            and any(fits(o["key"], l["equipment"]) for o in orgs))
    if n:
        why.append("%d production line%s without an organisation" % (n, "s" if n > 1 else ""))
    p = sum(o["points"] for o in orgs if o["points"] > 0 and open_traits(g, o))
    if p:
        why.append("%d trait point%s of our organisations unspent" % (p, "s" if p > 1 else ""))
    return why


def do_assign(g, line_index, org_name):
    orgs = organisations(g)
    L = lines(g)
    if not 0 <= int(line_index) < len(L):
        return {"error": "no line %s" % line_index}
    line = L[int(line_index)]
    o = find(orgs, org_name)
    if not o:
        return {"error": "no organisation %s of ours" % org_name}
    if not fits(o["key"], line["equipment"]):
        return {"error": "%s does not make %s; these do: %s" % (
            short(o["key"]), line["equipment"],
            ", ".join(short(x["key"]) for x in orgs if fits(x["key"], line["equipment"]))
            or "none of ours")}
    ok, why = assign(g, line["ptr"], line["handle"], o["ptr"])
    return {"assigned": short(o["key"]), "line": int(line_index)} if ok else \
        {"error": why or "the game refused it"}


def do_unlock(g, org_name, trait):
    o = find(organisations(g), org_name)
    if not o:
        return {"error": "no organisation %s of ours" % org_name}
    if o["points"] <= 0:
        return {"error": "%s has no trait point" % short(o["key"])}
    tid = token_id(g, trait)
    if tid is None:
        return {"error": "no trait %s" % trait}
    ok, why = unlock(g, o["handle"], tid)
    return {"unlocked": trait, "organisation": short(o["key"])} if ok else \
        {"error": why or "not open now (its parents first, or excluded)"}


def do_attach(g, org_name, policy):
    o = find(organisations(g), org_name)
    if not o:
        return {"error": "no organisation %s of ours" % org_name}
    if o["policy"] == policy:
        return {"error": "%s already has %s" % (short(o["key"]), policy)}
    tok = next((t for t, k in o["policies"] if k == policy), None)
    if tok is None:
        return {"error": "%s may not take %s; it may take: %s" % (
            short(o["key"]), policy, ", ".join(k for _, k in o["policies"]))}
    ok, why = attach_policy(g, o["handle"], tok)
    return {"attached": policy, "organisation": short(o["key"])} if ok else \
        {"error": why or "the game refused it (political power, or the cooldown)"}
