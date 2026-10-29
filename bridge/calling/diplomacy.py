"""Diplomacy: justifying war goals, declaring war, and related actions.

Structure:
  CDiplomaticAction (base)             +0x08 type token
                                       +0x10/+0x14  sender   (CCountryTag)
                                       +0x18/+0x1c  target   (CCountryTag)
                                       +0x28/+0x40  game scope (gamestate+0x460)
                                       +0x60 int, +0x64 int, +0x69 u8
  CGenerateWarGoalAction (0x90 bytes)  +0x70 CPdxArray<...> target states
                                       +0x88 CWarGoalType*
  CDiplomaticActionCommand (0x30 bytes) +0x28 CDiplomaticAction*  -> posted to the command channel
"""
import re
import struct

VT_GENERATE_WARGOAL = 0x4359718
FN_WARGOAL_CTOR  = 0xA7DDE0   # (this, CCountryTag* from, CCountryTag* to, CWarGoalType*, CPdxArray* states, scope*)
SZ_WARGOAL       = 0x90
FN_DIPCMD_CTOR   = 0xA66120   # (this, CDiplomaticAction*)
FN_DIPCMD_CANEXE = 0xA68750
SZ_DIPCMD        = 0x30
SCOPE_VT         = 0x445CD48  # two vptrs, at +0x10 and +0x60
G_GAMESTATE      = 0x459E9F0
GS_TURN          = 0x460      # the int that goes into the scope's +0x08
ALLOC            = 0x4505128

ACT_TOKEN   = 0x08
ACT_FROM    = 0x10
ACT_TO      = 0x18
WG_STATES   = 0x70
WG_TYPE     = 0x88


def wargoal_types(g):
    """War goal types in the game: name -> CWarGoalType*"""
    p = g.p
    out = {}
    for w in g.oi.instances("CWarGoalType"):
        try:
            n = p.cstr(p.u64(w + 0x10))
        except Exception:
            continue
        if n and n.isprintable():
            out[n] = w
    return out


def _scope(g):
    """The small scope struct the action constructor expects."""
    p = g.p
    gs = p.u64(p.base + G_GAMESTATE)
    s = g.malloc(0x18)
    p.write(s, struct.pack("<QiiQ", p.base + SCOPE_VT + 0x10,
                           p.i32(gs + GS_TURN), 0, p.base + SCOPE_VT + 0x60))
    return s


CO_TAG_INDEX = 0x08     # the CCountryTag inside CCountry (GER=1, POL=10 ...)
CO_CAPITAL_STATE = 0xFF0   # a STATE id (checked 18.10: BEL 6 Flanders, HOL 7 Holland)


def tag_index(g, tag):
    return g.p.i32(g.country(tag).ptr + CO_TAG_INDEX)


def _tagptr(g, tag):
    """A pointer in the target process to a CCountryTag (i32)."""
    idx = tag_index(g, tag)
    a = g.malloc(4)
    g.p.write(a, struct.pack("<i", idx))
    return a, idx


