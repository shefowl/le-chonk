"""Faction goals (the 1.19 faction rework): read our faction's goals, list the goals the
game would let its leader add, add one. Found 25.10 on 1.19.3 (UK 1936, the Allies).

CCountry+0xF60 (CDiplomacyStatus) +0x280 -> CFaction (vtable 0x4353758).
CFaction+0x58: members (CPdxArray<CCountry*>, [0] the leader); +0x5F0 the goal set: its
records at +0x608 (0x530 bytes each, +0x0 the goal definition), count at +0x614.
Goal definition (vtable 0x4354C08): +0x8 name token, +0xCE0 category (0 short, 1 medium,
2 long, 4 the manifest). Triggers, 0x58 apart: completed +0x20, visible +0x78,
available +0xD0, allowed +0x128, cancel +0x230; +0x180 and +0x1D8 are EFFECTS
(complete_effect, select_effect): never call the functions that run them.
NFactions::CAddFactionGoalCommand (0x40 bytes, ctor 0xA06340): +0x24 tag, +0x28 goal,
+0x30 the goal it replaces (same category) or 0, +0x38 initiative cost (1 to replace an
unfinished goal). Its CanExecute (slot 10) checks the slot and 'available', not 'visible':
the visible check is 0x9D3A20 (goal, faction, scope), the scope ROOT the leader with FROM
the member at scope+0x20, as the CanExecute builds it.
"""
import glob
import os
import re
import struct
import time

from . import bridge as B

VT_FACTION, VT_GOAL = 0x4353758, 0x4354C08
CO_DIPLO, DS_FACTION = 0xF60, 0x2C8     # 1.19.3: 0x280
F_MEMBERS, F_GOALS, F_GOALS_N, GOAL_REC = 0x58, 0x608, 0x614, 0x530
GD_TOKEN, GD_CATEGORY = 0x8, 0xCE0
CATS = {0: "short_term", 1: "medium_term", 2: "long_term", 4: "manifest"}
SLOTS = {"short_term": 1, "medium_term": 1, "long_term": 1}   # NFactions MAX_NUM_*_GOALS
ADD_CTOR, ADD_SIZE, VT_CANEXE = 0xA06340, 0x40, 10
VISIBLE = 0x9D3A20
SCOPE_CTOR, SCOPE_SET, SCOPE_DTOR, SCOPE_SIZE, SCOPE_FROM = (0x1FEDFC0, 0x1FF02A0,
                                                            0x1FEE140, 0x100, 0x20)
REPLACE_COST = 1                     # NFactions REPLACING_UNFINISHED_FACTION_GOAL_COST

_DEFS = {}                           # pid -> (time, {name: ptr})


def faction_ptr(g, country_ptr=None):
    p = g.p
    c = country_ptr or g.player().ptr
    ds = p.u64(c + CO_DIPLO)
    f = p.u64(ds + DS_FACTION) if ds else 0
    return f if f and p.u64(f) == p.base + VT_FACTION else None


def _token(g, tok):
    from fieldmap import token_name
    return token_name(g, tok)


def goal_defs(g):
    """{name: definition ptr} for every faction goal; a heap scan (~7 s) kept per process."""
    p = g.p
    c = _DEFS.get(p.pid)
    if c and all(p.u64(d) == p.base + VT_GOAL for d in list(c[1].values())[:5]):
        return c[1]
    pat, out = struct.pack("<Q", p.base + VT_GOAL), {}
    for m in p.regions():
        if "rw" not in m["perms"] or m["path"] not in ("", "[heap]"):
            continue
        a = m["start"]
        while a < m["end"]:
            n = min(64 << 20, m["end"] - a)
            try:
                b = p.read(a, n)
            except OSError:
                a += n
                continue
            i = b.find(pat)
            while i != -1:
                if i % 8 == 0:
                    d = a + i
                    nm = _token(g, p.u32(d + GD_TOKEN))
                    if nm and p.i32(d + GD_CATEGORY) in CATS:
                        out[nm] = d
                i = b.find(pat, i + 1)
            a += n
    _DEFS[p.pid] = (time.time(), out)
    return out


def category(g, d):
    return CATS.get(g.p.i32(d + GD_CATEGORY), "?")


