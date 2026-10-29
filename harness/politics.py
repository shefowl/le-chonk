"""Political power as the politics minister sees and spends it: the national focus, laws,
advisors and military staff, decisions. What may be taken now is asked of the game
(bridge/calling/cabinet.py: each command's own CanExecute); no judgement here.
"""
from . import bridge as B
from .ops import OpError

RANKS = ["disarmed_nation", "volunteer_only", "limited_conscription",
         "extensive_conscription", "service_by_requirement", "all_adults_serve",
         "scraping_the_barrel"]


def picture(g):
    cab = B.module("cabinet")
    c = g.player()
    cur = g.current_focus()
    prog = g.focus_progress()
    return {
        "political_power": round(c.political_power),
        "focus": cur["name"] if cur else None,
        "focus_key": cur["key"] if cur else None,
        "focus_done": prog.get("raw_0x38"),
        "completed": prog.get("completed"),
        "startable": cab.startable_focuses(g) if not cur else [],
        "laws": cab.laws(g),
        "hireable": cab.hireable(g),
        "decisions": [k for k in takeable_decisions(g) if not decision_is_targeted(k)],
        "targeted_decisions": targeted_decisions(g),
        "visible_decisions": [d["key"] for d in g.decisions(None, "visible")],
        "ideas": g.politics()["active_ideas"],
        "removers": _removers(g),
    }


def _removers(g):
    from . import spirits
    G = B.module("game")
    tree = {G.item_name(g.p, f + G.NF_KEY) for f in g.focus_tree()}
    return spirits.removers(g.politics()["active_ideas"], g.completed_focuses(), tree,
                            g.player_tag())


def manpower(g):
    """(available manpower, divisions, the pool where the next conscription law is due)."""
    mp = int(g.player().manpower() or 0)
    if mp >= 400000:
        return mp, None, None
    n = len(g.divisions(limit=3000)["divisions"])
    # the user, 24.10: about 100-200k for a large army at war; a small country less
    return mp, n, max(20000, 2500 * n)