def justify_wargoal(g, target_tag, wargoal="annex_everything", states=(), source_tag=None):
    """Start justifying a war goal against a target country (a player command).

    WARNING: a hand-built CGenerateWarGoalAction object is INCOMPLETE.
    On the normal (slow) path this only starts a timer and is fine. With `instant_wargoal`
    cheat enabled, Execute tries to complete the justification IMMEDIATELY and walks
    enabled, the vtable+0x1C8 virtual walks an uninitialised field and segfaults.
    Call chain, from the crash dump:
        command queue -> CDiplomaticActionCommand::Execute (0xA438C0+0x2F5)
        -> 0xA44000+0xDEE -> CGenerateWarGoalAction vtable+0x1C8 (0xA57770+0x21D)
        -> 0xF4FFF0+0x468  SIGSEGV
    DO NOT USE THIS FUNCTION WHILE `instant_wargoal` IS ENABLED.
    """
    p = g.p
    wgs = wargoal_types(g)
    if wargoal not in wgs:
        return False, "bilinmeyen wargoal: %s (secenekler: %s)" % (wargoal, ", ".join(sorted(wgs)))
    own = g._inj is None
    if own: g.attach()
    try:
        src = source_tag or g.player().tag
        a_from, i_from = _tagptr(g, src)
        a_to, i_to = _tagptr(g, target_tag)
        sc = _scope(g)
        # State ids, int32 (they were packed as 8-byte words). Never empty: when the
        # justification completes the game takes the list's last state, and an empty
        # list crashed it eight days in (18.10, 1.19.3, SIGSEGV at 0xF4FFD8). With no
        # states named, the target's capital state goes in, as the game's own UI does
        # for the war goals that take no states.
        if not states:
            states = [p.i32(g.country(target_tag).ptr + CO_CAPITAL_STATE)]
        st = g.malloc(0x18)
        d = g.malloc(len(states) * 4)
        p.write(d, b"".join(struct.pack("<i", x) for x in states))
        p.write(st, struct.pack("<QiiQ", d, len(states), len(states), p.base + ALLOC))
        act = g.malloc(SZ_WARGOAL); p.write(act, b"\0" * SZ_WARGOAL)
        g.call(FN_WARGOAL_CTOR, [act, a_from, a_to, wgs[wargoal], st, sc])
        if p.u64(act) != p.base + VT_GENERATE_WARGOAL:
            return False, "could not build the action (wrong vptr)"
        if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
            return False, "sides were not written (%d/%d)" % (p.i32(act + ACT_FROM), p.i32(act + ACT_TO))
        if p.u64(act + WG_TYPE) != wgs[wargoal]:
            return False, "war goal type was not written"
        cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
        g.call(FN_DIPCMD_CTOR, [cmd, act])
        if p.u64(cmd + 0x28) != act:
            return False, "the command was not bound to the action"
        ok = g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "CanExecute false (conditions not met)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ================================================================== generic actions
def action_catalog():
    """All diplomatic action classes: {name: {vtable, ctor, size}}"""
    import diploscan
    return diploscan.build()


def build_action(g, action_class, target_tag, source_tag=None):
    """Build the action object and VERIFY both sides were written correctly.

    Returns (action_ptr, error). If the constructor does not match the
    (this, from*, to*, scope*) signature, an error is returned and nothing is posted.
    """
    p = g.p
    cat = action_catalog()
    if action_class not in cat:
        return None, "unknown action: %s" % action_class
    info = cat[action_class]
    src = source_tag or g.player().tag
    a_from, i_from = _tagptr(g, src)
    a_to, i_to = _tagptr(g, target_tag)
    sc = _scope(g)
    act = g.malloc(info["size"]); p.write(act, b"\0" * info["size"])
    import api
    api.allow_call(info["ctor"])     # diploscan verified these statically
    g.call(info["ctor"], [act, a_from, a_to, sc])
    if p.u64(act) != p.base + info["vtable"]:
        return None, "%s: the constructor did not write the vtable (signature may differ)" % action_class
    if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
        return None, ("%s: sides were not written (from=%d to=%d, expected %d/%d). "
                      "this class's constructor wants extra parameters"
                      % (action_class, p.i32(act + ACT_FROM), p.i32(act + ACT_TO), i_from, i_to))
    return act, None


def send_action(g, action_class, target_tag, source_tag=None, dry_run=False):
    """Send a diplomatic action through the player command channel.

    With dry_run=True it only builds the action and checks whether it would be
    allowed; nothing is sent.
    """
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        act, err = build_action(g, action_class, target_tag, source_tag)
        if err:
            return False, err
        cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
        g.call(FN_DIPCMD_CTOR, [cmd, act])
        if p.u64(cmd + 0x28) != act:
            return False, "the command was not bound to the action"
        ok = g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "not applicable (conditions are not met)"
        if dry_run:
            return True, "uygulanabilir (dry_run: gonderilmedi)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def declare_war(g, target_tag, dry_run=False):
    """Declare war. Requires a valid war goal."""
    return send_action(g, "CDeclareWarAction", target_tag, dry_run=dry_run)