def members(g, f):
    from game import _arr
    return _arr(g.p, f, F_MEMBERS)


def active(g, f):
    """[(name, category, record ptr)] of the faction's goals, the manifest included."""
    p = g.p
    arr, n = p.u64(f + F_GOALS), p.i32(f + F_GOALS_N)
    out = []
    for i in range(max(0, min(n, 64))):
        r = arr + i * GOAL_REC
        d = p.u64(r)
        out.append((_token(g, p.u32(d + GD_TOKEN)), category(g, d), r))
    return out


def _scope(g, country_ptr):
    """A trigger scope on a country, built by the game's own constructor (as CanExecute)."""
    import api
    for fn in (SCOPE_CTOR, SCOPE_SET):
        api.allow_call(fn)
    s = g.malloc(SCOPE_SIZE)
    g.p.write(s, b"\0" * SCOPE_SIZE)
    g.call(SCOPE_CTOR, [s])
    g.call(SCOPE_SET, [s, country_ptr + 8, 0])
    return s


def _free_scope(g, s):
    import api
    api.allow_call(SCOPE_DTOR)
    g.call(SCOPE_DTOR, [s])


def visible(g, f, defs, member_ptr=None):
    """{name: bool} the goal's 'visible' trigger, ROOT the leader, FROM the member (us)."""
    import api
    api.allow_call(VISIBLE)
    leader = members(g, f)[0]
    frm = _scope(g, member_ptr or g.player().ptr)
    root = _scope(g, leader)
    g.p.write(root + SCOPE_FROM, struct.pack("<Q", frm))
    try:
        return {nm: bool(g.call(VISIBLE, [d, f, root]) & 0xFF) for nm, d in defs.items()}
    finally:
        g.p.write(root + SCOPE_FROM, b"\0" * 8)
        _free_scope(g, root)
        _free_scope(g, frm)


def _cmd(g, d, replace=0, cost=0):
    import api
    api.allow_call(ADD_CTOR)
    cmd = g.malloc(ADD_SIZE)
    g.p.write(cmd, b"\0" * ADD_SIZE)
    g.call(ADD_CTOR, [cmd, g.player().tag_id, d, replace, cost])
    return cmd


def _can(g, cmd):
    import api
    p = g.p
    fn = p.u64(p.u64(cmd) + 8 * VT_CANEXE) - p.base
    api.allow_call(fn)
    return bool(g.call(fn, [cmd, 0]) & 0xFF)


def options(g):
    """What our faction's leader (us) may put into each goal slot now:
    {"active": [(name, category)], "free": {category: [name]}, "swap": {category: [name]}}.
    'free' fill an empty slot at no cost; 'swap' replace the goal now in that slot."""
    goal_defs(g)                     # the heap scan (~7 s) before the game is stopped
    own = g._inj is None
    if own:
        g.attach()
    try:
        f = faction_ptr(g)
        if not f:
            return None
        defs = goal_defs(g)
        act = active(g, f)
        have = {nm for nm, _, _ in act}
        by_cat = {}
        for nm, cat, _ in act:
            by_cat.setdefault(cat, []).append(nm)
        cand = {nm: d for nm, d in defs.items() if nm not in have
                and category(g, d) in SLOTS}
        vis = visible(g, f, cand)
        free, swap = {}, {}
        for nm, d in cand.items():
            if not vis.get(nm):
                continue
            cat = category(g, d)
            if len(by_cat.get(cat, [])) < SLOTS[cat]:
                if _can(g, _cmd(g, d)):
                    free.setdefault(cat, []).append(nm)
            else:
                old = defs.get(by_cat[cat][0])
                if old and _can(g, _cmd(g, d, old, REPLACE_COST)):
                    swap.setdefault(cat, []).append(nm)
        return {"active": [(nm, cat) for nm, cat, _ in act], "free": free, "swap": swap,
                "leader": members(g, f)[0] == g.player().ptr}
    finally:
        if own:
            g.detach()


