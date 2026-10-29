"""Political power: national focus, laws, advisors and military staff, decisions.

What the country may take right now is asked of the game itself: each candidate command
is built and its CanExecute (vtable slot 10, a pure check) answers, all within one
attach. Nothing is posted until an order is given.

  CAddAdvisorCommand (0x50)  +0x24 handle character (type 73)  +0x2c u8 [cost]: pay it
                             +0x30 std::string [slot] (political_advisor, theorist,
                             army_chief, navy_chief, air_chief, high_command)
  CAdvisorTemplate           +0x10 CCharacterTemplate*  +0x20 std::string idea token
                             +0x70 std::string slot     +0xb8 i64 cost x1e5
  CCharacter                 +0x08 handle  +0x18 CCharacterTemplate*
  CReplaceIdeaCommand (0x40) +0x24 i32 tag  +0x28 CIdea* old  +0x30 CIdea* new  +0x38 u8 1
Laws are ideas, one per category: changing a law REPLACES the one in force (AddIdea's
CanExecute refuses a law while its category is full; checked 18.10).
"""
import copy
import struct

from industry import BASE_INIT, can_execute

CMD_ADVISOR = (0x4415A50, 0x50)
CMD_REPLACE_IDEA = (0x4415D70, 0x40)
AT_TEMPLATE, AT_IDEA, AT_SLOT, AT_COST = 0x10, 0x20, 0x70, 0xB8
CH_HANDLE, CH_TEMPLATE = 0x08, 0x18
SLOTS = ("political_advisor", "theorist", "army_chief", "navy_chief", "air_chief",
         "high_command")

LAWS = {
    "conscription": ["disarmed_nation", "volunteer_only", "limited_conscription",
                     "extensive_conscription", "service_by_requirement", "all_adults_serve",
                     "scraping_the_barrel"],
    "economy": ["isolation", "civilian_economy", "low_economic_mobilisation",
                "partial_economic_mobilisation", "war_economy", "tot_economic_mobilisation"],
    "trade": ["free_trade", "export_focus", "limited_exports", "closed_economy"],
}


def _attached(g, fn):
    own = g._inj is None
    if own: g.attach()
    try:
        return fn()
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ advisors
def advisors(g, tag=None):
    """Advisor and staff roles of the country's characters:
    [{character, name, idea, slot, cost, ptr}]"""
    from game import item_name
    p = g.p
    chars = {int(c["ptr"], 16): c for c in g.country_characters(tag)["characters"]}
    by_tmpl = {p.u64(c + CH_TEMPLATE): c for c in chars}
    out = []
    for a in g.oi.instances("CAdvisorTemplate"):
        c = by_tmpl.get(p.u64(a + AT_TEMPLATE))
        if not c:
            continue
        out.append({"character": chars[c]["key"], "name": chars[c]["name"],
                    "idea": p.stdstring(a + AT_IDEA), "slot": p.stdstring(a + AT_SLOT),
                    "cost": round(p.i64(a + AT_COST) / 1e5), "ptr": c})
    return out


def _advisor_cmd(g, char_ptr, slot):
    p = g.p
    vt, size = CMD_ADVISOR
    cmd = g.malloc(size)
    blob = bytearray(size)
    struct.pack_into("<Q", blob, 0, p.base + vt)
    for off, fmt, val in BASE_INIT:
        struct.pack_into(fmt, blob, off, val)
    blob[0x24:0x2C] = p.read(char_ptr + CH_HANDLE, 8)
    blob[0x2C] = 1
    s = slot.encode()
    if len(s) <= 15:
        buf = cmd + 0x40
        blob[0x40:0x40 + len(s)] = s
    else:
        buf = g.malloc(len(s) + 1)
        p.write(buf, s + b"\0")
    struct.pack_into("<QQ", blob, 0x30, buf, len(s))
    p.write(cmd, bytes(blob))
    return cmd


def _daily(fn):
    """Bound costly native availability checks to a game hour and cheap live changes.

    PP or war changes invalidate immediately, including within the same hour. Other
    external events expire within an hour; our own actions still call forget(). The
    epoch uses memory reads only, without attaching or invoking a native predicate.
    """
    import functools

    @functools.wraps(fn)
    def wrapped(g, tag=None):
        from combat import enemy_tags
        c = g._c(tag)
        try:
            epoch = (g.date_raw(), int(c.political_power), tuple(sorted(enemy_tags(g, tag))))
        except Exception:
            # Unknown freshness must not mean that a cached empty list is current.
            forget(g)
            return copy.deepcopy(fn(g, tag))
        key = (fn.__name__, c.tag_id, epoch)
        cache = g.__dict__.setdefault("_cabinet_cache", {})
        if key not in cache:
            for k in [k for k in cache if k[0] == fn.__name__]:
                del cache[k]
            cache[key] = fn(g, tag)
        return copy.deepcopy(cache[key])
    return wrapped


def forget(g):
    g.__dict__.pop("_cabinet_cache", None)


@_daily
def hireable(g, tag=None):
    """The advisor roles the game would let us hire now (CanExecute: a free slot AND the
    political power to pay; 24.10, at 114 PP the 150 PP political advisors drop out)."""
    roles = advisors(g, tag)

    def run():
        return [r for r in roles if can_execute(g, _advisor_cmd(g, r["ptr"], r["slot"]))]
    return _attached(g, run)