def justifying(g):
    """War goals currently being justified.

    The duration belongs to THE GAME: we only pick the target (and states, where
    the type needs them). How many days it takes and what it costs in political
    power is the game's calculation. These values are only READ here."""
    p = g.p
    out = []
    idx2tag = {}
    for t in g.tags():
        try: idx2tag[tag_index(g, t)] = t
        except Exception: pass
    valid = set(idx2tag)
    for a in g.oi.instances("CTimedWargoalActivity"):
        try:
            tgt, src = p.i32(a + 0x10), p.i32(a + 0x14)
            if tgt not in valid or src not in valid:
                continue        # stale or invalid object
            prog, per = p.i64(a + 0x18), p.i64(a + 0x20)
            out.append({"from": idx2tag.get(src, src), "target": idx2tag.get(tgt, tgt),
                        "progress_left": round(prog / 1e5, 2),
                        "per_day": round(per / 1e5, 2),
                        "days_left": (round(prog / per, 1) if per else None),
                        "note": "the duration is set by the game and cannot be overridden"})
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ declaring war
# CDeclareWarAction(this, from*, to*, CWarGoal* wg, i32 flag, scope*)   size 0x78
FN_DECLAREWAR_CTOR = 0xA77580
SZ_DECLAREWAR      = 0x78
VT_DECLAREWAR      = 0x4359288
DW_WARGOAL         = 0x6C      # CWarGoal handle


def wargoals(g, tag=None):
    """Raw CWarGoal objects in the game. Which of them belongs to us
    `declare_war` picks one using the game's own CanExecute."""
    p = g.p
    out = []
    for w in g.oi.scan(["CWarGoal"])["CWarGoal"]:      # fresh: war goals come and go
        try:
            out.append({"ptr": hex(w), "raw": p.read(w, 0x20).hex()})
        except Exception:
            pass
    return out


def declare_war(g, target_tag, wargoal_ptr=None, flag=0, dry_run=False):
    """Declare war. Without wargoal_ptr, the country's first valid war goal is used."""
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        cands = ([wargoal_ptr] if wargoal_ptr is not None
                 else [int(w["ptr"], 16) for w in wargoals(g)] + [0])
        a_from, i_from = _tagptr(g, g.player().tag)
        a_to, i_to = _tagptr(g, target_tag)
        sc = _scope(g)
        for wg in cands:
            if not wg:
                continue
            act = g.malloc(SZ_DECLAREWAR); p.write(act, b"\0" * SZ_DECLAREWAR)
            g.call(FN_DECLAREWAR_CTOR, [act, a_from, a_to, wg, flag, sc])
            if p.u64(act) != p.base + VT_DECLAREWAR:
                continue
            if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
                continue
            cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
            g.call(FN_DIPCMD_CTOR, [cmd, act])
            if (g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF) == 0:
                continue
            if dry_run:
                return True, "uygulanabilir (dry_run: gonderilmedi)"
            g.cmd.post(cmd)
            return True, ""
        return False, ("no valid war goal against the target, justify one first "
                       "with hoi4_justify_wargoal (the game sets the duration)")
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ calling an ally
# CCallAllyAction(this, from*, to*, i32* enemy, scope*)   size 0x90 (18.10, 1.19.3)
# The "war" in the call is the enemy's CCountryTag: the game's own CJoinAllyAction for
# Slovakia held [10, 2, 33, ...] = POL, ENG, AST, ... (every country we were at war with).
FN_CALLALLY_CTOR = 0xA8B970
SZ_CALLALLY      = 0x90
VT_CALLALLY      = 0x435A280
CA_ENEMIES       = 0x70       # CPdxArray<CCountryTag>


def call_ally(g, ally_tag, enemy_tag, dry_run=False):
    """Call a faction member or subject into our war against enemy_tag (the call-to-arms
    button). The game decides whether the call is allowed and whether the ally comes."""
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        import api
        api.allow_call(FN_CALLALLY_CTOR)
        a_from, i_from = _tagptr(g, g.player().tag)
        a_to, i_to = _tagptr(g, ally_tag)
        a_en, i_en = _tagptr(g, enemy_tag)
        sc = _scope(g)
        act = g.malloc(SZ_CALLALLY); p.write(act, b"\0" * SZ_CALLALLY)
        g.call(FN_CALLALLY_CTOR, [act, a_from, a_to, a_en, sc])
        if p.u64(act) != p.base + VT_CALLALLY:
            return False, "could not build the action (wrong vptr)"
        if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
            return False, "sides were not written"
        arr = p.u64(act + CA_ENEMIES)
        if p.i32(act + CA_ENEMIES + 0xC) != 1 or p.i32(arr) != i_en:
            return False, "the enemy was not written"
        cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
        g.call(FN_DIPCMD_CTOR, [cmd, act])
        if p.u64(cmd + 0x28) != act:
            return False, "the command was not bound to the action"
        if not (g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF):
            return False, "the game does not allow this call (%s is not ours to call, or "\
                          "already in the war)" % ally_tag
        if dry_run:
            return True, "allowed (dry run: not sent)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# The list behind the game's alert "X is justifying against us" (the alert's tooltip