def add(g, name, replace=None):
    """Put goal `name` into its slot (replacing `replace`, a goal of the same category,
    for REPLACE_COST initiative). Returns (ok, why)."""
    goal_defs(g)
    own = g._inj is None
    if own:
        g.attach()
    try:
        f = faction_ptr(g)
        if not f:
            return False, "we are in no faction"
        if members(g, f)[0] != g.player().ptr:
            return False, "only the faction's leader sets its goals"
        defs = goal_defs(g)
        d = defs.get(name)
        if not d:
            return False, "no faction goal %s" % name
        if not visible(g, f, {name: d})[name]:
            return False, "%s is not open to our faction (its 'visible' is false)" % name
        old = 0
        if replace:
            old = defs.get(replace)
            if not old:
                return False, "no faction goal %s to replace" % replace
        cmd = _cmd(g, d, old, REPLACE_COST if old else 0)
        if not _can(g, cmd):
            return False, ("the game refuses: its slot is taken (name the goal to replace) "
                           "or its 'available' is false" if not old else
                           "the game refuses: %s is not ours to replace with it, or we lack "
                           "the faction initiative" % replace)
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own:
            g.detach()


# ------------------------------------------------------------------ the files
_FILE = None


_SIMPLER = [
    (r'custom_effect_tooltip = ("[^"]*"|[\w.\-]+)', ""),
    (r"visible_when_empty = yes", ""),
    (r"limit = \{ NOT = \{ has_completed_goal = [\w.\-]+ \} \}", ""),
    (r"count_in_collection = \{ collection = \{ input = game:scope operators = \{ "
     r"faction_members( limit = \{[^{}]*\})? \} name = \w+ \}", "faction_count = {"),
    (r"any_province_building_level = \{ province = \{ all_provinces = yes \} building = "
     r"(\w+) level > (\d+) \}", r"\1 > \2"),
    (r"is_controlled_by_ROOT_or_ally = yes", "held by us or an ally"),
    (r"input = \{ input = game:scope operators = \{ faction_members[^}]*\} name = \w+ \}",
     "faction members"),
]


