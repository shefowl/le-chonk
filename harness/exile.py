"""Governments in exile we host (La Resistance; 26.10 on 1.19.3: Poland fell to Germany
and its government came to London).

A country's diplomatic block (CCountry+0xF60; getter 0xD57310): +0x1A8 the host's tag
index, +0x1B0 legitimacy as int64 fixed x1e5 (0..100; setter 0xA53510). The host's block
lists the exiles it hosts at +0x190 (CPdxArray of tag indices). A division template keeps
the country it belongs to at +0x1EC and +0x1F0; an exile template is ours with the
exile's tag there, as the division designer's tooltip tells (0x1FCF930).

What the host gets (common/defines NCountry GIE_*): up to +5 civilian and +5 military
factories with legitimacy; exile divisions from the exile's veteran manpower at 25+
legitimacy, exile air wings at 50+; an exile army leader at 30, 60 and 90; exile
divisions +10% attack and defence against the occupier and +10% more on the exile's
cores; the occupied land resists harder the higher the legitimacy (target x2..x20).
"""
from . import bridge as B

DIPLO, HOST, LEGIT, HOSTED = 0xF60, 0x1F0, 0x1F8, 0x190   # 1.19.3: host 0x1A8, legit 0x1B0
FIXED = 1e5
TROOPS_AT, AIR_AT, LEADERS_AT, FACTORIES_MAX = 25, 50, (30, 60, 90), 5


def hosted(g):
    """[(tag, legitimacy)] of the governments in exile we host (pure reads)."""
    p = g.p
    D = B.module("diplomacy")
    blk = p.u64(g.player().ptr + DIPLO)
    if not blk:
        return []
    arr, n = p.u64(blk + HOSTED), p.i32(blk + HOSTED + 0xC)
    if not arr or not 0 < n < 50:
        return []
    idx = {}
    for t in g.tags():
        try:
            idx[D.tag_index(g, t)] = t
        except Exception:
            continue
    out = []
    for i in range(n):
        tag = idx.get(p.i32(arr + 4 * i))
        if not tag:
            continue
        b = p.u64(g._c(tag).ptr + DIPLO)
        out.append((tag, p.i64(b + LEGIT) / FIXED if b else 0.0))
    return out


def lines(g):
    rows = hosted(g)
    if not rows:
        return []
    out = []
    for tag, leg in rows:
        opened = [x for x, on in (
            ("exile divisions (from %s's veteran manpower, not ours)" % tag, leg >= TROOPS_AT),
            ("exile air wings", leg >= AIR_AT)) if on]
        nxt = sorted([(x, "exile divisions") for x in (TROOPS_AT,) if x > leg]
                     + [(x, "exile air wings") for x in (AIR_AT,) if x > leg]
                     + [(x, "an exile army leader") for x in LEADERS_AT if x > leg])
        out.append(
            "GOVERNMENT IN EXILE WE HOST: %s, legitimacy %.1f of 100. We get up to +%d civilian "
            "and +%d military factories with its legitimacy. %s. Exile divisions fight +10%% "
            "against the occupier and +10%% more on %s's own land; the occupied land resists "
            "its occupier harder as legitimacy grows, and legitimacy grows when its units fight "
            "in our wars.%s" % (
                tag, leg, FACTORIES_MAX, FACTORIES_MAX,
                ("Open now: " + "; ".join(opened)) if opened else
                "Exile divisions open at legitimacy %d" % TROOPS_AT,
                tag, (" Next: %s at legitimacy %d." % (nxt[0][1], nxt[0][0])) if nxt else ""))
    have = templates_of(g)
    if any(leg >= TROOPS_AT for _, leg in rows):
        out.append("EXILE TEMPLATES (train them; manpower from the exile, equipment ours): %s. "
                   "Army staff: exile_template copies one of our templates for an exile."
                   % ("; ".join("%s (%s)" % kv for kv in sorted(have.items())) or "none yet"))
    return out


T_EXILE = 0x1F4          # CDivisionTemplate: the exile whose manpower it draws (0: ours)
D_EXILE = T_EXILE - 0x18  # the same in CDivisionTemplateData


def templates_of(g):
    """{our template name: exile tag} of our exile templates (pure reads)."""
    D = B.module("diplomacy")
    idx = {}
    for t in g.tags():
        try:
            idx[D.tag_index(g, t)] = t
        except Exception:
            continue
    out = {}
    for t in g.division_templates():
        e = g.p.i32(int(t["ptr"], 16) + T_EXILE)
        if e and e in idx:
            out[t["name"]] = idx[e]
    return out