# builder, 0x2844E80, walks it on the player's CDiplomacyStatus): CCountryTag ids of the
# countries justifying a war goal against this one.
CO_DIPLOMACY = 0xF60
DS_JUSTIFIERS = 0xC8        # i32[] data; its size is the i32 at +0xD4


def justifications(g):
    """{target tag: [tags justifying a war goal against it]} over all countries. Pure read."""
    p = g.p
    tags = {c.tag_id: c.tag for c in g.countries()}
    out = {}
    for c in g.countries():
        if not c.ptr:
            continue
        try:
            d = p.u64(c.ptr + CO_DIPLOMACY)
            n = p.i32(d + DS_JUSTIFIERS + 0xC) if d else 0
            if not 0 < n < 256:
                continue
            ids = struct.unpack("<%di" % n, p.read(p.u64(d + DS_JUSTIFIERS), 4 * n))
        except OSError:
            continue
        out[c.tag] = [tags.get(i, str(i)) for i in ids]
    return out


# CGuaranteeAction (0x70): only a default constructor (0xA86170); the sides are written
# into it as the other actions carry them (+0x10/+0x14 from, +0x18/+0x1C to). Found 19.10.
FN_GUARANTEE_CTOR, SZ_GUARANTEE, VT_GUARANTEE = 0xA86170, 0x70, 0x4359BB8


GS_WORLD_THREAT = 0x6A0     # CWorldThreat*; +0x08 world tension x1e5 (found 19.10)
GUARANTEE_TENSION = 0.25    # a guarantee needs this much world tension


def world_tension(g):
    p = g.p
    t = p.u64(p.u64(p.base + G_GAMESTATE) + GS_WORLD_THREAT)
    return p.i64(t + 0x08) / 1e5 if t else None


# CGuaranteeRelation in the country's relation array (CDiplomacyStatus +0x20, the array the
# wars are read from): +0x0C guarantor, +0x10 guaranteed, CCountryTag ids. Read 20.10: FRA's
# four were (ENG, FRA) and (FRA, BEL/POL/...), the same object in both countries' arrays.
GR_FROM, GR_TO = 0x0C, 0x10
_GUAR_VT = None


def guarantees(g, tag=None):
    """{"gives": tags this country guarantees, "given_by": tags guaranteeing it}. Pure read.
    A guarantee does not bring the guarantor into a war by itself (20.10: Germany took
    Warsaw, FRA guaranteed POL and stayed at peace)."""
    global _GUAR_VT
    from game import _arr
    import combat
    p = g.p
    if _GUAR_VT is None:
        import rtti
        _GUAR_VT = rtti.build()["vtables"]["18CGuaranteeRelation"][0]
    vt = g.base + _GUAR_VT
    me = tag or g.player_tag()
    c = g.country(me)
    tags = {x.tag_id: x.tag for x in g.countries()}
    out = {"gives": [], "given_by": []}
    for r in _arr(p, p.u64(c.ptr + combat.CO_DIPLOMACY_STATUS), combat.DS_WAR_RELATIONS):
        if p.u64(r) != vt:
            continue
        a, b = tags.get(p.i32(r + GR_FROM)), tags.get(p.i32(r + GR_TO))
        if a == me and b:
            out["gives"].append(b)
        elif b == me and a:
            out["given_by"].append(a)
    return out