def alarm(g, pp_last=None):
    """Why the minister must act now, or None: no focus running, or political power
    grown by 100 since his last turn (07.02.1940: with every law at its limit he was
    woken every five days for 800 power with nothing to buy). Pure reads."""
    why = []
    if g.current_focus() is None:
        why.append("no national focus is running")
    mp, n, low = manpower(g)
    if low and mp < low:
        nxt = [x for x in B.module("cabinet").laws(g)["conscription"]["may_adopt"]
               if x in RANKS and RANKS.index(x) > RANKS.index(
                   B.module("cabinet").laws(g)["conscription"]["now"] or RANKS[0])]
        if nxt:
            # by tens of thousands, so a steady low pool does not wake him every day
            why.append("manpower is down to %dk for %d divisions; %s may be adopted"
                       % (mp // 10000 * 10, n, nxt[0]))
    pp = g.player().political_power
    if pp >= 150 and (pp_last is None or pp >= pp_last + 100):
        why.append("%d political power unspent" % pp)
    return "; ".join(why) or None


def set_focus(g, key):
    from . import planner
    shut = planner.against_history(key, g.player_tag()) if planner.HISTORICAL else []
    if shut:
        raise OpError("refused: this run keeps the historical course, and %s shuts %s of "
                      "the historical road for good" % (key, ", ".join(shut)))
    r = g.set_focus(key)
    if not r.get("ok"):
        raise OpError("the game did not start focus %s (not available now?)" % key)
    return r


def adopt_law(g, law):
    r = B.module("cabinet").adopt_law(g, law)
    if not r.get("ok"):
        raise OpError(r.get("error") or "the game did not adopt %s" % law)
    g._settle(0.5)
    if law not in g.politics()["active_ideas"]:
        raise OpError("%s was posted but is not active on readback" % law)
    r["verified"] = True
    return r


def hire(g, character, slot):
    r = B.module("cabinet").hire(g, character, slot)
    if not r.get("ok"):
        raise OpError(r.get("error") or "not hired")
    return r


def decision_is_targeted(key):
    import re
    return bool(re.search(r"(?m)^\t\t(?:targets|target_trigger|target_root_trigger|state_target)\s*=",
                          _blocks().get(key, "")))


def decision_targets(key):
    """Literal country candidates from the game's files, never strategic choices."""
    import re
    return re.findall(r"\b[A-Z]{3}\b", _part(_blocks().get(key, ""), "targets"))


def targeted_decisions(g):
    """Country decisions checked with the actual target, not SelectDecision."""
    from . import exile
    I = B.module("industry")
    live_tags = set(g.tags())
    candidates = [(d["key"], t) for d in g.decisions(None, "visible")
                  for t in decision_targets(d["key"]) if t in live_tags]
    if not candidates:
        return []
    def check():
        out = []
        for key, tag in candidates:
            cmd = exile._target_cmd(g, key, tag)
            if cmd and I.can_execute(g, cmd):
                out.append({"key": key, "target": tag})
        return out
    return B.module("cabinet")._attached(g, check)


def decision_options(g):
    ordinary = [k for k in takeable_decisions(g)
                if not decision_is_targeted(k)]
    return ordinary + ["%s target=%s" % (d["key"], d["target"])
                       for d in targeted_decisions(g)]


def _visible_decision(g, key):
    """The cached decision vector omits ordinary spirit remedies. Ask the script
    conditions too, so hidden/debug choices cannot leak through CanExecute alone."""
    import re
    body = _blocks().get(key)
    if not body or decision_is_targeted(key):
        return False
    conditions = " ".join(_part(body, field) for field in ("allowed", "visible"))
    result = g.console("eval_trigger %s = { %s }" % (g.player_tag(), conditions))
    return result.ok and bool(re.search(r"Result = yes\b", result.message or ""))


def _decision_cmd(g, key):
    d = next((d for d in g.decisions(None, "all") if d["key"] == key), None)
    if not d:
        return None
    cmd = g.cmd.make("SelectDecision", tag=g.player().tag_id, decision_ptr=d["ptr"])
    if g.p.i32(cmd + 0x24) != g.player().tag_id or g.p.u64(cmd + 0x28) != d["ptr"]:
        raise OpError("decision command fields did not match %s" % key)
    return cmd


def decision_available(g, key):
    I = B.module("industry")
    def check():
        if not _visible_decision(g, key):
            return False
        cmd = _decision_cmd(g, key)
        return bool(cmd and I.can_execute(g, cmd))
    return B.module("cabinet")._attached(g, check)


def takeable_decisions(g):
    """Regular candidates plus checked remedies for our active national spirits.
    A missing cached UI entry must not prevent inspecting a documented remedy."""
    from . import spirits
    ordinary = list(B.module("cabinet").takeable_decisions(g))
    active = tuple(sorted(g.politics()["active_ideas"]))
    cache_key = (g.date_raw() // 24, active)
    cached = getattr(g, "_spirit_decision_cache", (None, []))
    if cached[0] == cache_key:
        extra = cached[1]
    else:
        remedies = spirits.decisions()
        keys = {k for idea in active for k in remedies.get(idea, [])} - set(ordinary)
        extra = sorted(k for k in keys if decision_available(g, k))
        g._spirit_decision_cache = (cache_key, extra)
    return list(dict.fromkeys(ordinary + extra))


def decision_description(key):
    """Cost and effects from game files, without choosing an action for the model."""
    import re
    body = _blocks().get(key, "")
    cost = re.search(r"(?m)^\t\tcost\s*=\s*([^\s#]+)", body)
    effects = []
    for phase in ("complete_effect", "remove_effect"):
        effect = _part(body, phase)
        if effect:
            effects.append("%s: %s" % (phase, effect))
    return "cost %s; %s" % (cost.group(1) if cost else "0 PP", "; ".join(effects)[:900] or "no scripted effect")


def law_lines(laws):
    """Costs and modifiers, so the minister can compare laws rather than their names."""
    cats = idea_categories()
    lines = ["LAW COSTS AND BASE MODIFIERS (from game files; available choices checked "
             "by the game). Their production, consumer-goods and energy effects apply "
             "in peace too; compare benefits and penalties for preparation, not just "
             "the law's name:"]
    for category, row in laws.items():
        for key in dict.fromkeys([row.get("now")] + row.get("may_adopt", [])):
            if not key:
                continue
            data = cats.get(key)
            detail = ("%d PP to adopt; %s" % (data[1], data[2] or "no base modifiers")) if data else "effects unknown"
            lines.append("- %s %s %s: %s" %
                         (category, "IN FORCE" if key == row.get("now") else "MAY ADOPT", key, detail))
    return lines


def country_mechanics(g):
    """Read the same stability/paranoia values the player can inspect."""
    import re
    from .modui import _number
    c = g.player()
    lines = ["STABILITY: %.1f%%; WAR SUPPORT: %.1f%%" %
             (c.stability() * 100, c.war_support() * 100)]
    if g.player_tag() == "SOV":
        active = g.console("eval_trigger SOV = { has_country_flag = SOV_paranoia_system_active_flag }")
        match = re.search(r"Result = (yes|no)", active.message or "")
        value = g.console("get_var SOV.SOV_paranoia")
        n = _number(value.message) if value.ok else None
        lines.append("SOVIET PURGE MECHANIC: paranoia %s; system active %s. "
                     "Review this together with completed purge focuses and available decisions; "
                     "an inactive system is not a demand to restart purges." %
                     ("%.1f" % n if n is not None else "unknown",
                      match.group(1) if match else "unknown"))
    return lines


def decide(g, key, target=None):
    if target or decision_is_targeted(key):
        targets = decision_targets(key)
        target = (target or "").upper()
        if not target or target not in targets:
            raise OpError("decision %s needs a valid country target (%s)" %
                          (key, ", ".join(targets) or "no supported country target"))
        from . import exile
        I = B.module("industry")
        before = g.player().political_power
        running_before = sum(m["key"] == key for m in running_missions(g))
        def send():
            cmd = exile._target_cmd(g, key, target)
            if not cmd or not I.can_execute(g, cmd):
                raise OpError("the game does not allow %s for %s now" % (key, target))
            g.cmd.post(cmd)
        B.module("cabinet")._attached(g, send)
        g.__dict__.pop("_cabinet_cache", None)
        g._settle(0.5)
        # Posting is not confirmation. Read the power and timed-decision state back;
        # effects of a free instantaneous decision may need the next briefing.
        after = g.player().political_power
        running = sum(m["key"] == key for m in running_missions(g))
        return {"decision": key, "target": target, "posted": True,
                "political_power_before": before, "political_power_after": after,
                "effect_observed": after != before or running > running_before,
                "running": running}
    if not decision_available(g, key):
        raise OpError("decision %s cannot be taken now" % key)
    before = g.player().political_power
    running_before = sum(m["key"] == key for m in running_missions(g))
    def send():
        cmd = _decision_cmd(g, key)
        if not cmd or not B.module("industry").can_execute(g, cmd):
            raise OpError("decision %s cannot be taken now" % key)
        g.cmd.post(cmd)
    B.module("cabinet")._attached(g, send)
    g.__dict__.pop("_cabinet_cache", None)
    g.__dict__.pop("_spirit_decision_cache", None)
    g._settle(0.5)
    after = g.player().political_power
    running = sum(m["key"] == key for m in running_missions(g))
    return {"decision": key, "posted": True, "political_power_before": before,
            "political_power_after": after, "running": running,
            "effect_observed": after != before or running > running_before}


_DEC = None


def decision_terms(key):
    """What the game asks before a decision opens: its 'available' block from the game's
    files, squeezed to one line (23.10: Japan's way out of the naval treaty,
    MTG_abandon_treaty_japanese, waits for world tension above 20%)."""
    global _DEC
    if _DEC is None:
        import glob
        import os
        import re
        from .bridge import game_dir
        _DEC = {}
        for f in glob.glob(os.path.join(game_dir(), "common", "decisions", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\t(\w+) = \{", txt):
                body = txt[m.end():m.end() + 6000]
                end = re.search(r"(?m)^\t\}", body)
                body = body[:end.start()] if end else body
                a = re.search(r"(?m)^\t\tavailable = \{(.*?)^\t\t\}", body, re.S)
                if a:
                    t = re.sub(r"#[^\n]*", "", a.group(1))
                    t = re.sub(r"\s+", " ", t).strip()
                    cost = re.search(r"(?m)^\t\tcost = (\S+)", body)
                    _DEC[m.group(1)] = (t or "-") + (" (cost %s)" % cost.group(1) if cost else "")
    return _DEC.get(key)


_MISSION = None


def _blocks():
    """{decision key: its body} from the game's decision files."""
    global _MISSION
    if _MISSION is None:
        import glob
        import os
        import re
        from .bridge import game_dir
        from .spirits import _block_end
        _MISSION = {}
        for f in glob.glob(os.path.join(game_dir(), "common", "decisions", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\t([\w.\-]+)\s*=\s*\{", txt):
                _MISSION.setdefault(m.group(1), txt[m.end() - 1:_block_end(txt, m.end() - 1)])
    return _MISSION


def _part(body, name):
    import re
    from .spirits import _block_end
    m = re.search(r"(?m)^\t\t%s\s*=\s*\{" % name, body)
    if not m:
        return ""
    t = body[m.end():_block_end(body, m.end() - 1) - 1]
    t = re.sub(r"#[^\n]*", "", t)
    return re.sub(r"\s+", " ", t).strip()


def running_missions(g):
    """Our timed decisions and missions now running, from the game's memory: CDecisionStatus
    (country +0xF78) +0xA0 is an array of entries: +0x10 the decision, +0x18 days left,
    +0x1C state (0 running, 4 a cool-down after it). The save calls them
    active_timed_decision {decision days state} (23.10: JAP_kwantung_army_impatience,
    638 days left on 1936.04.05, counting down)."""
    import re
    from .bridge import module as B
    from .events import loc
    G = B("game")
    p = g.p
    ds = p.u64(g.player().ptr + G.CO_DECISIONS)
    out = []
    for e in G._arr(p, ds, 0xA0):
        key = g._decision_name(p.u64(e + 0x10))
        days, state = p.i32(e + 0x18), p.i32(e + 0x1C)
        if state != 0 or not key:
            continue
        body = _blocks().get(key, "")
        good = _part(body, "available")
        if "always = no" in good:
            good = ""
        ends = _part(body, "visible")
        timeout = _part(body, "timeout_effect")
        evs = sorted(set(re.findall(r"\bid = ([\w.]+)", timeout)))
        evs = ["%s \"%s\"" % (x, loc(x + ".t") or x) for x in evs[:3]]
        mission = "days_mission_timeout" in body
        out.append({"key": key, "name": loc(key) or key, "days_left": days,
                    "mission": mission, "succeeds_when": good[:220],
                    "holds_while": ends[:220],
                    "on_timeout": ("events " + ", ".join(evs)) if evs else timeout[:200]})
    # Chosen days_remove decisions use a separate CDecisionCooldown queue.
    # Save0xEDFA63 and dailyloop0xEE2C68: +70 array, entry+08 decision,+10 days.
    # This is not the +58 re-enable cooldown, nor the +A0 mission state format.
    for e in G._arr(p, ds, 0x70):
        key = g._decision_name(p.u64(e + 0x08))
        days = p.i32(e + 0x10)
        if not key or not 0 <= days <= 365000:
            continue
        body = _blocks().get(key, "")
        out.append({"key": key, "name": loc(key) or key, "days_left": days,
                    "mission": False, "succeeds_when": "",
                    "holds_while": _part(body, "visible")[:220],
                    "on_timeout": _part(body, "remove_effect")[:900]})
    return out


def mission_lines(g):
    ms = running_missions(g)
    if not ms:
        return []
    out = ["RUNNING MISSIONS AND TIMED DECISIONS (from the game; each ends at its date):"]
    for m in ms:
        out.append("- %s \"%s\": %d days left.%s%s Then: %s" % (
            m["key"], m["name"], m["days_left"],
            (" Done early when: %s." % m["succeeds_when"]) if m["succeeds_when"] else "",
            (" It lasts only while: %s." % m["holds_while"]) if m["holds_while"] else "",
            m["on_timeout"] or "nothing in its files"))
    return out


_EVENTS = None


def _event_blocks():
    """{event id: its block} from the game's event files."""
    global _EVENTS
    if _EVENTS is None:
        import glob
        import os
        import re
        from .bridge import game_dir
        from .spirits import _block_end
        _EVENTS = {}
        for f in glob.glob(os.path.join(game_dir(), "events", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\w+ = \{", txt):
                body = txt[m.end() - 1:_block_end(txt, m.end() - 1)]
                i = re.search(r"(?m)^\tid = ([\w.\-]+)", body)
                if i:
                    _EVENTS.setdefault(i.group(1), body)
    return _EVENTS


def war_targets(event_id, own):
    """The countries an event makes us declare war on (in a scope of our tag)."""
    import re
    body = _event_blocks().get(event_id, "")
    if not re.search(r"\b%s = \{" % own, body):
        return []
    out = []
    for t in re.findall(r"declare_war_on = \{\s*target = (\w+)", body):
        if t not in out and t != own:
            out.append(t)
    return out


def coming_wars(g):
    """[{mission, name, days_left, event, targets}]: wars the game itself will start for us
    when a running mission ends (23.10: the Kwantung Army's impatience fires china.1, war
    on CHI, SHX, SND and HBC at once; the staff prepared for HBC's 14 divisions alone)."""
    import re
    own = g.player_tag()
    out = []
    for m in running_missions(g):
        body = _blocks().get(m["key"], "")
        for e in sorted(set(re.findall(r"\bid = ([\w.]+)", _part(body, "timeout_effect")))):
            ts = war_targets(e, own)
            if ts:
                out.append({"mission": m["key"], "name": m["name"], "days_left": m["days_left"],
                            "event": e, "targets": ts})
    return out


def coming_targets(g):
    return {t for w in coming_wars(g) for t in w["targets"]}


def coming_war_lines(g):
    from .bridge import module as B
    from .events import loc
    G = B("game")
    p = g.p
    def divs(tag):
        try:
            return len(G._arr(p, g.country(tag).ptr, 0x290))
        except Exception:
            return None
    ours = divs(g.player_tag())
    out = []
    for w in coming_wars(g):
        n = {t: divs(t) for t in w["targets"]}
        n = {t: v for t, v in n.items() if v is not None}
        if not n:
            continue
        out.append("THE WAR THE GAME WILL START: when %s \"%s\" runs out (%d days left), event "
                   "%s \"%s\" brings war with %s: %d divisions together against our %d. "
                   "Prepare the theatres, the army and the factories for all of them, not "
                   "only for the neighbour on today's border." % (
                       w["mission"], w["name"], w["days_left"], w["event"],
                       loc(w["event"] + ".t") or w["event"],
                       ", ".join("%s (%d divisions)" % kv for kv in n.items()),
                       sum(n.values()), ours or 0))
    return out


_IDEA_CATS = None
CONCERN_CATS = ("industrial_concern",)


def idea_categories():
    """{idea key: (category, cost, first modifiers)} from common/ideas (cached)."""
    global _IDEA_CATS
    if _IDEA_CATS is None:
        import glob, os, re
        _IDEA_CATS = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "ideas", "*.txt")):
            depth, cat, key, buf = 0, None, None, []
            for raw in open(f, encoding="utf-8-sig", errors="replace"):
                ln = raw.split("#", 1)[0]
                m = re.match(r"\s*([A-Za-z0-9_\.]+)\s*=\s*\{", ln)
                if m and depth == 1:
                    cat = m.group(1)
                elif m and depth == 2:
                    key, buf = m.group(1), []
                elif key and depth >= 3:
                    buf.append(ln.strip())
                depth += ln.count("{") - ln.count("}")
                if key and depth <= 2:
                    txt = _strip_block(" ".join(buf), "ai_will_do")
                    c = re.search(r"\bcost\s*=\s*(\d+)", txt)
                    mods = re.findall(r"(?:modifier|research_bonus|equipment_bonus)\s*=\s*"
                                      r"\{([^{}]*)\}", txt)
                    # conditions the game checks and AddIdea does not (an empty slot has no
                    # replace command to ask)
                    cond = bool(re.search(r"\b(available|visible)\s*=\s*\{\s*[^}\s]", txt))
                    _IDEA_CATS[key] = (cat, int(c.group(1)) if c else 150,
                                       " ".join(" ".join(mods).split()), cond)
                    key = None
                if depth <= 1:
                    cat = None if depth < 1 else cat
    return _IDEA_CATS


def _strip_block(txt, name):
    """txt without the `name = { ... }` blocks (braces matched)."""
    import re
    while True:
        m = re.search(r"\b%s\s*=\s*\{" % name, txt)
        if not m:
            return txt
        i, d = m.end(), 1
        while i < len(txt) and d:
            d += {"{": 1, "}": -1}.get(txt[i], 0)
            i += 1
        txt = txt[:m.start()] + txt[i:]


def concerns(g):
    """Industrial concerns we may take: [{key, cost, effect}], and the ones in force."""
    cats = idea_categories()
    active = set(g.politics()["active_ideas"])
    have = [k for k in active if cats.get(k, (None,))[0] in CONCERN_CATS]
    own = g.player_tag()
    av = [i["key"] for i in g.available_ideas()]
    opts = [{"key": k, "cost": cats[k][1], "effect": cats[k][2][:160]} for k in av
            if cats.get(k, (None,))[0] in CONCERN_CATS and k not in active and not cats[k][3]]
    return {"in_force": have, "options": opts}


def hire_concern(g, key):
    c = concerns(g)
    if c["in_force"]:
        raise OpError("the industrial concern slot holds %s already" % ", ".join(c["in_force"]))
    opt = next((o for o in c["options"] if o["key"] == key), None)
    if opt is None:
        raise OpError("%s is not an industrial concern we may take (options: %s)"
                      % (key, ", ".join(o["key"] for o in c["options"])))
    pp = g.politics()["political_power"]
    if pp < opt["cost"]:
        raise OpError("it costs %d political power, we have %d" % (opt["cost"], pp))
    r = g.add_idea(key)
    if not r.get("ok"):
        raise OpError("the game did not take it")
    return {"hired": key, "cost": opt["cost"]}


_FOCUS_IDS = None


def focus_ids():
    """Every national focus id in the game's files (cached)."""
    global _FOCUS_IDS
    if _FOCUS_IDS is None:
        import glob, os, re
        _FOCUS_IDS = set()
        for f in glob.glob(os.path.join(B.game_dir(), "common", "national_focus", "*.txt")):
            for ln in open(f, encoding="utf-8-sig", errors="replace"):
                m = re.match(r"\s*id\s*=\s*([A-Za-z0-9_]+)", ln)
                if m:
                    _FOCUS_IDS.add(m.group(1))
    return _FOCUS_IDS


def wrong_focus_keys(text, own):
    """Focus-like keys of ours in `text` that are no focus, with the game's nearest id
    (23.10: the plan named JAP_purge_the_kodoha_faction; the game's is
    JAP_sea_purge_the_kodoha_faction, and the minister left the slot idle waiting for it)."""
    import difflib, re
    ids = focus_ids()
    mine = [i for i in ids if i.startswith(own + "_")]
    out = []
    for k in sorted(set(re.findall(r"\b%s_[a-z0-9_]+\b" % own, text or ""))):
        if k in ids:
            continue
        near = difflib.get_close_matches(k, mine, n=1, cutoff=0.8)
        if near:
            out.append((k, near[0]))
    return out


# ------------------------------------------------------------ idle power (24.10)
# The user, 24.10: the minister sat on 244 political power, wrote "no free advisor slot"
# (his own note from 1936.07, never a refusal of the game) while high command, the
# political advisors, the theorist and most organisation policies stood empty, and the
# alarm only fired from a flat 250.
RESERVE = 50                 # kept for events


def empty_slots(g):
    """What the power could fill now: {"advisors": {slot: [hire rows]} (the slots the game
    takes a hire into now), "concern": [options] or [], "policies": [organisations that
    work our lines and hold no policy]}."""
    cab = B.module("cabinet")
    by = {}
    for r in cab.hireable(g):
        by.setdefault(r["slot"], []).append(r)
    c = concerns(g)
    out = {"advisors": by, "concern": [] if c["in_force"] else c["options"], "policies": []}
    try:
        from . import mio
        work = {l["org"] for l in mio.lines(g) if l["org"]}
        out["policies"] = [mio.short(o["key"]) for o in mio.organisations(g)
                           if o["key"] in work and not o["policy"] and o["policies"]]
    except Exception:
        pass
    return out


def cheapest_fill(e):
    """The lowest price that fills one of empty_slots' slots, or None."""
    from .mio import POLICY_COST
    costs = [r["cost"] for v in e["advisors"].values() for r in v]
    costs += [o["cost"] for o in e["concern"]]
    if e["policies"]:
        costs.append(POLICY_COST)
    return min(costs) if costs else None


def plan_reserve(plan_text):
    """The power THE PLAN names for coming buys in its politics part: the dearest one (the
    road runs one focus at a time), plus RESERVE. "PP 270:" is the plan's count of what we
    hold, not a price."""
    import re
    t = plan_text or ""
    i = t.find("POLITICS:")
    if i >= 0:
        t = t[i:].split("\n", 1)[0]
    prices = [int(m.group(1)) for m in re.finditer(r"\bPP (\d+)(?!\d*\s*:)", t)]
    prices += [int(m.group(1)) for m in re.finditer(r"\b(\d+) (?:PP|political power)\b", t)]
    prices = [p for p in prices if p <= 400]
    return RESERVE + (max(prices) if prices else 0)