def make_template(g, base, tag, new_name, dry_run=False):
    """A copy of our template `base` that draws the manpower of the exile `tag` we host:
    what the division designer makes from a hosted exile's template (26.10: CCreate-
    DivisionTemplateCommand with the template's +0x1F4 set to the exile's tag index;
    the copy costs no experience)."""
    import struct
    T = B.module("templates")
    D = B.module("diplomacy")
    tag = tag.upper()
    rows = dict(hosted(g))
    if tag not in rows:
        return {"ok": False, "error": "we host no government in exile of %s (we host: %s)" % (
            tag, ", ".join(rows) or "none")}
    if rows[tag] < TROOPS_AT:
        return {"ok": False, "error": "%s's legitimacy is %.1f; exile divisions need %d" % (
            tag, rows[tag], TROOPS_AT)}
    src = T._template(g, base)
    if src is None:
        return {"ok": False, "error": "no template of ours called %r" % base}
    if T._template(g, new_name) is not None:
        return {"ok": False, "error": "a template called %r exists" % new_name}
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        new = g.malloc(T.DATA_SIZE)
        p.write(new, p.read(src + T.T_DATA, T.DATA_SIZE))
        p.write(new + D_EXILE, struct.pack("<i", D.tag_index(g, tag)))
        b = new_name.encode("utf-8")
        buf = g.malloc(len(b) + 1)
        p.write(buf, b + b"\0")
        p.write(new + T.D_NAME, struct.pack("<QQ", buf, len(b)))
        r = T._send(g, T.NEW_CTOR, {"old": src + T.T_DATA, "new": new}, dry_run)
    finally:
        if own:
            g.detach()
    if r.get("ok"):
        r["note"] = ("train it like any template: its divisions take %s's manpower, our "
                     "equipment" % tag)
    return r


# The decisions a host takes for an exile it hosts (common/decisions/_exiled_governments_
# decisions.txt): each targets the exile. 26.10: CSelectTargetedDecisionCommand with
# {exile's tag index, 0} passed CanExecute for 4 of 12 on POL, with GER or -1 for none.
DECISIONS = {
    "grant_parliamentary_audience": (0, "120 days: the exile's legitimacy +0.05 a day "
                                        "(about +6); our political power costs +15%"),
    "public_support_for_from": (0, "120 days: the exile's legitimacy +0.05 a day (about +6); "
                                   "our political power costs +25%"),
    "lobby_for_parliamentary_support": (0, "120 days: OUR legitimacy +0.05 a day (useful only "
                                           "when we are the exile); political power costs +25%"),
    "request_reinstatement": (10, "asks the exile to take its country back (once its land "
                                  "is free)"),
    "exile_extraction_campaign": (0, "25 command power; 60 days: the exile's manpower for "
                                     "exile divisions +50%"),
    "exile_recruitment_campaign": (0, "more manpower for the exile's divisions"),
    "purge_infiltrators": (0, "30 days, then +50 political power for us"),
    "expatriate_donations": (25, "360 days: the exile donates a civilian and a military "
                                 "factory (needs its legitimacy above 60)"),
    "request_control_of_navy": (25, "once: the exile's navy becomes ours; its legitimacy +10"),
    "weapons_for_the_resistance": (50, "120 days: our equipment arms the resistance in the "
                                       "exile's occupied land"),
    "unity_parade": (50, "45 days: our war support +1% a week; the exile's legitimacy -0.5 a "
                         "day; our political power costs +15%"),
    "joint_training_exercise": (0, "25 command power: army experience +25, air +50, navy +50 "
                                   "for us and for the exile"),
}


def _target_cmd(g, key, tag):
    D = B.module("diplomacy")
    ptr = next((d["ptr"] for w in ("visible", "all") for d in g.decisions(None, w)
                if d["key"] == key), None)
    if not ptr:
        return None
    return g.cmd.make("SelectTargetedDecision", tag=g._c(None).tag_id,
                      target_tag=D.tag_index(g, tag), target_state=0, decision_ptr=ptr)


def takeable(g):
    """[(decision, exile tag)] the game lets us take now (it asks the game: attaches)."""
    rows = hosted(g)
    if not rows:
        return []
    key = ("exile_takeable", g.date_raw() // 24)
    cache = g.__dict__.setdefault("_cabinet_cache", {})
    if key in cache:
        return list(cache[key])
    I = B.module("industry")
    pp = g._c(None).political_power
    out = []
    own = g._inj is None
    if own:
        g.attach()
    try:
        for tag, _ in rows:
            for k, (cost, _) in DECISIONS.items():
                cmd = _target_cmd(g, k, tag)
                if cmd and cost <= pp and I.can_execute(g, cmd):
                    out.append((k, tag))
    finally:
        if own:
            g.detach()
    for k in [k for k in cache if k and k[0] == "exile_takeable"]:
        del cache[k]
    cache[key] = out
    return list(out)


def decision_lines(g):
    if not hosted(g):
        return []
    can = takeable(g)
    return ["EXILE DECISIONS YOU MAY TAKE (take_decision with target = the exile's tag): " + (
        "; ".join("%s for %s (%d PP: %s)" % (k, t, DECISIONS[k][0], DECISIONS[k][1])
                  for k, t in can) or "none now")
            + ". Others open with the exile's legitimacy, our political power or the war: "
            + "; ".join("%s (%d PP: %s)" % (k, c, e) for k, (c, e) in DECISIONS.items()
                        if k not in {x for x, _ in can} and k != "lobby_for_parliamentary_support")]


def decide(g, key, tag):
    tag = tag.upper()
    if key not in DECISIONS:
        return {"ok": False, "error": "%s is no exile decision; take it without a target" % key}
    if (key, tag) not in takeable(g):
        return {"ok": False, "error": "the game does not allow %s for %s now" % (key, tag)}
    own = g._inj is None
    if own:
        g.attach()
    try:
        g.cmd.post(_target_cmd(g, key, tag))
    finally:
        if own:
            g.detach()
    g.__dict__.pop("_cabinet_cache", None)
    return {"ok": True, "decision": key, "target": tag, "effect": DECISIONS[key][1]}