def guarantee(g, target_tag, dry_run=False):
    """Guarantee a country's independence: we join any war of aggression against it."""
    import api
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        api.allow_call(FN_GUARANTEE_CTOR)
        act = g.malloc(SZ_GUARANTEE); p.write(act, b"\0" * SZ_GUARANTEE)
        g.call(FN_GUARANTEE_CTOR, [act])
        i_from, i_to = tag_index(g, g.player().tag), tag_index(g, target_tag)
        p.write(act + ACT_FROM, struct.pack("<ii", i_from, i_from))
        p.write(act + ACT_TO, struct.pack("<ii", i_to, i_to))
        turn = p.i32(p.u64(p.base + G_GAMESTATE) + GS_TURN)
        p.write(act + 0x28, struct.pack("<i", turn))        # the game scope, as the
        p.write(act + 0x40, struct.pack("<i", turn))        # (from, to, scope) ctors set it
        cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
        g.call(FN_DIPCMD_CTOR, [cmd, act])
        if p.u64(cmd + 0x28) != act:
            return False, "the command was not bound to the action"
        ok = g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            wt = world_tension(g)
            if wt is not None and wt < GUARANTEE_TENSION:
                return False, ("a guarantee needs %d%% world tension; it is %.1f%% now (or we "
                               "have guaranteed it already)" % (GUARANTEE_TENSION * 100, wt * 100))
            return False, "the game does not allow it now (already guaranteed, or at war)"
        if dry_run:
            return True, "allowed (dry run: not sent)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ incoming requests
# A request another country sent us (call to arms, access, ...): CCountry +0xFD0 ->
# CIncomingDiplomaticActionStatus, +0x08 CPdxArray<CIncomingDiplomaticAction*>. Each one:
# +0x08 its handle, +0x18 the CDiplomaticAction inside (20.10: Poland's CCallAllyAction to
# France against Germany on 31.08.1939, which the harness never saw; a guarantor that is
# a player is asked, not taken in).
CO_INCOMING, IS_ACTIONS, IA_HANDLE, IA_ACTION = 0xFD0, 0x08, 0x08, 0x18
VT_INCOMING = 0x435E4F0
# CIncomingDiplomaticActionActingCommand(this, CCountryTag*, CIncomingDiplomaticAction*,
# answer, flag), 0x38 bytes. Execute (0xAC4A60) writes the answer into the action's +0x60,
# and the actions carry out their effect only on 2 (0xA844B4: `cmp [rdi+0x60], 2; jne
# return`); the game's own automatic answer is 2. 20.10: taking 1 for "accept" declined
# Poland's call to arms and France stayed at peace.
FN_ANSWER_CTOR, SZ_ANSWER = 0x1D028E0, 0x38
PR_POOL, PR_PRICE = 0xB8, (0x158, 0x160)   # CRequestEquipmentPurchaseAction (vt 0x4392000)
ACCEPT, DECLINE = 2, 1
_NAMES = None


def incoming(g, tag=None):
    """Requests waiting for our answer: [{id, kind, from, enemies}]. Pure read."""
    global _NAMES
    from game import _arr
    p = g.p
    st = p.u64(g._c(tag).ptr + CO_INCOMING)
    if not st or not p.i32(st + IS_ACTIONS + 0xC):
        return []
    if _NAMES is None:
        import rtti
        _NAMES = {a: n for n, v in rtti.build()["vtables"].items() for a in v}
    names = {g.base + a: n for a, n in _NAMES.items()}
    tags = {x.tag_id: x.tag for x in g.countries()}
    out = []
    for ia in _arr(p, st, IS_ACTIONS):
        if p.u64(ia) != g.base + VT_INCOMING:
            continue
        act = p.u64(ia + IA_ACTION)
        # '20CDockingRightsAction', 'N9NFactions23COfferJoinFactionActionE'
        m = re.search(r"(C[A-Za-z]+Action)", names.get(p.u64(act), ""))
        kind = m.group(1) if m else "?"
        rec = {"id": p.u64(ia + IA_HANDLE), "kind": kind, "from": tags.get(p.i32(act + ACT_FROM))}
        if kind == "CCallAllyAction":
            n = p.i32(act + CA_ENEMIES + 0xC)
            arr = p.u64(act + CA_ENEMIES)
            rec["enemies"] = [tags.get(p.i32(arr + 4 * i)) for i in range(n)] if 0 < n < 64 else []
        elif kind == "CRequestEquipmentPurchaseAction":
            # 26.10: the action holds a copy of the contract definition at +0x70: the
            # equipment asked for in its pool (+0xB8), the price at +0x158/+0x160
            import market
            rec["items"] = [(e, d, a) for _, e, d, a in market.pool_items(g, act + PR_POOL)]
            rec["price"] = max(p.i64(act + o) for o in PR_PRICE) / market.FIXED
        out.append(rec)
    return out