def hire(g, character_key, slot, tag=None, dry_run=False):
    rs = [r for r in advisors(g, tag) if r["character"] == character_key and r["slot"] == slot]
    if not rs:
        return {"ok": False, "error": "%s has no %s role" % (character_key, slot)}

    def run():
        cmd = _advisor_cmd(g, rs[0]["ptr"], slot)
        if not can_execute(g, cmd):
            return {"ok": False, "error": "the game does not allow hiring %s as %s now (already "
                    "hired, slot taken, not enough political power, or not available)"
                    % (character_key, slot)}
        if dry_run:
            return {"ok": True, "note": "allowed (dry run: not sent)"}
        g.cmd.post(cmd)
        return {"ok": True, "hired": character_key, "slot": slot, "cost": rs[0]["cost"]}
    try:
        return _attached(g, run)
    finally:
        forget(g)


# ------------------------------------------------------------- focus and ideas
def _cmd_ok(g, name, **fields):
    return can_execute(g, g.cmd.make(name, **fields))


def startable_focuses(g, tag=None):
    """Focuses the game would let us start now: [{key, name}]."""
    c = g._c(tag)
    try:
        cands = g.selectable_focuses(tag)
    except OSError:
        cands = []
    if not cands:
        # the game's selectable list (+0x1C10) read as garbage on 1.01.1936, in a
        # France game that had not yet started a focus, and stood EMPTY from spring 1939
        # to 30.09.1939 while 8 focuses could be started (France never reached the focus
        # that joins the Allies): ask the whole tree instead
        import game as G
        done = set(g.completed_focuses(tag))
        cands = []
        for f in g.focus_tree(tag):
            k = G.item_name(g.p, f + G.NF_KEY)
            if k and k not in done:
                cands.append({"key": k, "name": G.item_name(g.p, f + G.NF_NAME), "ptr": hex(f)})

    # 23.10: the whole tree held the game 1.0 s each time; once a game day (or after
    # a focus is set), in short holds
    key = (c.tag_id, g.date_raw() // 24, len(cands))
    cache = g.__dict__.setdefault("_focus_cache", {})
    if key in cache:
        return list(cache[key])
    out = []
    for i in range(0, len(cands), 40):
        part = cands[i:i + 40]
        out += _attached(g, lambda: [
            {"key": f["key"], "name": f["name"]} for f in part
            if _cmd_ok(g, "SetNationalFocus", tag=c.tag_id, focus_ptr=int(f["ptr"], 16))])
    cache.clear()
    cache[key] = out
    return list(out)


def _replace_cmd(g, tag_id, old, new):
    p = g.p
    vt, size = CMD_REPLACE_IDEA
    cmd = g.malloc(size)
    blob = bytearray(size)
    struct.pack_into("<Q", blob, 0, p.base + vt)
    for off, fmt, val in BASE_INIT:
        struct.pack_into(fmt, blob, off, val)
    struct.pack_into("<iQQB", blob, 0x24, tag_id, old, new, 1)
    p.write(cmd, bytes(blob))
    return cmd


def _active_ideas(g, tag=None):
    """{name: CIdea*} of the ideas in force."""
    import game as G
    p = g.p
    pol = p.u64(g._c(tag).ptr + G.CO_POLITICS)
    return {G.item_name(p, i + G.IDEA_NAME): i for i in G._arr(p, pol, G.PL_ACTIVE_IDEAS)}


@_daily
def laws(g, tag=None):
    """{category: {"now": law, "may_adopt": [laws the game allows now]}}. The law in
    force is the active idea the game lets a law of the category replace."""
    c = g._c(tag)
    active = _active_ideas(g, tag)
    ideas = {i["key"]: i["ptr"] for i in g.available_ideas(tag)}

    def run():
        out = {}
        for cat, names in LAWS.items():
            now = next((n for n in names if n in active), None)
            olds = [active[now]] if now else list(active.values())
            ok = []
            for n in names:
                if n not in ideas or n == now:
                    continue
                for o in olds:
                    if can_execute(g, _replace_cmd(g, c.tag_id, o, ideas[n])):
                        ok.append(n)
                        if not now:
                            now = next(k for k, v in active.items() if v == o)
                            olds = [o]
                        break
            out[cat] = {"now": now, "may_adopt": ok}
        return out
    return _attached(g, run)


def adopt_law(g, law, tag=None):
    """Replace the law in force in `law`'s category by `law`."""
    c = g._c(tag)
    cat = next((k for k, v in LAWS.items() if law in v), None)
    if cat is None:
        return {"ok": False, "error": "%s is not a law" % law}
    state = laws(g, tag)[cat]
    if law not in state["may_adopt"]:
        return {"ok": False, "error": "the game does not allow %s now (may adopt: %s)"
                % (law, ", ".join(state["may_adopt"]) or "nothing")}
    active = _active_ideas(g, tag)
    new = {i["key"]: i["ptr"] for i in g.available_ideas(tag)}[law]

    def run():
        cmd = _replace_cmd(g, c.tag_id, active[state["now"]], new)
        if not can_execute(g, cmd):
            return {"ok": False, "error": "refused by the game"}
        g.cmd.post(cmd)
        return {"ok": True, "law": law, "replaced": state["now"]}
    try:
        return _attached(g, run)
    finally:
        forget(g)


@_daily
def takeable_decisions(g, tag=None):
    c = g._c(tag)
    ds = g.decisions(tag, "visible")

    def run():
        return [d["key"] for d in ds
                if _cmd_ok(g, "SelectDecision", tag=c.tag_id, decision_ptr=d["ptr"])]
    return _attached(g, run)
