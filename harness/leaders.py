"""Traits of generals and admirals: the assignable ones, learnt for command power.

Found 22.10 on Japan 1936:
- CUnitLeaderTrait objects (vtable 0x43D6328) hold their name token at +0x08; the game
  keeps them in a heap array (194 on 1.19.3), found here by scanning for the vtable.
- A leader's traits: CArmyLeader (= character +0xC0) +0xCC8, CPdxArray<CUnitLeaderTrait*>.
- CLearnTraitCommand ctor 0x2337CD0 (this, CArmyLeader*, CUnitLeaderTrait*); CanExecute
  (vtable slot 10) writes the game's reason: "not enough command power to assign trait",
  "All parent traits must be unlocked", "Field Marshal traits are only assignable to Field
  Marshals", "A mutually exclusive trait is already assigned".
"""
import glob
import os
import re

from .bridge import module as B

VT_TRAIT = 0x43D6328
TR_TOKEN = 0x08
LD_TRAITS = 0xCC8
LD_RANK = 0xD7C          # 0 field marshal, 1 general, 2 admiral
LEARN_CTOR = 0x2337CD0

_DB = {}
_FILES = None
_TOKENS = None


def _tokens(g):
    from .tokens import table
    return table(g)


def assignable():
    """{trait: short effect text} of the assignable traits, from common/unit_leader."""
    global _FILES
    if _FILES is None:
        from .bridge import game_dir
        _FILES = {}
        for f in glob.glob(os.path.join(game_dir(), "common", "unit_leader", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\t(\w+) = \{", txt):
                body = txt[m.end():m.end() + 3000]
                end = re.search(r"(?m)^\t\}", body)
                body = body[:end.start()] if end else body
                if not re.search(r"type = assignable_trait", body):
                    continue
                mods = re.findall(r"(?m)^\t\t\t(\w+) = (-?[\d.]+)", body)
                _FILES[m.group(1)] = ", ".join("%s %s" % kv for kv in mods[:4])
                _META[m.group(1)] = _meta(body)
    return _FILES


_META = {}
_KINDS = {0: {"land", "all", "field_marshal", "corps_commander"},
          1: {"land", "all", "corps_commander"}, 2: {"navy", "all"}}


def _names(v):
    return set(re.findall(r"\w+", v.strip("{} "))) if v else set()


def _meta(body):
    """What the trait file already says about who can learn it: its type, the parents
    (any_parent / all_parents), the mutually exclusive traits and has_trait
    prerequisites. 23.10: asking the game for every trait of every leader held it
    71 times in a minute; this leaves only the ones that might pass."""
    def field(k):
        m = re.search(r"(?m)^\t\t%s\s*=\s*(\{[^}]*\}|\w+)" % k, body)
        return m.group(1) if m else None
    pre = re.search(r"(?ms)^\t\tprerequisites\s*=\s*\{(.*?)^\t\t\}", body)
    return {"type": _names(field("type")), "any": _names(field("any_parent")),
            "all": _names(field("all_parents")) | _names(field("parent")),
            "excl": _names(field("mutually_exclusive")),
            "needs": set(re.findall(r"has_trait\s*=\s*(\w+)", pre.group(1))) if pre else set()}


def maybe(trait, ld):
    """False when the files alone rule the trait out for this leader."""
    m = _META.get(trait)
    if not m:
        return True
    have = set(ld["traits"])
    kinds = _KINDS.get(ld["rank"])
    if m["type"] and kinds and not (m["type"] & kinds):
        return False
    if m["any"] and not (m["any"] & have):
        return False
    if not m["all"] <= have or not m["needs"] <= have:
        return False
    return not (m["excl"] & have)


def traits_db(g):
    """{trait name: CUnitLeaderTrait*}"""
    p = g.p
    if p.base not in _DB:
        tok = _tokens(g)
        vt = p.base + VT_TRAIT
        _DB[p.base] = {tok[p.i32(t + TR_TOKEN)]: t for t in p.scan_ptr(vt)
                       if p.u64(t) == vt and p.i32(t + TR_TOKEN) in tok}
    return _DB[p.base]


def leaders(g, ranks=(0, 1)):
    """[{key, name, rank, ptr, traits}] of our leaders of these ranks."""
    p = g.p
    G = B("game")
    tok = _tokens(g)
    out = []
    for c in g.all_characters():
        lp = p.u64(c + 0xC0)
        if not lp or p.i32(lp + LD_RANK) not in ranks:
            continue
        out.append({"key": G.item_name(p, c + G.CH_KEY), "name": G.item_name(p, c + G.CH_NAME),
                    "rank": p.i32(lp + LD_RANK), "ptr": lp,
                    "traits": [tok.get(p.i32(t + TR_TOKEN), "?")
                               for t in G._arr(p, lp, LD_TRAITS)]})
    return out


def _check(g, leader_ptr, trait_ptr, send):
    api, ao = B("api"), B("armyops")
    p = g.p
    api.allow_call(LEARN_CTOR)
    cmd = g.malloc(0x40)
    p.write(cmd, b"\0" * 0x40)
    g.call(LEARN_CTOR, [cmd, leader_ptr, trait_ptr])
    ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
    api.allow_call(ce)
    st = ao._mkstring(g)
    ok = bool(g.call(ce, [cmd, st]) & 0xFF)
    if ok and send:
        g.cmd.post(cmd)
    return ok, (ao._readstring(g, st) or "").replace("#~", "").strip()


WHY = {}     # the game's refusals of the last check: reason -> count (25.10, n3c: "no
            # general can learn" while Yamashita had two free slots and Fortress Buster open)


def learnable(g, ranks=(0, 1)):
    """{leader key: [traits the game lets him learn now]}"""
    db = traits_db(g)
    names = [n for n in assignable() if n in db]
    out = {}
    WHY.clear()
    for ld in leaders(g, ranks):
        cand = [n for n in names if n not in ld["traits"] and maybe(n, ld)]
        if not cand:
            continue
        # one short hold per leader, not one for all (23.10: 10.7 s frozen in one go)
        own = g._inj is None
        if own:
            g.attach()
        try:
            can = []
            for n in cand:
                ok, why = _check(g, ld["ptr"], db[n], False)
                if ok:
                    can.append(n)
                elif why:
                    WHY[why] = WHY.get(why, 0) + 1
        finally:
            if own:
                g.detach()
        if can:
            out[ld["key"]] = can
    return out


def learn(g, leader_key, trait):
    db = traits_db(g)
    if trait not in db or trait not in assignable():
        return {"error": "%s is not an assignable trait" % trait}
    want = str(leader_key).lower()
    ld = next((x for x in leaders(g, (0, 1, 2))
               if x["key"].lower() == want or (x["name"] or "").lower() == want), None)
    if ld is None:
        return {"error": "no leader %s of ours" % leader_key}
    own = g._inj is None
    if own:
        g.attach()
    try:
        ok, why = _check(g, ld["ptr"], db[trait], True)
    finally:
        if own:
            g.detach()
    if not ok:
        return {"error": why or "the game refused"}
    return {"learnt": trait, "leader": leader_key}


TRAIT_COST = 15.0
_LEARN = {}


def p_base(g):
    return g.p.base      # UNIT_LEADER_ASSIGN_TRAIT_COST in common/defines


def lines(g, ranks=(0, 1)):
    """Briefing lines: what each leader can learn now (only those who can). The check
    asks the game for every trait and leader (about 10 s), so only with command power
    enough for one trait."""
    cp = g.p.i64(g.player().ptr + B("game").CO_COMMAND_POWER) / 1e5
    if cp < TRAIT_COST:
        return ["TRAITS: a trait costs %d command power; we have %.0f" % (TRAIT_COST, cp)]
    # the check stops the game for every leader and trait (~10 s): once a month of game
    # time (23.10: the staff's and the admiralty's turns froze the game)
    day = g.date()["raw"] // 24
    hit = _LEARN.get((p_base(g), ranks))
    if hit and day - hit[0] < 30:
        can = hit[1]
    else:
        import os
        log = os.environ.get("HOI4_FREEZE_LOG")
        if log:
            with open(log, "a") as fh:
                fh.write("trait check: key %r day %d cached %r (module %s)\n" % (
                    (p_base(g), ranks), day, hit and hit[0], id(_LEARN)))
        can = learnable(g, ranks)
        _LEARN[(p_base(g), ranks)] = (day, can, dict(WHY))
        hit = _LEARN[(p_base(g), ranks)]
    if not can:
        why = sorted((hit[2] if len(hit) > 2 else {}).items(), key=lambda kv: -kv[1])[:3]
        return ["TRAITS: no general can learn a trait now (we have %.0f command power); "
                "the game says: %s" % (cp, "; ".join("%s (%d)" % kv for kv in why)
                                       or "they cost command power and need the parent "
                                          "trait first")]
    eff = assignable()
    out = ["TRAITS your commanders can learn now (learn_trait; costs command power):"]
    names = {x["key"]: x["name"] for x in leaders(g, ranks)}
    for k, ts in can.items():
        out.append("- %s (%s): %s" % (k, names.get(k), "; ".join("%s (%s)" % (t, eff.get(t) or "?") for t in ts)))
    return out