def answer_incoming(g, request_id, accept):
    """Accept or decline a request from incoming(): the game's own answer command."""
    from game import _arr
    import api
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        st = p.u64(g.player().ptr + CO_INCOMING)
        ia = next((x for x in _arr(p, st, IS_ACTIONS)
                   if p.u64(x) == g.base + VT_INCOMING and p.u64(x + IA_HANDLE) == request_id),
                  None)
        if ia is None:
            return False, "no such request (answered already, or expired)"
        api.allow_call(FN_ANSWER_CTOR)
        a_tag, _ = _tagptr(g, g.player().tag)
        cmd = g.malloc(SZ_ANSWER); p.write(cmd, b"\0" * SZ_ANSWER)
        g.call(FN_ANSWER_CTOR, [cmd, a_tag, ia, ACCEPT if accept else DECLINE, 0])
        if p.u64(cmd + 0x28) != request_id:
            return False, "the command was not bound to the request"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ joining a faction
# NFactions::CJoinFactionAction(this, CCountryTag* from, CCountryTag* to, scope*): our
# request to join the faction `to` leads (20.10; the Allies did not invite France in 1939).
FN_JOINFACTION_CTOR, SZ_JOINFACTION, VT_JOINFACTION = 0x9BD750, 0x80, 0x4353F58


def join_faction(g, leader_tag, dry_run=False):
    """Ask the leader of a faction to take us in. The game decides whether it may be
    asked now (world tension, ideology, wars) and whether the leader accepts."""
    import api
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        api.allow_call(FN_JOINFACTION_CTOR)
        a_from, i_from = _tagptr(g, g.player().tag)
        a_to, i_to = _tagptr(g, leader_tag)
        act = g.malloc(SZ_JOINFACTION); p.write(act, b"\0" * SZ_JOINFACTION)
        g.call(FN_JOINFACTION_CTOR, [act, a_from, a_to, _scope(g)])
        if p.u64(act) != p.base + VT_JOINFACTION:
            return False, "could not build the action (wrong vptr)"
        if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
            return False, "sides were not written"
        cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
        g.call(FN_DIPCMD_CTOR, [cmd, act])
        if not (g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF):
            return False, "the game does not allow asking %s now%s" % (leader_tag,
                                                                        why_not(g, act))
        if dry_run:
            return True, "allowed (dry run: not sent)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ simple actions
# 22.10: the actions with a (this) constructor of 0x70 bytes work as the guarantee does:
# write both sides and the turn, wrap them in the diplomatic command, ask CanExecute.
SIMPLE_ACTIONS = {
    "military_access": (0xA887A0, "CMilitaryAccessAction"),          # we ask for access
    "offer_military_access": (0xA8A360, "COfferMilitaryAccessAction"),
    "docking_rights": (0xA206E0, "CDockingRightsAction"),            # we ask to dock
    "offer_docking_rights": (0xA22550, "COfferDockingRightsAction"),
    "air_base_access": (0xA1CCD0, "CAirBaseAccessAction"),
    "offer_air_base_access": (0xA1E9D0, "COfferAirBaseAccessAction"),
    "send_attache": (0xA9E4B0, "CSendAttacheAction"),
    "improve_relations": (0xA9D510, "CImproveRelationAction"),
    "non_aggression_pact": (0xAA0B50, "CNonAggressionPactAction"),
    "embargo": (0xAB9D20, "CEmbargoAction"),
    "reduce_autonomy": (0xAB1480, "CReduceAutonomyAction"),
    "increase_autonomy": (0xAB1930, "CIncreaseAutonomyAction"),
}