def _clean(s):
    s = re.sub(r"#[^\n]*", "", s)
    s = re.sub(r"custom_effect_tooltip\s*=\s*generic_skip_one_line_tt", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    for a, b in _SIMPLER:
        s = re.sub(a, b, s)
    s = re.sub(r"\{\s*\}", "", s)
    return re.sub(r"\s+", " ", s).strip()


def info():
    """{name: {"title", "category", "needs", "gives"}} from common/factions/goals."""
    global _FILE
    if _FILE is None:
        from .doctrine_files import _blocks
        from .events import loc
        _FILE = {}
        base = os.path.join(B.game_dir(), "common", "factions", "goals")
        for path in glob.glob(os.path.join(base, "*.txt")):
            for key, body in _blocks(path):
                def part(k):
                    m = re.search(r"\b%s\s*=\s*\{" % k, body)
                    if not m:
                        return ""
                    from .spirits import _block_end
                    s = body[m.end() - 1:_block_end(body, m.end() - 1)]
                    s = _drop(s, "hidden_trigger")
                    s = _drop(s, "hidden_effect")
                    s = re.sub(r"tooltip\s*=\s*([\w.\-]+)",
                               lambda x: 'tooltip = "%s"' % loc(x.group(1)), s)
                    return _clean(s)[1:-1].strip()
                nm = re.search(r"\bname\s*=\s*([\w.\-]+)", body)
                ds = re.search(r"\bdescription\s*=\s*([\w.\-]+)", body)
                cat = re.search(r"\bcategory\s*=\s*(\w+)", body)
                needs = part("completed")
                if not needs or len(needs) > 400 or "variable" in needs:
                    needs = "(in the game's words) " + (loc(ds.group(1)) if ds else "?")
                _FILE[key] = {"title": loc(nm.group(1)) if nm else key,
                              "category": cat.group(1) if cat else None,
                              "needs": needs, "gives": part("complete_effect")}
    return _FILE


def _drop(s, word):
    from .spirits import _block_end
    while True:
        m = re.search(r"\b%s\s*=\s*\{" % word, s)
        if not m:
            return s
        s = s[:m.start()] + s[_block_end(s, m.end() - 1):]


def _short(s, n):
    return s if len(s) <= n else s[:n - 3] + "..."


def picture(g):
    """Lines for the briefing of the faction's leader (or a member: then read only)."""
    o = options(g)
    if o is None:
        return []
    I = info()
    lines = []
    for nm, cat in o["active"]:
        i = I.get(nm, {})
        lines.append("  %s %s (%s): needs %s" % (cat.upper(), nm, i.get("title", nm),
                                                _short(i.get("needs") or "-", 220)))
    if not o["leader"]:
        return ["OUR FACTION'S GOALS (the leader sets them):"] + lines
    for cat, n in SLOTS.items():
        k = sum(1 for _, c in o["active"] if c == cat)
        if k < n:
            lines.append("  EMPTY %s SLOT: fill it (set_faction_goal), free of cost" % cat.upper())
    out = ["OUR FACTION'S GOALS (we lead: a goal fulfilled gives the reward to the faction; "
           "the manifest's fulfilment makes the faction initiative):"] + lines
    for cat, names in sorted(o["free"].items()):
        out.append("FACTION GOALS THAT MAY FILL THE EMPTY %s SLOT (needs; gives):" % cat.upper())
        for nm in sorted(names):
            i = I.get(nm, {})
            out.append("  %s (%s): %s; GIVES %s" % (
                nm, i.get("title", nm), _short(i.get("needs") or "-", 160),
                _short(i.get("gives") or "-", 200)))
    for cat, names in sorted(o["swap"].items()):
        out.append("MAY REPLACE THE %s GOAL (%d initiative, its progress lost): %s" % (
            cat.upper(), REPLACE_COST, ", ".join("%s (%s)" % (nm, I.get(nm, {}).get("title", nm))
                                                 for nm in sorted(names))))
    return out


# ------------------------------------------------------------------ rules (26.10)
# NFactions::CFactionRule (vtable 0x4355798): +0x8 name token, +0x18 type token; triggers
# 0x58 apart (count at +0x14 of each): visible +0x20, available +0x78, can_remove +0xD0.
# CFaction+0x5A8: the active rules (CPdxArray<CFactionRule*>). CSetFactionRuleCommand and
# CEraseFactionRuleCommand (0x30 bytes, ctors 0x9FEB60 / 0x9FF430: (this, &tag, rule
# token)); their CanExecute checks only the initiative (1 a change), so the group and the
# triggers are checked here. The faction initiative: [0xDA3650(&tag)+0x48], fixed 1e5.
VT_RULE, F_RULES = 0x4355798, 0x5A8
R_TYPE, R_VISIBLE, R_AVAILABLE, R_CAN_REMOVE, TRIG_COUNT = 0x18, 0x20, 0x78, 0xD0, 0x14
SET_RULE, ERASE_RULE, RULE_SIZE = 0x9FEB60, 0x9FF430, 0x30
TRIGGER = 0x134F3C0                 # (trigger*, scope*) -> bool
MEMBER_DATA, MD_INITIATIVE = 0xDA3650, 0x48
RULE_COST = 1                       # NFactions FACTION_INITIATIVE_CHANGE_RULE_COST
_RULES = {}


def initiative(g):
    import api
    api.allow_call(MEMBER_DATA)
    t = g.malloc(4)
    g.p.write(t, struct.pack("<i", g.player().tag_id))
    x = g.call(MEMBER_DATA, [t])
    return g.p.i64(x + MD_INITIATIVE) / 1e5 if x else None


def rule_defs(g):
    """{name: CFactionRule ptr}, a heap scan kept per process."""
    p = g.p
    c = _RULES.get(p.pid)
    if c and all(p.u64(d) == p.base + VT_RULE for d in list(c.values())[:5]):
        return c
    pat, out = struct.pack("<Q", p.base + VT_RULE), {}
    for m in p.regions():
        if "rw" not in m["perms"] or m["path"] not in ("", "[heap]"):
            continue
        a = m["start"]
        while a < m["end"]:
            n = min(64 << 20, m["end"] - a)
            try:
                b = p.read(a, n)
            except OSError:
                a += n
                continue
            i = b.find(pat)
            while i != -1:
                if i % 8 == 0:
                    nm = _token(g, p.u32(a + i + 8))
                    if nm and not nm.startswith("original_"):
                        out[nm] = a + i
                i = b.find(pat, i + 1)
            a += n
    _RULES[p.pid] = out
    return out


def active_rules(g, f):
    from game import _arr
    return [_token(g, g.p.u32(r + 8)) for r in _arr(g.p, f, F_RULES)]


def _trig(g, obj, off, scope):
    """A trigger block of a game object; an empty one is true."""
    if g.p.i32(obj + off + TRIG_COUNT) == 0:
        return True
    import api
    api.allow_call(TRIGGER)
    return bool(g.call(TRIGGER, [obj + off, scope]) & 0xFF)


_GROUPS = None


def rule_groups():
    """{group: [rule]} from common/factions/rules/groups."""
    global _GROUPS
    if _GROUPS is None:
        from .doctrine_files import _blocks
        _GROUPS = {}
        for path in glob.glob(os.path.join(B.game_dir(), "common", "factions", "rules",
                                           "groups", "*.txt")):
            for key, body in _blocks(path):
                m = re.search(r"\brules\s*=\s*\{([^}]*)\}", body)
                if m:
                    _GROUPS[key] = re.findall(r"[\w.\-]+", re.sub(r"#[^\n]*", "", m.group(1)))
    return _GROUPS


_RINFO = None


def rule_info():
    """{rule: {"type", "text"}}: what a rule does, from its file (modifier, trigger)."""
    global _RINFO
    if _RINFO is None:
        from .doctrine_files import _blocks
        from .spirits import _block_end
        from .events import loc
        _RINFO = {}
        for path in glob.glob(os.path.join(B.game_dir(), "common", "factions", "rules",
                                           "*.txt")):
            for key, body in _blocks(path):
                parts = []
                body = _drop(body, "ai_will_do")
                for k in ("trigger", "modifier", "effect", "dismiss_member_trigger",
                          "peace_action_modifiers"):
                    m = re.search(r"\b%s\s*=\s*\{" % k, body)
                    if m:
                        s = body[m.end() - 1:_block_end(body, m.end() - 1)]
                        s = re.sub(r"tooltip\s*=\s*([\w.\-]+)",
                                   lambda x: 'tooltip = "%s"' % loc(x.group(1)), s)
                        s = _clean(_drop(s, "hidden_effect"))
                        if s.strip("{} "):
                            parts.append("%s %s" % (k, _short(s, 200)))
                t = re.search(r"\btype\s*=\s*(\w+)", body)
                _RINFO[key] = {"type": t.group(1) if t else None, "title": loc(key),
                               "text": "; ".join(parts) or "-"}
    return _RINFO


def rule_options(g):
    """Per rule group: the active rule, whether it may be removed, the rules it may be
    changed to (visible and available for us, the leader). {group: {...}}"""
    rule_defs(g)                     # the heap scan before the game is stopped
    own = g._inj is None
    if own:
        g.attach()
    try:
        f = faction_ptr(g)
        if not f or members(g, f)[0] != g.player().ptr:
            return None
        R = rule_defs(g)
        act = set(active_rules(g, f))
        root = _scope(g, g.player().ptr)
        try:
            out = {}
            for grp, rules in rule_groups().items():
                rules = [r for r in rules if r in R]
                cur = [r for r in rules if r in act]
                vis = [r for r in rules if r not in act and _trig(g, R[r], R_VISIBLE, root)]
                ok = [r for r in vis if _trig(g, R[r], R_AVAILABLE, root)]
                out[grp] = {"active": cur, "removable": all(
                    _trig(g, R[r], R_CAN_REMOVE, root) for r in cur),
                    "may_set": ok, "not_yet": [r for r in vis if r not in ok]}
            return {"groups": out, "initiative": initiative(g)}
        finally:
            _free_scope(g, root)
    finally:
        if own:
            g.detach()


def set_rule(g, name, erase=False):
    """Set (or erase) a faction rule as the leader. Returns (ok, why)."""
    import api
    rule_defs(g)
    own = g._inj is None
    if own:
        g.attach()
    try:
        f = faction_ptr(g)
        if not f or members(g, f)[0] != g.player().ptr:
            return False, "only the faction's leader sets its rules"
        R = rule_defs(g)
        if name not in R:
            return False, "no faction rule %s" % name
        act = set(active_rules(g, f))
        grp = next((k for k, v in rule_groups().items() if name in v), None)
        root = _scope(g, g.player().ptr)
        try:
            if erase:
                if name not in act:
                    return False, "%s is not in force" % name
                if not _trig(g, R[name], R_CAN_REMOVE, root):
                    return False, "the game does not let %s be removed now" % name
            else:
                if name in act:
                    return False, "%s is in force already" % name
                if not _trig(g, R[name], R_VISIBLE, root):
                    return False, "%s is not open to our faction" % name
                if not _trig(g, R[name], R_AVAILABLE, root):
                    return False, "%s is not available yet (its condition is unmet)" % name
                old = [r for r in rule_groups().get(grp, []) if r in act]
                if any(not _trig(g, R[r], R_CAN_REMOVE, root) for r in old):
                    return False, "the rule now in its group (%s) may not be removed now" % old
        finally:
            _free_scope(g, root)
        fi = initiative(g)
        if fi is not None and fi < RULE_COST:
            return False, "a rule change costs %d faction initiative; we have %.2f" % (
                RULE_COST, fi)
        ctor = ERASE_RULE if erase else SET_RULE
        api.allow_call(ctor)
        t = g.malloc(4)
        g.p.write(t, struct.pack("<i", g.player().tag_id))
        cmd = g.malloc(RULE_SIZE)
        g.p.write(cmd, b"\0" * RULE_SIZE)
        g.call(ctor, [cmd, t, g.p.u32(R[name] + 8)])
        if not _can(g, cmd):
            return False, "the game refuses (faction initiative?)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own:
            g.detach()


# ------------------------------------------------------------------ member upgrades (26.10)
# NFactions::CFactionMemberUpgradeGroup (vtable 0x4356EC8): +0x8 name token; its upgrades
# at +0x78 (16-byte slots, the item pointer first; count +0x80); +0xF0 the upgrade type
# (the key below). An upgrade item: +0x8 name token. Our current upgrade per type: the
# member data (0xDA3650(&tag)) +0x58, a hash map (24-byte entries: +0x4 used, +0x8 key,
# +0x10 the upgrade item; mask +0x64). CSetFactionUpgradeCommand (0x38 bytes, ctor
# 0x9FFA60: (this, &tag, upgrade item)). The only group in 1.19.3: manpower contribution
# (0/5/10/15% of the monthly manpower gain to the faction's garrison pool; influence).
VT_UPGROUP, UG_ITEMS, UG_COUNT, UG_TYPE = 0x4356EC8, 0x78, 0x80, 0xF0
MD_UPGRADES, MD_UP_MASK = 0x58, 0x64
SET_UPGRADE, UPGRADE_SIZE = 0x9FFA60, 0x38


def _member_data(g, tag_id=None):
    import api
    api.allow_call(MEMBER_DATA)
    t = g.malloc(4)
    g.p.write(t, struct.pack("<i", g.player().tag_id if tag_id is None else tag_id))
    return g.call(MEMBER_DATA, [t])


_UPG = {}


def upgrade_groups(g):
    """{group: {"type": key, "items": {name: ptr}}} found on the heap (kept per process)."""
    p = g.p
    c = _UPG.get(p.pid)
    if c and all(p.u64(it) and p.u32(it + 8) for v in c.values() for it in v["items"].values()):
        return c
    pat, out = struct.pack("<Q", p.base + VT_UPGROUP), {}
    for m in p.regions():
        if "rw" not in m["perms"] or m["path"] not in ("", "[heap]"):
            continue
        a = m["start"]
        while a < m["end"]:
            n = min(64 << 20, m["end"] - a)
            try:
                b = p.read(a, n)
            except OSError:
                a += n
                continue
            i = b.find(pat)
            while i != -1:
                if i % 8 == 0:
                    gp = a + i
                    k = p.i32(gp + UG_COUNT)
                    items = {}
                    if 0 < k < 32:
                        d = p.u64(gp + UG_ITEMS)
                        for j in range(k):
                            it = p.u64(d + 16 * j)
                            items[_token(g, p.u32(it + 8))] = it
                    out[_token(g, p.u32(gp + 8))] = {"type": p.i32(gp + UG_TYPE), "items": items}
                i = b.find(pat, i + 1)
            a += n
    _UPG[p.pid] = out
    return out


def current_upgrades(g, md=None):
    """{type key: upgrade item ptr} of our member data."""
    p = g.p
    md = md or _member_data(g)
    data, mask = p.u64(md + MD_UPGRADES), p.i32(md + MD_UP_MASK)
    out = {}
    if not data or not 0 <= mask < 1024:
        return out
    for i in range(mask + 1 + 16):
        e = data + 24 * i
        try:
            if p.u8(e + 4):
                out[p.i32(e + 8)] = p.u64(e + 0x10)
        except OSError:
            break
    return out


def upgrades(g):
    """[{"group", "current", "choices"}] for us."""
    upgrade_groups(g)
    own = g._inj is None
    if own:
        g.attach()
    try:
        if not faction_ptr(g):
            return []
        cur = current_upgrades(g)
        out = []
        for grp, v in upgrade_groups(g).items():
            now = cur.get(v["type"])
            name = next((n for n, pt in v["items"].items() if pt == now), None)
            out.append({"group": grp, "current": name or "the default",
                        "choices": list(v["items"])})
        return out
    finally:
        if own:
            g.detach()


def set_upgrade(g, name):
    """Choose our member upgrade `name` (e.g. faction_manpower_sharing1)."""
    import api
    upgrade_groups(g)
    own = g._inj is None
    if own:
        g.attach()
    try:
        if not faction_ptr(g):
            return False, "we are in no faction"
        it = None
        for v in upgrade_groups(g).values():
            it = v["items"].get(name) or it
        if not it:
            return False, "no faction member upgrade %s" % name
        api.allow_call(SET_UPGRADE)
        t = g.malloc(4)
        g.p.write(t, struct.pack("<i", g.player().tag_id))
        cmd = g.malloc(UPGRADE_SIZE)
        g.p.write(cmd, b"\0" * UPGRADE_SIZE)
        g.call(SET_UPGRADE, [cmd, t, it])
        if not _can(g, cmd):
            return False, "the game refuses (faction initiative?)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own:
            g.detach()


_UPLOC = {"manpower_contribution_full_enable": "0%", "faction_manpower_sharing1": "5%",
          "faction_manpower_sharing2": "10%", "faction_manpower_sharing3": "15%"}


def rules_picture(g):
    """Lines on the faction's rules, initiative and our member upgrade."""
    o = rule_options(g)
    lines = []
    if o:
        I = rule_info()
        lines.append("FACTION INITIATIVE: %.2f (a rule change costs %d; replacing an unfinished "
                     "goal %d; it grows with the manifest's fulfilment and our influence)"
                     % (o["initiative"] or 0, RULE_COST, REPLACE_COST))
        lines.append("FACTION RULES (we lead; one rule a group; set_faction_rule replaces the "
                     "group's rule, erase=true removes it):")
        for grp, v in sorted(o["groups"].items()):
            if not (v["active"] or v["may_set"]):
                continue
            act = ", ".join("%s (%s)" % (r, I.get(r, {}).get("title", r)) for r in v["active"])
            lines.append("  %s: IN FORCE %s%s" % (grp, act or "none",
                         "" if v["removable"] else " (may not be removed now)"))
            for r in v["may_set"]:
                i = I.get(r, {})
                lines.append("    may set %s (%s): %s" % (r, i.get("title", r),
                                                          _short(i.get("text", "-"), 220)))
    for u in upgrades(g):
        lines.append("OUR MEMBER UPGRADE %s: now %s; choices %s (set_member_upgrade). %s"
                     % (u["group"], _UPLOC.get(u["current"], u["current"]),
                        ", ".join("%s (%s)" % (c, _UPLOC.get(c, c)) for c in u["choices"]),
                        "Manpower contribution: that share of our monthly manpower gain goes "
                        "to the faction's garrison pool and raises our influence in it."
                        if "manpower" in u["group"] else ""))
    return lines