def simple_action(g, kind, target_tag, dry_run=False):
    import api
    if kind not in SIMPLE_ACTIONS:
        return False, "unknown action %s" % kind
    ctor, cls = SIMPLE_ACTIONS[kind]
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        api.allow_call(ctor)
        act = g.malloc(0x70); p.write(act, b"\0" * 0x70)
        g.call(ctor, [act])
        if not p.u64(act):
            return False, "%s: the constructor wrote no vtable" % cls
        i_from, i_to = tag_index(g, g.player().tag), tag_index(g, target_tag)
        p.write(act + ACT_FROM, struct.pack("<ii", i_from, i_from))
        p.write(act + ACT_TO, struct.pack("<ii", i_to, i_to))
        turn = p.i32(p.u64(p.base + G_GAMESTATE) + GS_TURN)
        p.write(act + 0x28, struct.pack("<i", turn))
        p.write(act + 0x40, struct.pack("<i", turn))
        cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
        g.call(FN_DIPCMD_CTOR, [cmd, act])
        if p.u64(cmd + 0x28) != act:
            return False, "the command was not bound to the action"
        if not g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF:
            return False, "the game does not allow %s with %s now%s" % (
                kind.replace("_", " "), target_tag, why_not(g, act))
        if dry_run:
            return True, "allowed (dry run: not sent)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ the game's reasons
# CDiplomaticAction::IsSelectable is a virtual (offsets.VT_SLOTS) (this, std::string* tooltip, 0): the
# command's CanExecute only says "IsSelectable is false"; the tooltip has the rules
# (22.10: lend-lease "We need world tension at 80%", "SIA must be at war").
VT_IS_SELECTABLE = __import__("offsets").VT_SLOTS["ACTION_IS_SELECTABLE"][1]


def _clean(text):
    text = re.sub(r"\x12\w{3}", "", text)          # flag icons
    text = re.sub(r"\x11.", "", text)               # colour codes
    return "; ".join(x.strip(" -+") for x in text.splitlines() if x.strip())


def why_not(g, act):
    """': reason' from the action's own tooltip, or '' (attached, the action built)."""
    import api
    ao = _armyops()
    p = g.p
    f = p.u64(p.u64(act) + VT_IS_SELECTABLE) - p.base
    api.allow_call(f)
    s = ao._mkstring(g)
    g.call(f, [act, s, 0])
    t = _clean(ao._readstring(g, s) or "")
    return (": " + t) if t else ""


def _armyops():
    import importlib
    return importlib.import_module("armyops")


def _post_action(g, act, what, dry_run):
    import api
    p = g.p
    cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
    g.call(FN_DIPCMD_CTOR, [cmd, act])
    if p.u64(cmd + 0x28) != act:
        return False, "the command was not bound to the action"
    if not g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF:
        return False, "the game does not allow %s now%s" % (what, why_not(g, act))
    if dry_run:
        return True, "allowed (dry run: not sent)"
    g.cmd.post(cmd)
    return True, ""


def _tagged(g, ctor, size, target_tag):
    """An action built by its (this, from*, to*, scope*) constructor."""
    import api
    p = g.p
    api.allow_call(ctor)
    a_from, i_from = _tagptr(g, g.player().tag)
    a_to, i_to = _tagptr(g, target_tag)
    act = g.malloc(size); p.write(act, b"\0" * size)
    g.call(ctor, [act, a_from, a_to, _scope(g)])
    if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
        raise RuntimeError("the action's sides were not written")
    return act


# ------------------------------------------------------------------ inviting to our faction
# NFactions::COfferJoinFactionAction (vtable 0x43541A0, token offer_join_faction), 0x78
# bytes, ctor 0x9C4040 (this, from*, to*, scope*). Found 22.10; Japan 1936 has no faction,
# so it was refused ("offer_join_faction is not valid").
FN_OFFERFACTION_CTOR, SZ_OFFERFACTION = 0x9C4040, 0x78


def invite_to_faction(g, target_tag, dry_run=False):
    own = g._inj is None
    if own: g.attach()
    try:
        act = _tagged(g, FN_OFFERFACTION_CTOR, SZ_OFFERFACTION, target_tag)
        return _post_action(g, act, "inviting %s to our faction" % target_tag, dry_run)
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ lend-lease
# CLendLeaseAction (token lend_lease), 0x140 bytes, ctor 0xA26D50 (this, from*, to*,
# scope*). Its fields by the serializer: +0x70 fuel_daily, +0x78 fuel_percentage,
# +0x80 equipment, +0xC0 production_percentage, +0x100 once: three CEquipmentVariantPool.
# Pool add 0xCFB6E0 (pool, CEquipmentVariant*, whole units). Verified 22.10 on Japan 1936:
# built and checked; the game refused by its rules (world tension 80%, 70% to a puppet,
# and the receiver must be at war).
FN_LENDLEASE_CTOR, SZ_LENDLEASE = 0xA26D50, 0x140
LL_EQUIPMENT = 0x80
FN_POOL_ADD = 0xCFB6E0


def lend_lease(g, target_tag, items, dry_run=False):
    """items: [(equipment name prefix, units)] taken from our stockpile."""
    import api
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        g._load_equipment()
        ps = p.u64(g._c(None).ptr + 0x0F40)
        n = p.i32(ps + 0x220 + 0xC)
        base = p.u64(ps + 0x220)
        stock = []
        for i in range(n):
            vp, cnt = struct.unpack("<qq", p.read(base + 16 * i, 16))
            stock.append((g._eq_by_id.get(p.i32(vp + 0x14)) or "", vp, cnt // 100000))
        act = _tagged(g, FN_LENDLEASE_CTOR, SZ_LENDLEASE, target_tag)
        api.allow_call(FN_POOL_ADD)
        sent = []
        for name, units in items:
            have = [s for s in stock if s[0].startswith(name) and s[2] > 0]
            if not have:
                return False, "no %s in our stockpile" % name
            left = int(units)
            for nm, vp, cnt in sorted(have, key=lambda s: -s[2]):
                k = min(left, cnt)
                if k > 0:
                    g.call(FN_POOL_ADD, [act + LL_EQUIPMENT, vp, k])
                    sent.append("%d %s" % (k, nm))
                    left -= k
        ok, why = _post_action(g, act, "lend-lease to %s" % target_tag, dry_run)
        return ok, why or ", ".join(sent)
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ subjects' autonomy
# CSubjectRelation (in both sides' relation arrays): +0x0C overlord tag id, +0x10 subject
# tag id, +0x50 the autonomy state (+0x8 its name: autonomy_dominion, autonomy_colony,
# autonomy_puppet, autonomy_integrated_puppet...). CReduceAutonomyAction (0x70 bytes,
# ctor 0xAB1480) and CIncreaseAutonomyAction (ctor 0xAB1930): default ctors (this),
# one level a time, paid in political power (26.10, UK 1937: Raj colony -> puppet 50 PP).
SR_AUTONOMY = 0x50
FN_REDUCE_AUTONOMY, FN_INCREASE_AUTONOMY, SZ_AUTONOMY = 0xAB1480, 0xAB1930, 0x70


def subjects(g):
    """[(tag, autonomy state name)] of our subjects: pure reads."""
    import combat
    import rtti
    from game import _arr, item_name
    p = g.p
    vt = g.base + rtti.build()["vtables"]["16CSubjectRelation"][0]
    me = g.player()
    d = p.u64(me.ptr + combat.CO_DIPLOMACY_STATUS)
    ids = {}
    for t in g.tags():
        try:
            ids[g.country(t).tag_id] = t
        except Exception:
            continue
    out = []
    for x in _arr(p, d, combat.DS_WAR_RELATIONS):
        if p.u64(x) == vt and p.u16(x + 0x0C) == me.tag_id:
            t = ids.get(p.u16(x + 0x10))
            st = p.u64(x + SR_AUTONOMY)
            if t:
                out.append((t, item_name(p, st + 8) if st else None))
    return sorted(out)


def change_autonomy(g, target_tag, reduce=True, dry_run=False):
    """One level less (reduce) or more autonomy for a subject of ours. (ok, why)."""
    # default constructors (this): the sides and the turn are written by simple_action
    return simple_action(g, "reduce_autonomy" if reduce else "increase_autonomy",
                         target_tag, dry_run)
