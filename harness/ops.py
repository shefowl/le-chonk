"""Mechanical operations the models can order. No military judgement lives here.

Every function runs on the gateway thread (first argument: the bridge Game), turns the
model's terms (armies, army groups, sectors, states) into the game's (orders groups, orders,
provinces), posts through the bridge, and then READS BACK what actually changed. A
command the game accepted but did not apply is reported as a failure, never as success.
"""
import collections
import math
import time

from . import bridge as B

SETTLE = 0.5            # commands apply on the game's next frame, even when paused


def still(fn):
    """Run an order that reads the game's fronts and then draws on them with the game
    paused, and resume it after. The fronts are rebuilt as the line moves: four times
    (17-18.10) the game crashed (SIGSEGV at 0x1A24D08) right after a sector cut drawn
    while the game ran and the front moved (Ardennes, Holland), from section indices and
    front pointers read a few seconds, and game days, before the commands executed."""
    import functools

    @functools.wraps(fn)
    def run(g, *a, **kw):
        session = B.module("session")
        if session.is_paused(g):
            return fn(g, *a, **kw)
        set_running(g, False)
        try:
            return fn(g, *a, **kw)
        finally:
            set_running(g, True)
    return run
OG_HANDLE, OG_DIVS, OG_ORDERS, OG_LEADER, OG_NAME = 0x08, 0x50, 0x98, 0x88, 0x160
OI_ID, OI_TYPE, OI_PROVS, OI_CHILDREN, OI_EXEC = 0x244, 0x30, 0x88, 0x1F8, 0x248
CARMY_SUB, CARMY_PROV, PROV_ID = 0xB8, 0x1F0, 0xA4
A_STR, A_ORG = 0x420, 0x428
OI_FRONT, OI_SECTION, OI_FROM, OI_TO = 0x25C, 0x260, 0x268, 0x270   # part of the section,
FULL = 100000                                                      # in 1/FULL of its length


class OpError(Exception):
    pass


def _armyops():
    return B.module("armyops")


# ------------------------------------------------------------------ lookups
OG_IS_GROUP, OG_PARENT, OG_MEMBERS = 0x39, 0x1B8, 0x228


def _caps():
    """Command limits from the game's defines (the player's own rules)."""
    import re
    txt = open(B.game_dir() + "/common/defines/00_defines.lua", encoding="utf-8").read()
    def val(name, default):
        m = re.search(r"\b%s\s*=\s*(-?\d+)" % name, txt)
        return int(m.group(1)) if m else default
    return {"army_divisions": val("CORPS_COMMANDER_DIVISIONS_CAP", 24),
            "field_marshal_armies": val("FIELD_MARSHAL_ARMIES_CAP", 5),
            "promotion_command_power": val("PROMOTE_LEADER_CP_COST", 40)}


CAPS = None


def caps():
    global CAPS
    if CAPS is None:
        CAPS = _caps()
    return CAPS


def _hid(g, og):
    return g.p.i32(og + OG_HANDLE + 4)


def armies(g):
    """Our ARMIES (orders groups that hold divisions) by handle id."""
    return {_hid(g, og): og for og in _armyops().orders_groups(g)}


def groups(g):
    """Our ARMY GROUPS by handle id (the parents of our armies)."""
    out = {}
    for og in armies(g).values():
        ag = g.p.u64(og + OG_PARENT)
        if ag:
            out[_hid(g, ag)] = ag
    return out


def members(g, ag):
    return B.arr(g.p, ag, OG_MEMBERS)


def is_group(g, og):
    return bool(g.p.u8(og + OG_IS_GROUP))


# theatres the player keeps (roster.json): their armies are not ours to order (23.10)
PLAYER_THEATRES = set()


def player_theatre(g, og):
    """The player's theatre this army or group stands in, or None."""
    if not PLAYER_THEATRES:
        return None
    ths = theatres(g)
    for tid in PLAYER_THEATRES:
        tg = ths.get(tid)
        if not tg:
            continue
        for x in B.arr(g.p, tg, TG_MEMBERS):
            if x == og or (is_group(g, x) and og in members(g, x)):
                return tid
    return None


def _guard(g, og, og_id):
    tid = player_theatre(g, og)
    if tid is not None:
        raise OpError("army %s is in the player's theatre %r: the player commands it, not us"
                      % (og_id, theatre_name(g, theatres(g)[tid])))
    return og


def army(g, og_id):
    """An army or an army group by its id: both hold orders the same way."""
    og = armies(g).get(og_id) or groups(g).get(og_id)
    if not og:
        raise OpError("army or army group %s does not exist (any more)" % og_id)
    return _guard(g, og, og_id)


def division_bases(g, og):
    if is_group(g, og):
        return [d for m in members(g, og) for d in division_bases(g, m)]
    return [e - CARMY_SUB for e in B.arr(g.p, og, OG_DIVS)]


def leader_name(g, og):
    ld = g.p.u64(og + OG_LEADER)
    return g.p.cstr(g.p.u64(ld + 0x40)) if ld else None


# ------------------------------------------------------------------ theatres
# A field marshal commands a THEATRE, a stretch of the map with one military sense (the
# north-east against Germany and the Low Countries, the Alps, North Africa), not an army
# group: one theatre may hold several army groups (the user, 19.10). The game has them:
# CTheatre (country +0x168, one per country) lists the theatres the player sees, each a
# CTheaterGroup: +0x08 handle, +0x28 name, +0x48 its army groups and armies (a group's
# armies listed beside it).
CO_THEATRE, TH_GROUPS = 0x168, 0xC8
TG_NAME, TG_MEMBERS = 0x28, 0x48
# CAssignToTheaterGroupCommand(this, CPdxArray<COrdersGroup*>*, CTheaterGroup* or 0, name):
# with no theatre the game opens a new one under that name (19.10)
TG_ASSIGN_CTOR, TG_ASSIGN_SIZE = 0x24FA6D0, 0x68


def theatres(g):
    """{theatre id: CTheaterGroup*} of ours."""
    p, out = g.p, {}
    for c in B.arr(p, g.player().ptr, CO_THEATRE):
        for tg in B.arr(p, c, TH_GROUPS):
            out[p.i32(tg + 0x0C)] = tg
    return out


def theatre_name(g, tg):
    p = g.p
    n = p.u64(tg + TG_NAME + 8)
    return p.read(p.u64(tg + TG_NAME), n).decode("utf-8", "replace") if 0 < n < 200 else ""


def theatre_members(g, tid):
    """Army groups and armies of a theatre (orders groups), the groups first."""
    tg = theatres(g).get(tid)
    if not tg:
        raise OpError("theatre %s does not exist (any more)" % tid)
    out = []
    for og in B.arr(g.p, tg, TG_MEMBERS):
        for x in [og] + (members(g, og) if is_group(g, og) else []):
            if x not in out:
                out.append(x)
    return sorted(out, key=lambda x: not is_group(g, x))


def theatre_armies(g, tid):
    return [og for og in theatre_members(g, tid) if not is_group(g, og)]


def theatre_of(g, og):
    for tid, tg in theatres(g).items():
        if og in B.arr(g.p, tg, TG_MEMBERS):
            return tid
    return None


def theatre_listing(g):
    """{theatre id: {name, army_groups, armies}} as the game holds them."""
    out = {}
    for tid, tg in theatres(g).items():
        ms = theatre_members(g, tid)
        out[tid] = {"name": theatre_name(g, tg),
                    "army_groups": [_hid(g, x) for x in ms if is_group(g, x)],
                    "armies": [_hid(g, x) for x in ms if not is_group(g, x)]}
    return out


def _std_string(g, text):
    import struct
    b = text.encode("utf-8")
    s = g.malloc(0x20)
    if len(b) <= 15:
        g.p.write(s, struct.pack("<QQ", s + 0x10, len(b)) + b + b"\0" * (16 - len(b)))
    else:
        buf = g.malloc(len(b) + 1)
        g.p.write(buf, b + b"\0")
        g.p.write(s, struct.pack("<QQQQ", buf, len(b), len(b), 0))
    return s


def _assign_theatre(g, ogs, tg, name=""):
    api = B.module("api")
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        arr = _armyops()._mkarray(g, list(ogs))
        cmd = g.malloc(TG_ASSIGN_SIZE)
        p.write(cmd, b"\0" * TG_ASSIGN_SIZE)
        api.allow_call(TG_ASSIGN_CTOR)
        g.call(TG_ASSIGN_CTOR, [cmd, arr, tg or 0, _std_string(g, name)])
        if p.i32(cmd + 0x3C) != len(ogs):
            raise OpError("the theatre command took %d of %d commands"
                          % (p.i32(cmd + 0x3C), len(ogs)))
        g.cmd.post(cmd)
    finally:
        if own:
            g.detach()
    time.sleep(SETTLE)


def _ogs(g, ids):
    out = []
    for i in ids:
        og = armies(g).get(int(i)) or groups(g).get(int(i))
        if not og:
            raise OpError("army or army group %s does not exist" % i)
        out.append(_guard(g, og, i))
    return out


def new_theatre(g, name, og_ids):
    """Open a theatre in the game named `name` with these army groups and armies (an army
    leaves its army group). Returns its id."""
    from . import roster
    name = roster.name_for_new("theatres", name)
    if not og_ids:
        raise OpError("a theatre opens with at least one army group or army in it")
    before = set(theatres(g))
    ogs = _ogs(g, og_ids)
    _assign_theatre(g, ogs, None, name)
    new = set(theatres(g)) - before
    if not new:
        raise OpError("the game opened no theatre")
    return new.pop()


def move_to_theatre(g, og_ids, tid):
    """Army groups and armies into an existing theatre (an army leaves its army group).
    A theatre left empty is closed by the game. Returns {id: theatre id now}."""
    tg = theatres(g).get(tid)
    if not tg:
        raise OpError("theatre %s does not exist" % tid)
    ogs = _ogs(g, og_ids)
    _assign_theatre(g, ogs, tg)
    return {_hid(g, og): theatre_of(g, og) for og in ogs}


def clear_orders(g, og_ids):
    """Remove every order of these army groups and armies (and of a group's armies): their
    sectors on a line they no longer hold."""
    ao, p = _armyops(), g.p
    for og in _ogs(g, og_ids):
        for x in [og] + (members(g, og) if is_group(g, og) else []):
            for o in list(_orders(g, x)):
                ao.delete_order(g, x, p.i32(o + OI_ID))
    time.sleep(SETTLE)


AIMED = 6        # enemy divisions on a neutral's border, not facing us, that make it a road


def quiet_neutrals(g, mv, holders, tags, keep=()):
    """The tags among `tags` that are a wall, not a road: at peace with everybody, in no
    faction, no great power, no war goal justified on them, and fewer than AIMED divisions
    of our enemies on their border that do not stand against us too. (i42: the Swiss
    border was manned like a front all 1939; Germany never went through Switzerland.)"""
    C = B.module("combat")
    own = g.player_tag()
    majors = set(B.module("session").info(g).get("majors") or [])
    cand = []
    for t in tags:
        if t in keep or t in majors or t in LINE_KEEP:
            continue
        try:
            if C.enemy_tags(g, t) or g.faction_members(t)["members"]:
                continue
        except Exception:
            continue
        cand.append(t)
    if not cand:
        return set()
    try:
        aimed_at = {j["target"] for j in B.module("diplomacy").justifying(g)}
    except Exception:
        aimed_at = set()
    cand = [t for t in cand if t not in aimed_at]
    enemies = set(C.enemy_tags(g))
    owner = lambda pr: holders.get(mv.state_of.get(pr), (None, None))[1]
    G = B.module("game")
    at = collections.Counter()
    for e in enemies:
        try:
            for d in G._arr(g.p, g.country(e).ptr, 0x290):
                q = province_of(g, d)
                if q is not None:
                    at[q] += 1
        except Exception:
            continue
    out = set()
    for t in cand:
        n = 0
        for q, k in at.items():
            near = {owner(b) for b in mv.adj.get(q, ()) if b in mv.land}
            if t in near and own not in near:
                n += k
        if n < AIMED:
            out.add(t)
    return out


def falling_allies(g, mv, holders, at_war):
    """{ally tag: surrender progress} for the allies at war with our enemies whose
    surrender progress (the game's own) is 90% or more: about to capitulate."""
    own = g.player_tag()
    try:
        side = set(g.faction_members(own)["members"]) - {own}
    except Exception:
        return {}
    out = {}
    for t in side:
        try:
            if not set(B.module("combat").enemy_tags(g, t)) & set(at_war):
                continue
            sp = g.surrender_progress(t)
        except Exception:
            continue
        if 0.9 <= sp < 1.0:
            out[t] = sp
    return out


def land_borders(g, mv, holders, states, keep=()):
    """The countries our `states` touch over land, our faction and the quiet neutrals
    (quiet_neutrals; `keep` is never quiet) left out."""
    own = g.player_tag()
    try:
        friends = {t for t in g.faction_members(own)["members"] if isinstance(t, str)} | {own}
    except OSError:                  # in no faction: the pointer is not a faction
        friends = {own}
    # our subjects are no front either (23.10: Japan's line ran along the Korea-Manchukuo
    # border, both sides of it ours)
    try:
        friends |= set(B.module("combat").subject_tags(g, own))
    except Exception:
        pass
    # an ally is never a front: the game's front order against an ally holds nothing and
    # the divisions drift off (i45/i46); a falling ally's border gets a defence line
    # instead (ALLY FALLING in the situation, hold_line)
    owner = lambda q: holders.get(mv.state_of.get(q), (None, None))[1]
    out = set()
    for s in states:
        for pr in mv.land_provinces(int(s)):
            for n in mv.adj.get(pr, ()):
                if n in mv.land:
                    t = owner(n)
                    if t and t not in friends:
                        out.add(t)
    return out - quiet_neutrals(g, mv, holders, out, keep)


def territory(g, mv, holders, given):
    """{theatre id: its states}: the states the supreme gave each theatre, and every state
    we have taken from someone to the theatre whose states lie nearest to it over land (a
    front that advances takes its theatre with it)."""
    own = g.player_tag()
    out = {t: {int(s) for s in ss} for t, ss in given.items()}
    taken = [s for s, (o, c) in holders.items() if c == own and o != own]
    if not taken or not out:
        return out
    src = {}
    for t, ss in out.items():
        for s in ss:
            for pr in mv.land_provinces(s):
                src.setdefault(pr, t)
    dist, who = dict.fromkeys(src, 0), dict(src)
    q = collections.deque(src)
    while q:
        a = q.popleft()
        if dist[a] >= 40:
            continue
        for b in mv.adj.get(a, ()):
            if b in mv.land and b not in dist:
                dist[b], who[b] = dist[a] + 1, who[a]
                q.append(b)
    for s in taken:
        hit = [(dist[x], who[x]) for x in mv.land_provinces(s) if x in dist]
        if hit:
            out[min(hit)[1]].add(s)
    return out


def buffer_borders(g, mv, holders, foes, covered=()):
    """Neutral neighbours that also border a country our theatres face: the road an attack
    takes round a fortified border (Belgium in 1914 and 1940). [(tag, enemy it borders, our
    provinces on that border, how many of them have no division of ours)]. A border whose
    states all lie in a theatre (`covered`) is its field marshal's, and left out."""
    own = g.player_tag()
    owner = lambda pr: holders.get(mv.state_of.get(pr), (None, None))[1]
    ours = {p_ for s, (o, c) in holders.items() if c == own for p_ in mv.land_provinces(s)}
    near = collections.defaultdict(set)
    for pr in ours:
        for n in mv.adj.get(pr, ()):
            t = owner(n) if n in mv.land else None
            if t and t != own:
                near[t].add(pr)
    at = collections.Counter(province_of(g, d) for d in B.arr(g.p, g.player().ptr, 0x290))
    cap = g.p.i32(g.player().ptr + 0xFF0)                 # the capital state
    home = _bfs(mv, mv.land_provinces(cap), lambda p_: p_ in mv.land)
    try:
        friends = set(g.faction_members(own)["members"])
    except Exception:
        friends = set()
    # a great power is a war of its own, not a road (ENG showed up for its colonies)
    friends |= set(B.module("session").info(g).get("majors") or [])
    out = []
    for t, provs in near.items():
        if t in foes or t in friends:
            continue
        theirs = {p_ for s, (o, c) in holders.items() if c == t for p_ in mv.land_provinces(s)}
        touches = {owner(n) for pr in theirs for n in mv.adj.get(pr, ()) if n in mv.land} & set(foes)
        # a border of mountains is no road round a fortified line (19.10: the alert named
        # Switzerland, the staff turned the Alpine group against it, and the Italian border
        # in Savoy stood empty)
        provs = {pr for pr in provs if pr in home}      # not overseas (HOL: French Guiana)
        if provs and all(mv.state_of.get(pr) in covered for pr in provs):
            continue
        rough = sum(1 for pr in provs if mv.terrain.get(pr) == "mountain")
        empty = sum(1 for pr in provs if not at[pr])
        # only a border left mostly empty (SWI had 1 of 9 empty and still turned the
        # Alpine group away from Italy)
        if touches and provs and rough * 2 < len(provs) and empty * 2 >= len(provs):
            out.append((t, sorted(touches)[0], len(provs), empty))
    return out


def province_of(g, d):
    pr = g.p.u64(d + CARMY_PROV)
    return g.p.i32(pr + PROV_ID) if pr else None


def unassigned(g):
    p = g.p
    return [d for d in B.arr(p, g.player().ptr, 0x290) if not p.u64(d + 0xC0)]


def _orders(g, og):
    return B.arr(g.p, og, OG_ORDERS)


def _fronts(g, og):
    """The army's front line orders (its sectors)."""
    return [o for o in _orders(g, og) if g.p.i32(o + OI_TYPE) == 2]


def _provs(g, order):
    return frozenset(g.p.i32(pr + PROV_ID) for pr in B.arr(g.p, order, OI_PROVS))


def _divs(g, order):
    return [e - CARMY_SUB for e in B.arr(g.p, order, 0x210)]


def _on_army_sectors(g, ag):
    """Divisions of an army group that sit on one of its armies' own sectors."""
    return {d for m in members(g, ag) for o in _fronts(g, m) for d in _divs(g, o)}


_TARGETS = {}          # offensive order id -> target state labels, as ordered in this run
                       # (the game keeps an offensive's targets where we have not found them)


def _front_index(g):
    """CFront id -> pointer, live, from the game's handle table (about 1 ms)."""
    return _armyops().handle_objects(g, _armyops().FRONT_TYPE)


def _section_line(g, front, sec_id):
    """Provinces of one front section, in order along the line."""
    ao, p = _armyops(), g.p
    try:
        for s in B.arr(p, front, ao.FRONT_SECTIONS):
            if p.i32(s + ao.SEC_ID) == sec_id:
                return [p.i32(x + PROV_ID) for x in B.arr(p, s, ao.SEC_PROVS)]
    except OSError:
        # the game freed the front while we read it (i53, 02.12.1939: a garbage array
        # header ended the campaign); it is gone, so it has no line
        return []
    return []


def _span(n, frm, to):
    """Index range of a section's provinces covered by the fraction [frm, to)."""
    a = (frm * n + FULL // 2) // FULL           # rounded: 65384 of 26 provinces is 17
    return a, max(a + 1, (to * n + FULL // 2) // FULL)


def _sector_line(g, fronts, o):
    """Provinces of the front section a sector order stands on, as that section is NOW,
    cut to the part of it the order covers, in order along the line. The order's own
    province list (+0x88) is frozen when it is drawn: two months into the war Army 1's
    still ran through Slovakia and Kraków while its section had moved to Pomerania."""
    p = g.p
    f = fronts.get(p.i32(o + OI_FRONT))
    if not f:
        return []
    line = _section_line(g, f, p.i32(o + OI_SECTION))
    a, b = _span(len(line), p.i32(o + OI_FROM), p.i32(o + OI_TO))
    return line[a:b]


def _section_provs(g, fronts, o):
    return frozenset(_sector_line(g, fronts, o))


def _sector_states(g, mv, fronts, o, ours):
    """Our front-line states (`ours`) on the section this sector order stands on."""
    provs = _section_provs(g, fronts, o)
    return frozenset(s for s in (mv.state_of.get(x) for x in provs) if s in ours)


def _bfs(mv, sources, passable=None):
    dist = {p: 0 for p in sources}
    q = collections.deque(sources)
    while q:
        a = q.popleft()
        for b in mv.adj.get(a, ()):
            if b not in dist and (passable is None or passable(b)):
                dist[b] = dist[a] + 1
                q.append(b)
    return dist


PROV_CONTROLLER = 0x1A8        # u16 tag index (found 17.10: matches 599 of 600 sampled)
_PROV_PTRS = {}


def _province_ptrs(g):
    """province id -> CProvince*, built once per loaded game."""
    provs = g.oi.instances("CProvince")
    key = (provs[0], len(provs)) if provs else None
    if _PROV_PTRS.get("key") != key:
        _PROV_PTRS.clear()
        _PROV_PTRS["key"] = key
        _PROV_PTRS["map"] = {g.p.i32(pr + PROV_ID): pr for pr in provs}
    return _PROV_PTRS["map"]


def _passable(g, mv, holders):
    """Land provinces not controlled by an enemy, judged per province where it matters: a
    state the front runs through is held in parts (state control said our divisions
    advancing into Poland had no land route back to their own line)."""
    from .mapview import _tag
    enemies = set(B.module("combat").enemy_tags(g))
    own = g.player_tag()
    contested = {sid for sid, (o, c) in holders.items()
                 if o in enemies or c in enemies or o == own}
    ptrs = _province_ptrs(g)
    ctl = {}
    for sid in contested:
        for pr in mv.land_provinces(sid):
            ptr = ptrs.get(pr)
            if ptr:
                ctl[pr] = _tag(g, g.p.u16(ptr + PROV_CONTROLLER))

    def passable(pr):
        if pr not in mv.land:
            return False
        c = ctl.get(pr)
        if c is None:
            c = holders.get(mv.state_of.get(pr), (None, None))[1]
        return c not in enemies

    return passable


def _assign_nearest(g, mv, holders, og, only=None):
    """Share an army among its chosen sectors by their reachable province lengths.

    Within each land component keep existing assignments up to their quota, then fill
    the deficits with the nearest free divisions. `only` leaves every other division
    fixed, counting its existing load. Unreachable unassigned divisions stay in reserve
    (Moravian divisions must never be sent to an unreachable Königsberg enclave).
    Return verified division counts per order; unchanged assignments are not posted.
    """
    p = g.p
    passable = _passable(g, mv, holders)
    fronts = _front_index(g)
    sectors = _fronts(g, og)
    provs = [{pr for pr in _section_provs(g, fronts, o) if passable(pr)} for o in sectors]
    dists = [_bfs(mv, prs, passable) for prs in provs]
    divs = set(division_bases(g, og))
    movable = divs if only is None else divs & set(only)
    current = [set(_divs(g, o)) for o in sectors]
    plan = [ds - movable for ds in current]
    positions = {d: province_of(g, d) for d in divs}
    remaining, reached = set(movable), set()
    while remaining:
        d = min(remaining)
        pr = positions[d]
        if pr not in mv.land or not passable(pr):
            remaining.remove(d)
            continue
        component = _bfs(mv, [pr], passable)
        group = {d for d in remaining if positions[d] in component}
        remaining -= group
        lengths = [len(prs & component.keys()) for prs in provs]
        active = [i for i, n in enumerate(lengths) if n]
        if not active:
            continue
        reached |= group
        fixed = [sum(positions.get(d) in component for d in ds) for ds in plan]
        quota = list(fixed)
        empty = [i for i in active if not quota[i]]
        left = len(group)
        if left >= len(empty):
            for i in empty:
                quota[i] += 1
            left -= len(empty)
        # Allocate the next slot to the least filled piece per province. Fixed loads
        # are lower bounds, so a restricted `only` fills deficits instead of moving
        # excluded divisions or adding more to an already overfull piece.
        for _ in range(left):
            i = min(active, key=lambda i: ((quota[i] + 1) / lengths[i], i))
            quota[i] += 1
        need = [quota[i] - fixed[i] for i in range(len(sectors))]
        pool = set(group)
        for i in active:
            keep = sorted(pool & current[i], key=lambda d: (dists[i][positions[d]], d))[:need[i]]
            plan[i].update(keep)
            pool.difference_update(keep)
            need[i] -= len(keep)
        choices = sorted((dists[i][positions[d]], i, d) for i in active for d in pool)
        for _, i, d in choices:
            if d in pool and need[i]:
                plan[i].add(d)
                pool.remove(d)
                need[i] -= 1
    unreachable = movable - reached
    if any(ds & unreachable for ds in current):
        raise OpError("cannot assign: a division on an existing sector can reach none of "
                      "this army's sectors; its orders were left unchanged")
    changes = [(i, sorted(d for d in ds & movable
                          if [j for j, old in enumerate(current) if d in old] != [i]))
               for i, ds in enumerate(plan)]
    for i, ds in changes:
        if not ds:
            continue
        oid = p.i32(sectors[i] + OI_ID)
        ok, err = _armyops().assign_to_order(g, og, oid, ds)
        if not ok:
            raise OpError("could not assign divisions to sector %d: %s" % (oid, err))
    if any(ds for _, ds in changes):
        time.sleep(SETTLE)
    actual = [set(_divs(g, o)) for o in sectors]
    if any(have != want for have, want in zip(actual, plan)):
        raise OpError("sector assignment did not match the planned division quotas after post")
    return {p.i32(o + OI_ID): len(ds) for o, ds in zip(sectors, actual)}


def _march(g, mv, holders, line_provs):
    """Provinces to march from each province to a sector's line, over land not held by an
    enemy (a province missing from the result has no such route: cut off, or only by sea)."""
    passable = _passable(g, mv, holders)
    return _bfs(mv, [pr for pr in line_provs if passable(pr)], passable)


def _deployment(g, mv, holders, line_provs, divs):
    """Assigned is not deployed: divisions given to a sector still have to get there.
    (17.10: Bock's East Prussian divisions were all reassigned to Pomerania, left by sea,
    and the enclave fell before anyone arrived.)"""
    dist = _march(g, mv, holders, line_provs)
    on, way, cut = 0, [], 0
    for d in divs:
        h = dist.get(province_of(g, d))
        if h is None:
            cut += 1
        elif h <= 1:
            on += 1
        else:
            way.append(h)
    out = {"on_the_line": on}
    if way:
        out["marching_to_it"] = {"divisions": len(way), "farthest_provinces_away": max(way)}
    if cut:
        out["no_land_route_to_it"] = cut
    return out


def _plan_owners(g, og):
    """The orders groups whose orders make up this command's battle plan: an army, or an
    army group and each of its armies (an army inside a group may hold its own sector)."""
    return [og] + (members(g, og) if is_group(g, og) else [])


def _find_order(g, og, order_id):
    """(owning orders group, order) for an order anywhere in this command's plan."""
    for o_g in _plan_owners(g, og):
        try:
            return o_g, _order(g, o_g, order_id)
        except OpError:
            pass
    raise OpError("order %s not found in this command" % order_id)


def _order(g, og, order_id):
    for o in _orders(g, og):
        if g.p.i32(o + OI_ID) == order_id:
            return o
        for c in B.arr(g.p, o, OI_CHILDREN):
            if g.p.i32(c + OI_ID) == order_id:
                return c
    raise OpError("order %s not found in this army" % order_id)


# ------------------------------------------------------------------ observation
def _template(g, d):
    return B.module("game").item_name(g.p, g.p.u64(d + 0x3B8) + B.module("game").DT_NAME)


def division_mix(g, divs):
    """{template name: count}, biggest first."""
    return dict(collections.Counter(_template(g, d) for d in divs).most_common())


def visible_enemy(g, mv, holders, own, enemies):
    """Enemy divisions per state, limited to the states on our front line and our own
    states: a stand-in for fog of war (the player sees roughly this far)."""
    ours, theirs = mv.front_states(holders, own, enemies)
    seen = ours | theirs | {s for s, (_, c) in holders.items() if c == own}
    per = collections.defaultdict(lambda: [0, 0.0])
    pres = B.module("unitorders").enemy_presence(g, sorted(enemies))
    for pid, e in pres.items():
        s = mv.state_of.get(pid)
        if s in seen:
            per[s][0] += e["divisions"]
            per[s][1] += e["org"]
    return {mv.label(s): {"divisions": n, "avg_org": round(o / n, 1)}
            for s, (n, o) in sorted(per.items(), key=lambda kv: -kv[1][0]) if n}


def unguarded(g, mv, enemies):
    """Our land provinces on the border with an enemy-controlled province that no division
    of ours stands in, by state. (17.10: a front order too thin for its line left the
    whole stretch facing Poznań empty, and the Poles walked into Saxony.)"""
    from .mapview import _tag
    p, own = g.p, g.player_tag()
    ptrs = _province_ptrs(g)
    ctl = {}

    def held(pr):
        if pr not in ctl:
            ptr = ptrs.get(pr)
            ctl[pr] = _tag(g, p.u16(ptr + PROV_CONTROLLER)) if ptr else None
        return ctl[pr]
    manned = {province_of(g, d) for d in B.arr(p, g.player().ptr, 0x290)}
    out = collections.defaultdict(int)
    holders = mv.holders(g)
    for pr in mv.land:
        if held(pr) != own or pr in manned:
            continue
        # only our own states: a spearhead's flanks in enemy land always touch the enemy
        if holders.get(mv.state_of.get(pr), (None,))[0] != own:
            continue
        if any(b in mv.land and held(b) in enemies for b in mv.adj.get(pr, ())):
            out[mv.label(mv.state_of.get(pr))] += 1
    return dict(sorted(out.items(), key=lambda x: -x[1]))


def pockets(g, mv, enemies):
    """Enemy-held land cut off from the enemy's main body: {state label: divisions},
    per pocket. (17.10: Bock marched every army away from the Danzig pocket and it
    spread out again; nothing told him it was a pocket.)"""
    from .mapview import _tag
    p = g.p
    ptrs = _province_ptrs(g)
    held = {}
    for pr in mv.land:
        ptr = ptrs.get(pr)
        t = _tag(g, p.u16(ptr + PROV_CONTROLLER)) if ptr else None
        if t in enemies:
            held[pr] = t
    seen, parts = set(), []
    for pr in held:
        if pr in seen:
            continue
        comp, todo = [], [pr]
        seen.add(pr)
        while todo:
            a = todo.pop()
            comp.append(a)
            for b in mv.adj.get(a, ()):
                if b in held and b not in seen:
                    seen.add(b)
                    todo.append(b)
        parts.append(comp)
    if len(parts) < 2:
        return []
    parts.sort(key=len, reverse=True)
    pres = B.module("unitorders").enemy_presence(g, sorted(enemies))
    out = []
    own = g.player_tag()

    def ours(pr):
        ptr = ptrs.get(pr)
        return bool(ptr) and _tag(g, p.u16(ptr + PROV_CONTROLLER)) == own
    for comp in parts[1:]:                 # all but the main body
        # only land that touches ours: a colony overseas is cut off from France, not
        # encircled by us (18.10: Laos and French India paused the war as "pockets")
        if not any(ours(b) for a in comp for b in mv.adj.get(a, ()) if b in mv.land):
            continue
        divs = sum(pres.get(pr, {}).get("divisions", 0) for pr in comp)
        states = collections.Counter(mv.label(mv.state_of.get(pr)) for pr in comp)
        if divs or len(comp) >= 3:
            out.append({"states": [s for s, _ in states.most_common(3)],
                        "provinces": len(comp), "enemy_divisions": divs, "_provs": comp})
    return out


def order_of_battle(g):
    """Our armies and army groups as the supreme command sees them."""
    cap = caps()["army_divisions"]
    arm, grp = [], []
    for aid, og in sorted(armies(g).items()):
        n = len(division_bases(g, og))
        row = {"army": aid, "general": leader_name(g, og), "divisions": n,
               "composition": division_mix(g, division_bases(g, og))}
        if n > cap:
            row["OVER_LIMIT"] = "%d divisions, the limit is %d: split this army" % (n, cap)
        parent = g.p.u64(og + OG_PARENT)
        row["army_group"] = _hid(g, parent) if parent else None
        arm.append(row)
    for gid, ag in sorted(groups(g).items()):
        grp.append({"army_group": gid, "field_marshal": leader_name(g, ag),
                    "armies": [_hid(g, m) for m in members(g, ag)],
                    "divisions": len(division_bases(g, ag))})
    return {"armies": arm, "army_groups": grp}


def commander_key(g, og_id):
    """Character key of an army's or army group's commander (for the persona)."""
    name = leader_name(g, army(g, og_id))
    if not name:
        return None, None
    for r in g.generals()["generals"]:
        if r["name"] == name:
            return r["key"], name
    return None, name


def generals(g, rank="corps_commander"):
    """Commanders the player can assign now, best first. rank: corps_commander (leads
    an army) or field_marshal (leads an army group)."""
    rows = g.available_generals(rank=rank)["generals"]
    return [{k: r[k] for k in ("key", "name", "attack", "defense", "planning", "logistics")}
            for r in rows]


def command_power(g):
    c = g.player()
    return round(g.p.i64(c.ptr + B.module("game").CO_COMMAND_POWER) / 1e5, 1)


def unassigned_summary(g, mv):
    by_state = collections.Counter()
    for d in unassigned(g):
        by_state[mv.state_of.get(province_of(g, d))] += 1
    return {"total": sum(by_state.values()),
            "by_kind": division_mix(g, unassigned(g)),
            "by_state": {mv.label(s) if s else "?": n for s, n in by_state.most_common(15)}}


# ------------------------------------------------------------------ orders
def create_army(g, mv, holders, theatre_id, region, count=None, general_key=None,
                composition=None, take_from_armies=None):
    """New army in a theatre, of the divisions nearest its line, under general_key if given.
    `count` divisions of any kind, or `composition` {template name: count}. Divisions come
    from those in no army, and from the armies in take_from_armies if given (the game moves
    them out of their old army). More than the army limit is refused, not trimmed."""
    cap = caps()["army_divisions"]
    want = {k: int(v) for k, v in (composition or {}).items() if int(v) > 0}
    total = sum(want.values()) if want else int(count or 0)
    if total < 1:
        raise OpError("give a number of divisions or a composition")
    if total > cap:
        raise OpError("an army may hold at most %d divisions; asked for %d" % (cap, total))
    border = [pc["line"][i] for pc in theatre_line(g, mv, holders, region)
              for i in pc["idx"]] if region else []
    if not border:
        border = [p for s in region for p in mv.land_provinces(int(s))]
    # over land only: _bfs crosses sea provinces too (Taiwan "walked" to Jehol)
    dist = _march(g, mv, holders, border) if border else {}
    pool = list(unassigned(g))
    for aid in take_from_armies or []:
        og = army(g, int(aid))
        if is_group(g, og):
            raise OpError("%s is an army group; name its armies" % aid)
        pool += division_bases(g, og)
    # j, 07.1937: "North China" was built by template from the whole empire; 13 of its 23
    # divisions sat on Pacific islands and never reached the line. Only divisions that
    # can walk to the line are taken.
    # 23.10: those across the sea are taken last, not refused (10 divisions sat in Kanto
    # in no army for months): the reserve staging ships an army to its line
    sea = collections.Counter()
    if dist:
        for d in pool:
            if province_of(g, d) not in dist:
                sea[mv.label(mv.state_of.get(province_of(g, d)))] += 1
    pool.sort(key=lambda d: dist.get(province_of(g, d), 10 ** 6))
    over = ("; %d of the divisions on offer stand across the sea from this line (%s)"
            % (sum(sea.values()), ", ".join("%s %d" % kv for kv in sea.most_common(6)))
            ) if sea else ""
    if not pool:
        raise OpError("no divisions to take" + over)
    if want:
        pick, short = [], {}
        for tmpl, n in want.items():
            got = [d for d in pool if _template(g, d) == tmpl][:n]
            if len(got) < n:
                short[tmpl] = "%d of %d" % (len(got), n)
            pick += got
        if short:
            have = dict(collections.Counter(_template(g, d) for d in pool))
            raise OpError("not enough divisions of %s (there are: %s)%s" % (short, have, over))
    else:
        if len(pool) < total:
            raise OpError("only %d divisions to take, asked for %d%s" % (len(pool), total, over))
        pick = pool[:total]
    if general_key:
        free = {r["key"]: r["name"] for r in g.available_generals(rank="corps_commander")["generals"]}
        leading = {leader_name(g, og) for og in armies(g).values()}
        idle = [k for k, n in free.items() if n not in leading]
        # 16.06.1938: the army staff guessed four busy generals in a row and lost its turn
        rest = (", ".join(idle[:12]) if idle else
                "none: create the army without a general (leave general_key out), the game "
                "allows it")
        if general_key not in free:
            raise OpError("%r is not an available general. Generals with no army now: %s"
                          % (general_key, rest))
        if free[general_key] in leading:
            raise OpError("%s already leads an army. Generals with no army now: %s"
                          % (free[general_key], rest))
    before = set(armies(g))
    r = g.create_army(pick)
    time.sleep(SETTLE)
    new = set(armies(g)) - before
    if not new:
        raise OpError("army was not created: %s" % r)
    aid = new.pop()
    out = {"army": aid, "divisions": len(division_bases(g, armies(g)[aid])),
           "composition": division_mix(g, division_bases(g, armies(g)[aid]))}
    if theatre_id is not None and theatre_of(g, armies(g)[aid]) != theatre_id:
        move_to_theatre(g, [aid], theatre_id)
    out["theatre"] = theatre_of(g, armies(g)[aid])
    out["sector"] = "none yet: the theatre's field marshal gives it one (set_sectors)"
    far = sum(1 for d in pick if dist and province_of(g, d) not in dist)
    if far:
        out["across_the_sea"] = ("%d of its divisions stand across the sea: the harness ships "
                                 "the army to its line (convoys, some weeks)" % far)
    if general_key:
        try:
            out.update(appoint(g, aid, general_key))
        except OpError as e:
            out["general"] = "not appointed: %s" % e
    return out


def reinforce_army(g, mv, holders, army_id, count, take_from_armies=None):
    """Put `count` divisions into an existing army: those in no army first, then those of
    take_from_armies (an army left empty disappears). A division joins an army by being
    given one of its sectors (19.10: one from leaderless army 1137 went to Billotte's 409
    so; a new army with an army as its parent crashed the game)."""
    og = armies(g).get(int(army_id))
    if og is None:
        raise OpError("army %s does not exist" % army_id)
    secs = _fronts(g, og)
    cap = caps()["army_divisions"]
    if not secs:
        ok = ["%d (%d of %d%s)" % (i, len(division_bases(g, o)), cap,
                                   "" if leader_name(g, o) else ", no general")
              for i, o in armies(g).items() if _fronts(g, o) and len(division_bases(g, o)) < cap]
        raise OpError("army %s has no sector: divisions join an army through its sector. "
                      "Armies with a sector and room: %s" % (army_id, ", ".join(ok) or "none"))
    have = len(division_bases(g, og))
    room = cap - have
    if room <= 0:
        raise OpError("army %s is full (%d of %d divisions)" % (army_id, have, cap))
    n = min(int(count), room)
    pool = list(unassigned(g))
    taken = []
    for aid in take_from_armies or []:
        src = armies(g).get(int(aid))
        if src is None or int(aid) == int(army_id):
            continue
        taken += division_bases(g, src)
    if not pool and not taken:
        raise OpError("no divisions to give: none is outside an army, and no take_from_armies")
    # i38, 01.1940: the four divisions nearest Savoy were the southern end of the Maginot
    # by the Swiss border; taken off the line, the Germans walked in. A division facing
    # the enemy leaves only where another of ours stays in its province.
    enemies = set(B.module("combat").enemy_tags(g))
    hostile = {q for st, (_, c) in holders.items() if c in enemies for q in mv.land_provinces(st)}
    where = {d: province_of(g, d) for d in taken}
    stay = collections.Counter(where.values())
    held = 0
    for d in sorted(taken, key=lambda d: any(b in hostile for b in mv.adj.get(where[d], ()))):
        q = where[d]
        if any(b in hostile for b in mv.adj.get(q, ())):
            if stay[q] < 2:
                held += 1
                continue
            stay[q] -= 1
        pool.append(d)
    if not pool:
        raise OpError("every division of %s stands alone on a province facing the enemy: "
                      "taking one opens the line. Take from a reserve or a quiet stretch"
                      % take_from_armies)
    line = _section_provs(g, _front_index(g), secs[0])
    dist = _march(g, mv, holders, set(line))
    sea = [d for d in pool if province_of(g, d) not in dist and province_of(g, d) in mv.land]
    pool = [d for d in pool if province_of(g, d) in dist]
    pick = sorted(pool, key=lambda d: (d in where and any(
        b in hostile for b in mv.adj.get(where[d], ())), dist.get(province_of(g, d), 10 ** 6)))[:n]
    shipped, not_shipped = 0, []
    if sea and len(pick) < n:
        # across the sea: put on the sector, the game ships them port to port (23.10: 17
        # new divisions stood in Japan for a year, refused as "across the sea")
        try:
            go, not_shipped, _, _, _ = _shippable(g, mv, holders, sea, sorted(line))
        except OpError as e:
            go, not_shipped = [], [str(e)]
        go = go[:n - len(pick)]
        shipped = len(go)
        pick += go
    if not pick:
        raise OpError("none of the divisions you can give can reach this army's line: %s"
                      % ("; ".join(not_shipped) or "none stands on its land"))
    ok, err = _armyops().assign_to_order(g, og, g.p.i32(secs[0] + OI_ID), pick)
    if not ok:
        raise OpError("the game refused: %s" % err)
    time.sleep(SETTLE)
    r = {"army": int(army_id), "divisions_now": len(division_bases(g, og)),
         "added": len(pick), "limit": cap}
    if shipped:
        r["by_sea"] = ("%d of them cross the sea from port to port; the game ships them and "
                       "they march to the line" % shipped)
    if not_shipped:
        r["not_shipped"] = not_shipped
    if held and len(pick) < int(count):
        r["kept_on_the_line"] = ("%d divisions stand alone on provinces facing the enemy "
                                 "and were not taken" % held)
    return r


def split_army(g, mv, holders, army_id, count):
    """Take `count` divisions out of an army into a new army without a general (the UI's
    'new army from selected divisions'). Which ones: the farthest from the enemy, so the
    front line stays manned."""
    og = armies(g).get(army_id)
    if not og:
        raise OpError("army %s does not exist" % army_id)
    divs = division_bases(g, og)
    if int(count) >= len(divs):
        raise OpError("army %d has only %d divisions" % (army_id, len(divs)))
    enemies = set(B.module("combat").enemy_tags(g))
    hostile = [p for s, (_, c) in holders.items() if c in enemies for p in mv.land_provinces(s)]
    dist = _bfs(mv, hostile)
    pick = sorted(divs, key=lambda d: -dist.get(province_of(g, d), 10 ** 6))[:int(count)]
    before = set(armies(g))
    ok, err = _armyops().create_army(g, pick)
    if not ok:
        raise OpError("split refused: %s" % err)
    time.sleep(SETTLE)
    new = set(armies(g)) - before
    if not new:
        raise OpError("split was posted but no new army appeared")
    aid = new.pop()
    return {"new_army": aid, "divisions": len(division_bases(g, armies(g)[aid])),
            "left_in_army": len(division_bases(g, og))}


def appoint(g, og_id, general_key):
    """A corps commander to an army, or a field marshal to an army group."""
    og = army(g, og_id)
    names = {r["key"]: r["name"] for r in g.generals()["generals"]}
    before = {i: leader_name(g, o) for i, o in list(armies(g).items()) + list(groups(g).items())}
    r = g.assign_commander(og, general_key)
    if not r.get("ok"):
        raise OpError(r.get("error") or "appointment refused")
    time.sleep(SETTLE)
    name = leader_name(g, og)
    if not name:
        raise OpError("commander not set: %s" % r)
    out = {"id": og_id, "commander": name}
    was = [i for i, n in before.items() if n == names.get(general_key) and i != og_id]
    if was:                     # the game moves him; his old command is left without one
        out["moved_from"] = was
        out["now_without_commander"] = [i for i in was if not leader_name(g, army(g, i))]
    return out


def promote(g, general_key):
    """Corps commander -> field marshal, for command power."""
    before = command_power(g)
    r = g.promote_general(general_key)
    time.sleep(SETTLE)
    if not r.get("promoted"):
        raise OpError(r.get("error") or r.get("note") or "promotion refused: %s" % r)
    return {"promoted": general_key, "command_power_before": before,
            "command_power_left": command_power(g)}


def form_army_group(g, mv, holders, army_ids, field_marshal_key):
    """Group armies of one theatre under a field marshal: an army group of the game (his
    skills and traits work for its armies). The armies keep their sectors; the theatre's
    field marshal commands them as before, and the group stays in their theatre."""
    ao, p = _armyops(), g.p
    lim = caps()["field_marshal_armies"]
    if not 1 <= len(army_ids) <= lim:
        raise OpError("an army group takes 1 to %d armies; got %d" % (lim, len(army_ids)))
    fms = {r["key"]: r["name"] for r in g.available_generals(rank="field_marshal")["generals"]}
    if field_marshal_key not in fms:
        raise OpError("%s is not an available field marshal (available: %s); promote a "
                      "general first" % (field_marshal_key, ", ".join(fms) or "none"))
    leading = {leader_name(g, og) for og in list(armies(g).values()) + list(groups(g).values())}
    if fms[field_marshal_key] in leading:
        raise OpError("%s already commands an army or army group; appoint another general to "
                      "it first (appoint), or choose another field marshal"
                      % fms[field_marshal_key])
    arm = armies(g)
    ogs = []
    for aid in army_ids:
        og = arm.get(int(aid))
        if not og:
            raise OpError("army %s does not exist" % aid)
        ogs.append(og)
    where = {theatre_of(g, og) for og in ogs}
    if len(where) > 1:
        raise OpError("the armies stand in different theatres (%s); an army group is formed "
                      "inside one theatre. Moving armies between theatres is Le Chonk's "
                      "decision" % ", ".join(map(str, sorted(x for x in where if x))))
    tid = where.pop()
    taken = {}
    for og in ogs:                           # out of their old army groups first
        if p.u64(og + OG_PARENT):
            taken[_hid(g, og)] = _hid(g, p.u64(og + OG_PARENT))
    if taken:
        ok, err = ao.remove_from_army_group(g, [og for og in ogs if p.u64(og + OG_PARENT)])
        if not ok:
            raise OpError("could not take armies out of their army groups: %s" % err)
        time.sleep(SETTLE)
    ok, err = ao.create_army_group(g, ogs)
    if not ok:
        raise OpError("army group refused: %s" % err)
    time.sleep(SETTLE)
    ag = p.u64(ogs[0] + OG_PARENT)
    if not ag:
        raise OpError("army group was posted but did not appear")
    gid = _hid(g, ag)
    out = {"army_group": gid, "armies": [_hid(g, m) for m in members(g, ag)]}
    if taken:
        out["taken_from_army_groups"] = taken
    if tid is not None and theatre_of(g, ag) != tid:
        move_to_theatre(g, [gid], tid)
    out["theatre"] = theatre_of(g, ag)
    try:
        out.update(appoint(g, gid, field_marshal_key))
    except OpError as e:
        out["field_marshal"] = "not appointed: %s" % e
    return out


def transfer_army(g, mv, holders, army_id, group_id):
    """Move an army into another army group of its theatre; it keeps its sector. An army
    group left with no army is deleted by the game, and the result says so."""
    ao, p = _armyops(), g.p
    og, ag = army(g, army_id), army(g, group_id)
    if is_group(g, og):
        raise OpError("%s is an army group, not an army" % army_id)
    if not is_group(g, ag):
        raise OpError("%s is an army, not an army group" % group_id)
    old = p.u64(og + OG_PARENT)
    if old == ag:
        raise OpError("army %s is already in army group %s" % (army_id, group_id))
    if theatre_of(g, og) != theatre_of(g, ag):
        raise OpError("army group %s stands in another theatre; moving armies between "
                      "theatres is Le Chonk's decision" % group_id)
    old_id = _hid(g, old) if old else None
    ok, err = ao.assign_to_army_group(g, [og], ag)
    if not ok:
        raise OpError("transfer refused: %s" % err)
    time.sleep(SETTLE)
    if p.u64(og + OG_PARENT) != ag:
        raise OpError("army %s is not in army group %s after the transfer" % (army_id, group_id))
    out = {"army": army_id, "now_in_army_group": group_id, "came_from_army_group": old_id}
    if old_id is not None and old_id not in groups(g):
        out["army_group_dissolved"] = old_id
    # inside one theatre the army keeps its sector: the theatre's field marshal cut it
    return out


def balance(g, mv, holders, enemies):
    """Per enemy: divisions on each side in the border zone (our states along its border
    and its states along ours, wherever the line runs now)."""
    p, own = g.p, g.player_tag()
    ours_by_state = collections.Counter(mv.state_of.get(province_of(g, d))
                                        for d in B.arr(p, g.player().ptr, 0x290))
    pres = B.module("unitorders").enemy_presence(g, sorted(enemies))
    theirs_by_state = collections.Counter()
    for pid, e in pres.items():
        theirs_by_state[mv.state_of.get(pid)] += e["divisions"]
    out = {}
    for tag in sorted(enemies):
        o, t = mv.front_states(holders, own, {tag})
        if not o:
            continue
        zone = o | t
        out[tag] = {"our_divisions_in_the_border_zone": sum(ours_by_state[s] for s in zone),
                    "enemy_divisions_seen_there": sum(theirs_by_state[s] for s in zone)}
    return out


def allies_out_of_war(g, mv, holders, enemies):
    """Members of our faction not at war with an enemy of ours: [{ally, divisions,
    borders: enemies it touches, not_at_war_with}], those on an enemy's border first."""
    cb = B.module("combat")
    own = g.player_tag()
    out = []
    try:
        members = g.faction_members(own)["members"]
    except OSError:                  # in no faction: the pointer is not a faction
        return out
    for ally in members:
        if ally == own or not isinstance(ally, str) or ally.startswith("0x"):
            continue
        theirs = {w["enemy"] for w in cb.war_relations(g, ally)}
        miss = sorted(set(enemies) - theirs)
        if not miss:
            continue
        touch = set()
        for sid, (_, c) in holders.items():
            if c == ally:
                touch |= {holders.get(n, (None, None))[1] for n in mv.neighbours.get(sid, ())}
        out.append({"ally": ally, "divisions": len(B.arr(g.p, g.country(ally).ptr, 0x290)),
                    "borders": sorted(touch & set(miss)), "not_at_war_with": miss})
    out.sort(key=lambda a: (not a["borders"], -a["divisions"]))
    return out


def call_ally(g, ally, enemy):
    """Call a faction member into our war against `enemy`; read back whether it came."""
    ok, err = B.module("diplomacy").call_ally(g, ally, enemy)
    if not ok:
        raise OpError(err)
    time.sleep(SETTLE * 2)
    joined = any(w["enemy"] == enemy for w in B.module("combat").war_relations(g, ally))
    return {"called": ally, "against": enemy,
            "joined": joined or "not yet (the ally answers on its own; check next turn)"}


def justify_war_goal(g, tag):
    """Start justifying a war goal against `tag` (the game sets the days and the cost);
    the first kind the game allows of: topple the government, annex everything."""
    d = B.module("diplomacy")
    errs = []
    for kind in ("topple_government", "annex_everything"):
        ok, err = d.justify_wargoal(g, tag, kind)
        if ok:
            time.sleep(SETTLE)
            mine = [j for j in d.justifying(g) if j["target"] == tag]
            return {"justifying": kind, "against": tag,
                    "days_left": mine[0]["days_left"] if mine else "starts tomorrow"}
        errs.append("%s: %s" % (kind, err))
    raise OpError("the game allows no war goal against %s now (%s)" % (tag, "; ".join(errs)))


def declare_war(g, tag):
    r = g.declare_war(tag)
    time.sleep(SETTLE)
    enemies = B.module("combat").enemy_tags(g)
    if tag not in enemies:
        raise OpError("not at war with %s after the declaration: %s" % (tag, r))
    return {"at_war_with": enemies}


def draw_front(g, mv, holders, army_id, enemy_tag, assign=True, states=None):
    """Front line orders along every stretch of the border with enemy_tag that the army
    does not cover yet; with assign, divisions share reachable sectors by their length,
    nearest within each quota (see _assign_nearest). With `states` (our state ids), only the part of each
    stretch running from the first to the last of those states is drawn: how two army
    groups share one border (Bock from Pomerania, Rundstedt from Silesia).

    The game keeps many CFront objects for the same border (46 copies of 4 stretches
    against Poland in November 1939, several of them fragments of one front). A stretch
    counts as covered when its states are already on one of the army's sectors; drawing
    one order per copy is what gave every army a dozen duplicate sectors."""
    ao = _armyops()
    og = army(g, army_id)
    ours = front_line_states(g, mv, enemy_tag)
    fronts = _front_index(g)
    covered = set()
    used = set()
    for o in _fronts(g, og):
        covered |= _sector_states(g, mv, fronts, o, ours)
        used.add(g.p.i32(o + OI_FRONT))
    segs = _our_sections(g, enemy_tag)
    if not segs:
        raise OpError("no front with %s" % enemy_tag)
    # longest stretches first; among copies, the CFront the army already uses, then the oldest
    segs.sort(key=lambda s: (-len(s["border_provinces"]), s["front_handle"][1] not in used,
                             s["front_handle"][1]))
    want = None
    if states:
        want = {int(x) for x in states}
        if not want & ours:
            raise OpError("none of %s is one of our states on the border with %s (those are: "
                          "%s)" % (sorted(want), enemy_tag, ", ".join(map(mv.label, sorted(ours)))))
    drawn = []
    for s in segs:
        line = _section_line(g, s["front"], s["section"])
        frm, to = 0, FULL
        if want:
            idx = [i for i, pr in enumerate(line) if mv.state_of.get(pr) in want]
            if not idx:
                continue
            mine = [i for i, pr in enumerate(line) if mv.state_of.get(pr) in ours]
            # the line's ends outside our states (Slovak and Moravian provinces of the Polish
            # front in 1939) go with our state nearest to them, so they are nobody's gap
            lo = 0 if idx[0] == mine[0] else idx[0]
            hi = len(line) if idx[-1] == mine[-1] else idx[-1] + 1
            frm, to = lo * FULL // len(line), hi * FULL // len(line)
        a, b = _span(len(line), frm, to)
        here = {mv.state_of.get(pr) for pr in line[a:b]} & ours
        if not here or here <= covered:
            continue
        ok, err = ao.draw_front_line(g, og, s["front"], s["section"], frm, to)
        if not ok:
            raise OpError("front line refused: %s" % err)
        time.sleep(SETTLE)
        covered |= here
        drawn.append([mv.label(x) for x in _in_line_order(mv, line[a:b], here)])
    merged = consolidate(g, mv, holders, army_id, enemy_tag)
    if assign and drawn:
        # an army group's new sectors take only divisions no army holds on its own sector
        # (17.10: Le Chonk redrew Bock's front and every army sector was left empty)
        free = set(division_bases(g, og)) - _on_army_sectors(g, og) if is_group(g, og) \
            else None
        _assign_nearest(g, mv, holders, og, only=free)
    n = len(_fronts(g, og))
    if not n:
        raise OpError("no front line order appeared")
    # the ids to give orders with: models reused the replaced ids (17.10, Rundstedt)
    out = {"army": army_id, "sectors": n, "new_sectors": drawn,
           "sector_ids_now": [g.p.i32(o + OI_ID) for o in _fronts(g, og)]}
    if merged["merged"]:
        out["housekeeping"] = merged
    return out


def _in_line_order(mv, provs, states):
    out = []
    for pr in provs:
        st = mv.state_of.get(pr)
        if st in states and st not in out:
            out.append(st)
    return out


def _fill_offensives(g, og, parent):
    """Put the sector's divisions that are on none of its offensive lines onto them, the
    emptiest line first. The game splits one offensive into several arrows and at times
    leaves some, or all, without divisions (seen: 26 divisions in Manstein's sector, four
    arrows with none); an arrow without divisions does nothing. Returns {arrow: added}."""
    p = g.p
    kids = B.arr(p, parent, OI_CHILDREN)
    if not kids:
        return {}
    load = {c: [e - CARMY_SUB for e in B.arr(p, c, 0x210)] for c in kids}
    on = {d for ds in load.values() for d in ds}
    left = [d for d in _divs(g, parent) if d not in on]
    plan = collections.defaultdict(list)
    for d in left:
        c = min(kids, key=lambda k: len(load[k]) + len(plan[k]))
        plan[c].append(d)
    out = {}
    for c, ds in plan.items():
        oid = p.i32(c + OI_ID)
        ok, err = _armyops().assign_to_order(g, og, oid, ds)
        if ok:
            out[oid] = len(ds)
    if out:
        time.sleep(SETTLE)
    return out


def consolidate(g, mv, holders, army_id, enemy_tag):
    """Fold sectors that stand on the same stretch of border (same or fewer of our states,
    as the front is now) into one, moving their divisions over first. Duplicates are an
    artefact of how front orders get drawn and of the game re-splitting fronts, not a
    choice anyone made; left alone they spread an army one division per copy. The sector
    kept is the one with offensives, then the most divisions; offensive lines of the
    folded copies are lost and listed in the result. Sectors off the border are left."""
    ao = _armyops()
    og = army(g, army_id)
    p = g.p
    ours = front_line_states(g, mv, enemy_tag)
    fronts = _front_index(g)
    # by provinces, not states: an army cut by provinces holds two pieces in one state
    # (18.12), and those are not copies of each other
    geo = {o: _section_provs(g, fronts, o) if _sector_states(g, mv, fronts, o, ours)
           else frozenset() for o in _fronts(g, og)}
    order = sorted((o for o in geo if geo[o]), key=lambda o: -len(geo[o]))
    groups = []
    for o in order:
        for grp in groups:
            if geo[o] <= geo[grp[0]]:
                grp.append(o)
                break
        else:
            groups.append([o])
    merged, lost = [], []
    for members in groups:
        if len(members) < 2:
            continue
        keep = max(members, key=lambda o: (len(B.arr(p, o, OI_CHILDREN)) > 0,
                                           sum(len(B.arr(p, c, 0x210))
                                               for c in B.arr(p, o, OI_CHILDREN)),
                                           len(_divs(g, o)), len(geo[o])))
        rest = [o for o in members if o != keep]
        moving = []
        for o in rest:
            for d in _divs(g, o):
                if d not in moving:
                    moving.append(d)
        if moving:
            ok, err = ao.assign_to_order(g, og, p.i32(keep + OI_ID), moving)
            if not ok:
                raise OpError("could not move divisions while merging sectors: %s" % err)
            time.sleep(SETTLE)
        folded = [p.i32(o + OI_ID) for o in rest]       # read before the objects are freed
        for o, oid in zip(rest, folded):
            lost += [p.i32(c + OI_ID) for c in B.arr(p, o, OI_CHILDREN)]
            ao.delete_order(g, og, oid)
        time.sleep(SETTLE)
        merged.append({"kept": p.i32(keep + OI_ID), "folded": folded,
                       "divisions_moved": len(moving)})
    filled = {}
    for o in _fronts(g, og):
        filled.update(_fill_offensives(g, og, o))
    out = {"merged": merged, "offensives_removed": lost}
    if filled:
        out["divisions_put_on_empty_offensive_lines"] = filled
    return out


# Extra cost of entering a province, in "plains provinces": the attacker's real price of
# the ground (HoI4 penalises attacks into these), so a route goes round a marsh or a
# mountain range when a flat way of similar length exists.
TERRAIN_COST = {"plains": 0, "desert": 0.2, "urban": 0.8, "forest": 0.7, "hills": 0.8,
                "jungle": 1.2, "marsh": 1.5, "mountain": 2.0}
RIVER_COST = {"small": 1.0, "large": 2.0}


def _objective(mv, sid, held=None, enemies=()):
    """The province an attack on a state aims at: its biggest city, else its middle; in a
    state the front runs through, of the part the enemy still holds (18.10: arrows at
    Kraków's and Warsaw's cities, ours already, were drawn by the game as nothing)."""
    from . import mapimage
    provs = mv.land_provinces(sid)
    if held is not None:
        theirs = [p for p in provs if held(p) in enemies]
        provs = theirs or provs
    vps = [(vp, pr) for pr, vp, _ in mapimage._vp_provinces(mv, sid) if pr in provs]
    if vps:
        return max(vps)[1]
    st = mapimage.static()
    cx = sum(st["cx"][p] for p in provs) / len(provs)
    cy = sum(st["cy"][p] for p in provs) / len(provs)
    return min(provs, key=lambda p: (st["cx"][p] - cx) ** 2 + (st["cy"][p] - cy) ** 2)


def _route(g, mv, starts, goal, enemies, through_ours=False):
    """Cheapest land route from any of `starts` (our line) to `goal`, through provinces
    the enemy (or nobody of ours) controls; through_ours: also through ours (a leg from a
    city further on, where the spearheads already took ground). Dijkstra."""
    import heapq
    from .mapview import _tag
    ptrs, own = _province_ptrs(g), g.player_tag()
    ctl = {}

    def held(pr):
        if pr not in ctl:
            ptr = ptrs.get(pr)
            ctl[pr] = _tag(g, g.p.u16(ptr + PROV_CONTROLLER)) if ptr else None
        return ctl[pr]
    dist = {s: 0.0 for s in starts}
    prev = {}
    q = [(0.0, s) for s in starts]
    heapq.heapify(q)
    while q:
        d, a = heapq.heappop(q)
        if a == goal:
            break
        if d > dist.get(a, 1e9):
            continue
        for b in mv.adj.get(a, ()):
            if b not in mv.land or (held(b) == own and b != goal and not through_ours):
                continue
            if held(b) not in enemies and held(b) != own:
                continue                      # not through neutral countries
            r = mv.rivers.get((a, b)) or mv.rivers.get((b, a))
            nd = d + 1 + TERRAIN_COST.get(mv.terrain.get(b), 0.5) + RIVER_COST.get(r, 0)
            if nd < dist.get(b, 1e9):
                dist[b], prev[b] = nd, a
                heapq.heappush(q, (nd, b))
    if goal not in dist:
        return None
    path = [goal]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    return path[::-1]


def enforce_execution(g, theatre_id, run=True):
    """Bring every order of the theatre's plan (its groups' and armies') to the marshal's
    decision. The game's execute sets the orders that exist; a sector or arrow drawn
    afterwards starts idle: on 17.10 Bock's whole plan, 51 divisions, stood still for
    two weeks of war after he redrew it. Returns the orders that were off."""
    p = g.p
    off = []
    for owner in theatre_members(g, theatre_id):
        todo = list(_orders(g, owner))
        while todo:                    # sectors, their arrows, arrows chained to arrows
            x = todo.pop()
            todo += B.arr(p, x, OI_CHILDREN)
            if bool(p.u8(x + OI_EXEC)) != bool(run):
                off.append(p.i32(x + OI_ID))
    if off:
        execute(g, theatre_id, run)
    return off


def _route_states(g, mv, arrow):
    """The states an arrow runs through, from the game's own path for it."""
    st = []
    for pr in _arrow_path(g, arrow):
        sid = mv.state_of.get(pr)
        if sid is not None and (not st or st[-1] != sid):
            st.append(sid)
    return [mv.label(x) for x in st]


def _arrow_path(g, arrow):
    """The provinces an offensive arrow runs through, as the game stored it."""
    from .mapimage import _i32_array
    return _i32_array(g.p, arrow + 0x70)


def p_type(g, order):
    return g.p.i32(order + OI_TYPE)


def fallback(g, mv, theatre_id, state_ids):
    """A fallback line through our states for the theatre: drawn for each of its army
    groups and each army in no group."""
    provs = [p for s in state_ids for p in mv.land_provinces(s)]
    done = []
    for og in theatre_members(g, theatre_id):
        if not is_group(g, og) and g.p.u64(og + OG_PARENT):
            continue
        ok, err = _armyops().fallback_line(g, og, provs)
        if not ok:
            raise OpError("fallback line refused: %s" % err)
        done.append(_hid(g, og))
    time.sleep(SETTLE)
    return {"drawn_for": done, "fallback_states": [mv.label(s) for s in state_ids]}


def cancel(g, army_id, order_id):
    og, _ = _find_order(g, army(g, army_id), order_id)
    ok, err = _armyops().delete_order(g, og, order_id)
    time.sleep(SETTLE)
    if not ok:
        raise OpError("delete refused: %s" % err)
    try:
        _order(g, og, order_id)
    except OpError:
        return {"deleted": order_id}
    raise OpError("order %s still exists after the delete" % order_id)


# The game's own evaluation of an army's battle plan, the colour of the order's icon
# (found 25.10: COrdersGroup's update, vtable slot 11, caches it; the icon is green at
# PLAN_CONSIDERED_GOOD or better, red at PLAN_CONSIDERED_BAD or worse, yellow between).
# Beside it the two sums it weighs: the strength of the army's divisions and of the enemy
# divisions its orders face, each division's strength being the sum of its combat stats
# scaled by its strength and organisation.
OG_EVAL, OG_EVAL_OURS, OG_EVAL_HIS = 0x1F8, 0x200, 0x208
OG_EVAL_DIRTY = 0x19F
G_PLAN_GOOD, G_PLAN_BAD = 0x4598F68, 0x4598F78     # NMilitary.PLAN_CONSIDERED_GOOD / _BAD


def plan_eval(g, og):
    """{value, colour, ours, his, stale} of the army's battle plan, as the game shows it."""
    p = g.p
    try:
        good, bad = p.i64(p.va(G_PLAN_GOOD)) / 1e5, p.i64(p.va(G_PLAN_BAD)) / 1e5
    except Exception:
        good, bad = 0.25, -0.25
    v = p.i64(og + OG_EVAL) / 1e5
    return {"value": v, "colour": "green" if v >= good else "yellow" if v > bad else "red",
            "ours": p.i64(og + OG_EVAL_OURS) / 1e5, "his": p.i64(og + OG_EVAL_HIS) / 1e5,
            "stale": bool(p.u8(og + OG_EVAL_DIRTY))}


def plan_eval_text(e):
    return "%s (%+.2f: ours %.0f, his %.0f%s)" % (
        e["colour"].upper(), e["value"], e["ours"], e["his"],
        ", before the last change" if e["stale"] else "")


def execute(g, theatre_id, run=True):
    """Run or stop the theatre's whole plan: its army groups' orders and its armies'. An
    army whose plan the game rates red is not set running (the user, 25.10: a red plan is
    never launched, a yellow one may be); it is reported instead."""
    done, red = [], []
    for o_g in theatre_members(g, theatre_id):
        if not _orders(g, o_g):
            continue
        if run and not g.p.u8(o_g + 0x39):
            e = plan_eval(g, o_g)
            if e["colour"] == "red":
                red.append("%s %s" % (_hid(g, o_g), plan_eval_text(e)))
                continue
        ok, err = _armyops().execute_plan(g, o_g, 0, run=run)
        if not ok:
            raise OpError(err)
        done.append(_hid(g, o_g))
    out = {"executing": bool(run), "plans": done}
    if red:
        out["not_launched_red"] = red
        out["note"] = ("the game rates these armies' plans RED: not launched. Mass more "
                       "divisions on the arrow, shorten it to a weaker point, or turn the "
                       "front another way (a landing)")
    return out


# ------------------------------------------------------------------ the front: army sectors
# An army group has no sector of its own: its front is its armies' sectors, as in the game
# when a front is drawn for an army group with armies in it. (17.10: group sectors and
# army sectors side by side; divisions belonged now to one, now to the other, and the
# marshals spent their turns moving them back and forth. docs/review-2026-10-17.md)
def _ctl(g):
    """province id -> controller tag, read on demand and kept for one call."""
    from .mapview import _tag
    ptrs, cache = _province_ptrs(g), {}

    def held(pr):
        if pr not in cache:
            ptr = ptrs.get(pr)
            cache[pr] = _tag(g, g.p.u16(ptr + PROV_CONTROLLER)) if ptr else None
        return cache[pr]
    return held


def tags_of(spec):
    """'GER+BEL+LUX' -> ['GER', 'BEL', 'LUX']: an army group's line may face several
    countries (the user, 19.10: a group "against Germany" kept all five armies at the
    Maginot line while the Belgian border stood empty)."""
    return [t for t in str(spec).upper().split("+") if t]


def _our_sections(g, enemy_tag):
    """The front sections against enemy_tag (one country, or several as 'GER+BEL') that run
    on our side. The game also keeps the enemy's side of each front, its provinces all his
    (18.10: 25 Polish provinces a copy); a sector drawn there leaves our divisions with no
    land route to it."""
    held = _ctl(g)
    tags = tags_of(enemy_tag)
    at_war = set(B.module("combat").enemy_tags(g))
    out = []
    for tag in tags:
        # the far side is the land of this front's country and of those at war with us;
        # not every neighbour's (21.10: Belgium joined the war, the German front ran on
        # through 8 Belgian provinces, counted as enemy, and the Maginot front was dropped
        # as "the enemy's side": no army held it)
        far = at_war | {tag}
        for s in _armyops().fronts_towards(g, tag):
            line = _section_line(g, s["front"], s["section"])
            if line and sum(held(pr) in far for pr in line) * 2 < len(line):
                out.append(s)
    return out


def front_line_states(g, mv, enemy_tag):
    """Our states the game's front lines against enemy_tag run through now, province by
    province. (18.10: taken by state controller, the border still listed Upper Silesia a
    month after the front had moved to the Vistula, and every sector drawn there failed:
    the game's line ran through Łódź and Kraków, half taken.)"""
    out = set()
    for s in _our_sections(g, enemy_tag):
        out |= {mv.state_of.get(pr) for pr in _section_line(g, s["front"], s["section"])}
    out.discard(None)
    return out


def border_stretches(g, mv, holders, enemy_tag):
    """Our state ids along the front with enemy_tag, in order along each stretch."""
    ours = front_line_states(g, mv, enemy_tag)
    out, seen = [], set()
    for s in sorted(_our_sections(g, enemy_tag),
                    key=lambda s: -len(s["border_provinces"])):
        st = _in_line_order(mv, _section_line(g, s["front"], s["section"]), ours)
        if st and not set(st) <= seen:
            seen |= set(st)
            out.append(st)
    return out


def border_line(g, mv, holders, enemy_tag):
    return [[mv.label(x) for x in st] for st in border_stretches(g, mv, holders, enemy_tag)]


def _army_sector_states(g, mv, fr, og, ours):
    return set().union(*[_sector_states(g, mv, fr, o, ours) for o in _fronts(g, og)])


# ------------------------------------------------------------------ the line, cut by province
def _steps(mv, a, b, cap=4):
    """Land steps from province a to b, None beyond cap."""
    if a == b:
        return 0
    seen, todo = {a}, [a]
    for k in range(1, cap + 1):
        nxt = []
        for q in todo:
            for n in mv.adj.get(q, ()):
                if n == b:
                    return k
                if n not in seen and n in mv.land:
                    seen.add(n)
                    nxt.append(n)
        todo = nxt
    return None


def line_pieces(g, mv, enemy_tag, states=None):
    """Our side of the line against enemy_tag as pieces of the game's front sections, in
    order along the line: [{front, fid, sec, line, idx}], idx the section's province
    indices in line order (reversed when the section runs the other way). The game keeps
    copies of a stretch; a section mostly covered by a longer one is left out. `states`:
    only the provinces in them (a theatre's), each unbroken run its own piece."""
    held_ = _ctl(g)
    far_ = set(tags_of(enemy_tag)) | set(B.module("combat").enemy_tags(g))
    secs = []
    for s in _our_sections(g, enemy_tag):
        line = _section_line(g, s["front"], s["section"])
        if line:
            secs.append((s, line))
    secs.sort(key=lambda x: -len(x[1]))
    pieces, seen, taken, cand = [], set(), set(), []
    for s, line in secs:
        if sum(pr in seen for pr in line) * 2 > len(line):
            continue
        seen.update(line)
        runs = [list(range(len(line)))]
        if states is not None:
            runs, cur = [], []
            for i, pr in enumerate(line):
                # a province of the line faces the enemy over land: the game's section can
                # run on along a border with our own subject (23.10: the Tumen section went
                # on down the Yalu, Korea against Manchukuo, and the Soviet theatre held it)
                if mv.state_of.get(pr) in states and any(
                        b in mv.land and held_(b) in far_ for b in mv.adj.get(pr, ())):
                    cur.append(i)
                elif cur:
                    runs.append(cur)
                    cur = []
            if cur:
                runs.append(cur)
        cand.extend((s, line, idx) for idx in runs)
    # a province already on a longer piece is not counted twice (23.10: the China line read
    # Chahar 3, Jehol 2, East Hebei 3, Jehol 2, Chahar 3: one-province runs of other
    # sections repeated the long one, and the cut gave the same province twice)
    cand.sort(key=lambda x: -len(x[2]))
    for s, line, idx in cand:
        cur = []
        for i in idx + [None]:
            if i is not None and line[i] not in taken:
                cur.append(i)
                continue
            if cur:
                pieces.append({"front": s["front"], "fid": s["front_handle"][1],
                               "sec": s["section"], "line": line, "idx": cur})
            cur = []
        taken.update(line[i] for i in idx)
    if not pieces:
        return []
    pieces.sort(key=lambda pc: -len(pc["idx"]))
    if states is None and len(tags_of(enemy_tag)) > 1:
        # several countries: only the pieces joined over land to the longest one (19.10:
        # HOL in also_facing put French Guiana on the 1st Army Group's line)
        # over our own land: Europe and Africa join by land through the Levant, and the
        # Alpine group's line (SWI+ITA) ran on to Gabès and Somaliland (19.10)
        own, held = g.player_tag(), _ctl(g)
        ours = {own} | set(B.module("combat").subject_tags(g))
        dist = _bfs(mv, [pieces[0]["line"][i] for i in pieces[0]["idx"]],
                    lambda p_: p_ in mv.land and held(p_) in ours)
        pieces = [pc for pc in pieces if any(pc["line"][i] in dist for i in pc["idx"])]

    def end(pc, last):
        return pc["line"][pc["idx"][-1 if last else 0]]
    # the first piece faces the way that lets the next one join it (19.10: the Belgian
    # piece ran from Lorraine to the sea and the German one could not hook on, so the line
    # read Nord-Pas-de-Calais, then Lorraine again)
    first = pieces[0]

    def joins(tail):
        return any(_steps(mv, tail, end(pc, e)) is not None for pc in pieces[1:] for e in (0, 1))
    if len(pieces) > 1 and not joins(end(first, True)) and joins(end(first, False)):
        first["idx"] = first["idx"][::-1]
    order, rest = [first], pieces[1:]
    while rest:
        tail = end(order[-1], True)
        best = None
        for pc in rest:
            for flip in (False, True):
                d = _steps(mv, tail, end(pc, flip))
                if d is not None and (best is None or d < best[0]):
                    best = (d, pc, flip)
        if best is None:                       # a separate stretch: the longest goes next
            pc, flip = rest[0], False
        else:
            _, pc, flip = best
        if flip:
            pc["idx"] = pc["idx"][::-1]
        order.append(pc)
        rest.remove(pc)
    return order


LINE_SKIP = set()           # neighbours outside our war, set by the runner each picture
LINE_KEEP = set()           # the countries of our war, at war or coming: never a quiet wall


ALLY_STEPS = 3     # allied states this many states from a theatre's own join its line
ALLY_KEEP_DAYS = 7  # an allied state taken on stays this long (no line that flickers)
_ALLY_SEEN = {}     # state -> day it was last taken on
_ALLY_CACHE = {}    # (hour, region) -> the result


def _supply_room(g, provs):
    """Supply that reaches these provinces beyond what the troops there use, per day, and
    what one of our divisions uses (the game's CSupplySystem records, see front.py)."""
    from . import front
    p = g.p
    try:
        ss = p.u64(p.u64(p.va(front.G_GAME)) + front.GAME_SUPPLY)
        arr = p.u64(ss + front.SS_PROVINCES)
    except Exception:
        arr = 0
    if not arr:
        return None
    room = 0.0
    for pr in provs:
        rec = arr + int(pr) * front.SS_PROV_SIZE
        room += max(0.0, p.i64(rec + 8) / 1e5 - p.i64(rec) / 1e5)
    return room


def _per_division(g, ours_at):
    """Supply one of our divisions uses a day, from the provinces they stand in."""
    from . import front
    p = g.p
    try:
        ss = p.u64(p.u64(p.va(front.G_GAME)) + front.GAME_SUPPLY)
        arr = p.u64(ss + front.SS_PROVINCES)
    except Exception:
        return 0.6
    dem = n = 0
    for pr, k in ours_at.items():
        d = p.i64(arr + int(pr) * front.SS_PROV_SIZE) / 1e5
        if d > 0:
            dem, n = dem + d, n + k
    return dem / n if n else 0.6


def allied_land(g, mv, holders, region):
    """The states of faction allies next to a theatre (up to ALLY_STEPS states out through
    their land) whose front against our enemy the theatre holds with them. We fight as an
    alliance and attack from an ally's land too (the user, 27.10): an ally's front joins
    the line unless it is no use to us there. Per ally:
    - its supply is full (no room for one more division of ours): bringing ours only
      starves both;
    - it holds alone (as many divisions there as the enemy facing it) and we have no
      division of ours free near it: let it hold.
    (27.10: Ethiopia's liberated states sat between East Africa and the Italians and the
    theatre held none of them; 5 Ethiopian militia faced the Italian army there.)"""
    own = g.player_tag()
    try:
        friends = {t for t in g.faction_members(own)["members"] if isinstance(t, str)} - {own}
    except Exception:
        return set()
    if not friends:
        return set()
    region = {int(x) for x in region}
    try:
        hour = g.date_raw()
    except Exception:
        hour = None
    key = (hour, frozenset(region))
    if hour is not None and key in _ALLY_CACHE:
        return set(_ALLY_CACHE[key])
    ctl = lambda x: holders.get(x, (None, None))[1]
    found, todo = collections.defaultdict(set), [(x, 0) for x in region]
    seen = set(region)
    while todo:
        x, d = todo.pop()
        if d >= ALLY_STEPS:
            continue
        for n in mv.neighbours.get(x, ()):
            if n not in seen and ctl(n) in friends:
                seen.add(n)
                found[ctl(n)].add(n)
                todo.append((n, d + 1))
    if not found:
        return set()
    enemies = set(B.module("combat").enemy_tags(g))
    his_at = B.module("unitorders").enemy_presence(g, sorted(enemies)) if enemies else {}
    # our divisions near: in the theatre's states or the allies' found; free = on no sector
    near = region | set().union(*found.values())
    on_sector = {d for og in armies(g).values() for o in _fronts(g, og) for d in _divs(g, o)}
    ours_at, free = collections.Counter(), 0
    try:
        for dv in g.divisions(own, limit=3000)["divisions"]:
            pr = province_of_division(g, dv)
            ours_at[pr] += 1
            if mv.state_of.get(pr) in near and int(dv["ptr"], 16) not in on_sector:
                free += 1
    except Exception:
        pass
    per_div = _per_division(g, ours_at)
    day = (hour or 0) // 24
    out = set()
    for tag, states in found.items():
        front = {pr for s in states for pr in mv.land_provinces(s)
                 if any(b in mv.land and ctl(mv.state_of.get(b)) in enemies
                        for b in mv.adj.get(pr, ()))}
        if not front:
            continue
        his = sum(his_at.get(b, {}).get("divisions", 0) for pr in front
                  for b in mv.adj.get(pr, ()) if ctl(mv.state_of.get(b)) in enemies)
        try:
            divs = g.divisions(tag, limit=3000)["divisions"]
        except Exception:
            divs = []
        here = sum(1 for dv in divs if mv.state_of.get(province_of_division(g, dv)) in states)
        room = _supply_room(g, front)
        fed = room / per_div if room is not None and per_div > 0 else 99
        if fed < 1:
            continue                    # no room for one division of ours
        alone = here >= his and here > 0
        kept = {s for s in states if day - _ALLY_SEEN.get(s, -99) <= ALLY_KEEP_DAYS}
        if alone and not free and not kept:
            continue
        for s in states:
            _ALLY_SEEN[s] = day
        out |= states
    if hour is not None:
        if any(k[0] != hour for k in _ALLY_CACHE):
            _ALLY_CACHE.clear()
        _ALLY_CACHE[key] = set(out)
    return out


def province_of_division(g, dv):
    from .navy import _prov_id
    try:
        return _prov_id(g, int(dv["province_ptr"], 16))
    except Exception:
        return None


def theatre_line(g, mv, holders, region):
    """A theatre's line: our side of the fronts with every country its states touch over
    land, inside its states and the minor allies' land next to them, in order along it."""
    region = set(region) | allied_land(g, mv, holders, region)
    tags = land_borders(g, mv, holders, region)
    if not tags:
        return []
    # while a war is on or coming, a theatre faces its countries, not every neutral it
    # touches (23.10: the China Front's 22 divisions were spread along Mengjiang)
    keep = set(tags) - LINE_SKIP
    if keep:
        tags = keep
    return line_pieces(g, mv, "+".join(sorted(tags)), set(region))


def describe_line(mv, pieces):
    """'state (n provinces), ...' along the line, the way a field marshal reads it."""
    out = []
    for pc in pieces:
        for i in pc["idx"]:
            s = mv.state_of.get(pc["line"][i])
            if out and out[-1][0] == s:
                out[-1][1] += 1
            else:
                out.append([s, 1])
    return ", ".join("%s %d" % (mv.label(s), n) for s, n in out)


MIN_STRETCH = 2
SHALLOW = 2                  # an attack this many provinces deep or less is shallow


def cut_front(g, mv, holders, theatre_id, region, plan, drop_attacks=True):
    # Validate before even pausing: malformed model input must change no game state.
    plan = validate_sector_plan(plan)
    return _cut_front(g, mv, holders, theatre_id, region, plan, drop_attacks)


def validate_sector_plan(plan):
    """Unique armies and finite positive weights, before native order access."""
    if not plan:
        raise OpError("list at least one army with a positive sector weight")
    result, seen = [], set()
    for army_id, weight in plan:
        if isinstance(army_id, bool) or not (
                isinstance(army_id, int) or isinstance(army_id, str) and army_id.isdecimal()):
            raise OpError("army ids must be positive integers")
        aid, weight = int(army_id), float(weight)
        if aid in seen:
            raise OpError("duplicate army %d: list each army once; combine its states in one row" % aid)
        if aid <= 0 or not math.isfinite(weight) or weight <= 0:
            raise OpError("army ids must be positive and sector weights finite and positive")
        seen.add(aid)
        result.append((aid, weight))
    return result


@still
def _cut_front(g, mv, holders, theatre_id, region, plan, drop_attacks=True):
    """Cut the theatre's line among its armies by province: plan [(army id, weight)] in order
    along the line from its first end. Every province of the line goes to exactly one
    army, in contiguous stretches as long as their weights; armies left out stand in
    reserve. An army whose stretch did not change keeps its sector, its attack and the
    game's planning. Returns {army id: {stretch, provinces, unchanged?}}."""
    ao, p = _armyops(), g.p
    mem = {_hid(g, m): m for m in theatre_armies(g, theatre_id)}
    bad = [a for a, _ in plan if a not in mem]
    if bad:
        raise OpError("army %s is not in this theatre (its armies: %s)" % (bad, sorted(mem)))
    pieces = theatre_line(g, mv, holders, region)
    fwd = forward_of_defence(g, mv, theatre_id)
    flat = [(n, i) for n, pc in enumerate(pieces) for i in pc["idx"]
            if pc["line"][i] not in fwd]
    if not flat and fwd:
        raise OpError("the whole border lies in front of your defence lines: the armies "
                      "hold those (hold_line), none is left for the border")
    if not flat:
        raise OpError("the theatre has no line now: its states (%s) touch no foreign land"
                      % ", ".join(map(mv.label, sorted(region))))
    # the game's front sections come and go while it rebuilds a front; a line cut then
    # misses whole stretches (i52, 01.12.1939: the line read 6 provinces without
    # Alsace-Lorraine, and army 644 was cut onto Picardy and left the Maginot line)
    on_line = {pc["line"][i] for pc in pieces for i in pc["idx"]}
    held, at_war = _ctl(g), set(B.module("combat").enemy_tags(g))
    contact = {q for st in region for q in mv.land_provinces(int(st))
               if held(q) == g.player_tag() and any(
                   b in mv.land and held(b) in at_war for b in mv.adj.get(q, ()))}
    missing = contact - on_line - fwd
    if len(missing) >= 3 and len(missing) * 4 > len(contact):
        raise OpError("the game's front is being rebuilt: YOUR LINE misses %d of our "
                      "provinces facing the enemy (%s). Nothing was cut; the sectors stand. "
                      "Cut next turn." % (len(missing), ", ".join(sorted(
                          {mv.label(mv.state_of.get(q)) for q in missing}))))
    plan = [(a, float(w)) for a, w in plan if float(w) > 0]
    # at least MIN_STRETCH provinces an army: twice the game crashed (SIGSEGV at
    # 0x1A24D08) right after armies were given one-province stretches (three armies on
    # Moselland's four, two on the Ardennes' two); the heaviest armies stay on the line
    # (the fourth crash came with 3-province stretches: the cause is a cut drawn while
    # the front moved, see still(); two provinces stay as a sane sector size: at three,
    # Bock re-cut eight times in one turn, 12.01.1940, to fit a fourth army)
    room = max(1, len(flat) // MIN_STRETCH)
    benched = []
    if len(plan) > room:
        keep = {a for a, _ in sorted(plan, key=lambda x: -x[1])[:room]}
        benched = [a for a, _ in plan if a not in keep]
        plan = [(a, w) for a, w in plan if a in keep]
    total = sum(w for _, w in plan)
    want, cum = {}, 0.0
    for a, w in plan:
        lo = round(len(flat) * cum / total)
        cum += w
        hi = round(len(flat) * cum / total)
        if hi <= lo:
            hi = min(lo + 1, len(flat))
        bits = collections.defaultdict(list)
        for n, i in flat[lo:hi]:
            bits[n].append(i)
        want[a] = []
        for n, ix in bits.items():
            pc = pieces[n]
            k = len(pc["line"])
            want[a].append((pc["fid"], pc["sec"], min(ix) * FULL // k, (max(ix) + 1) * FULL // k,
                            pc["front"]))
    out, fronts = {}, _front_index(g)
    if not drop_attacks:
        # a new stretch deletes the army's arrows and the planning they built (23.10: on
        # the first day of the China war Kan'in re-cut and lost a Beiping arrow a year old)
        lose = []
        for a, m in mem.items():
            fr_ = _fronts(g, m)
            if not any(B.arr(p, o, OI_CHILDREN) for o in fr_):
                continue
            have = sorted((p.i32(o + OI_FRONT), p.i32(o + OI_SECTION), p.i32(o + OI_FROM),
                           p.i32(o + OI_TO)) for o in fr_)
            new = sorted(x[:4] for x in want.get(a, []))
            if not (len(have) == len(new) and all(
                    h[:2] == n[:2] and abs(h[2] - n[2]) <= FULL // 40
                    and abs(h[3] - n[3]) <= FULL // 40 for h, n in zip(have, new))):
                lose.append(a)
        if lose:
            raise OpError("this cut moves the stretch of army %s, which has attacks drawn: "
                          "the new stretch deletes them (they are drawn again on it) and the "
                          "planning bonus they built is lost. Nothing was cut. Keep those armies' stretches as they are, or call "
                          "set_sectors again with drop_attacks true if losing them is worth it"
                          % ", ".join(map(str, lose)))
    # the arrows of every army, to draw again on its new stretch (25.10, n3d: the theatre
    # cuts on define_theatre and the automatic re-cut of a bare line wiped the save's
    # arrows, nobody drew them again, and the China armies stood for two months)
    arrows = {a: _arrows_of(g, mv, m) for a, m in mem.items()}
    for a, m in mem.items():
        have = sorted((p.i32(o + OI_FRONT), p.i32(o + OI_SECTION), p.i32(o + OI_FROM),
                       p.i32(o + OI_TO)) for o in _fronts(g, m))
        new = sorted(x[:4] for x in want.get(a, []))
        same = len(have) == len(new) and all(
            h[:2] == n[:2] and abs(h[2] - n[2]) <= FULL // 40 and abs(h[3] - n[3]) <= FULL // 40
            for h, n in zip(have, new))
        if same:
            if a in want:
                provs = [pr for o in _fronts(g, m) for pr in _sector_line(g, fronts, o)]
                out[a] = {"unchanged": True,
                          "states": sorted({mv.state_of[pr] for pr in provs if pr in mv.state_of}),
                          "provinces": len(provs),
                          "assigned": _assign_nearest(g, mv, holders, m)}
            continue
        for o in list(_fronts(g, m)):
            ao.delete_order(g, m, p.i32(o + OI_ID))
        time.sleep(SETTLE)
        for fid, sec, frm, to, fptr in want.get(a, []):
            ok, err = ao.draw_front_line(g, m, fptr, sec, frm, to)
            if not ok:
                raise OpError("the game refused army %d's stretch: %s" % (a, err))
        if a in want:
            time.sleep(SETTLE)
            assigned = _assign_nearest(g, mv, holders, m)
            provs = [pr for o in _fronts(g, m) for pr in _sector_line(g, fronts, o)]
            sts = list(dict.fromkeys(mv.state_of.get(pr) for pr in provs))
            out[a] = {"stretch": ", ".join(mv.label(s) for s in sts if s),
                      "states": [s for s in sts if s is not None],
                      "provinces": len(provs), "assigned": assigned}
            if arrows.get(a):
                out[a]["attack"] = _redraw(g, mv, holders, a, arrows[a])
        else:
            out[a] = {"stretch": None, "note": "in reserve"}
            if arrows.get(a):
                out[a]["attack"] = "lost: the army went to reserve (its arrow to %s)" % (
                    mv.label(arrows[a][0][-1]))
    for a in benched:
        out[a] = {"stretch": None, "note": "in reserve: the line has %d provinces, room for %d "
                  "armies of at least %d" % (len(flat), room, MIN_STRETCH)}
    return out


def _arrow_state_ids(g, mv, arrow):
    st = []
    for pr in _arrow_path(g, arrow):
        sid = mv.state_of.get(pr)
        if sid is not None and (not st or st[-1] != sid):
            st.append(sid)
    return st


def _arrows_of(g, mv, og):
    """The army's first attack as (route states, then states), or None."""
    p = g.p
    for o in _fronts(g, og):
        for c in B.arr(p, o, OI_CHILDREN):
            route = _arrow_state_ids(g, mv, c)
            if not route:
                continue
            then, x = [], c
            while B.arr(p, x, OI_CHILDREN):
                x = B.arr(p, x, OI_CHILDREN)[0]
                end = (_arrow_state_ids(g, mv, x) or [None])[-1]
                if end is not None and end not in route[-1:] + then[-1:]:
                    then.append(end)
            return route, then
    return None


def _redraw(g, mv, holders, army_id, arrow):
    """Draw an army's attack again after its stretch changed."""
    route, then = arrow
    try:
        attack(g, mv, holders, army_id, route[-1], via_states=route[1:-1], then_states=then)
        return "redrawn to %s" % mv.label(route[-1])
    except Exception as e:
        return "lost (%s): its arrow to %s; draw it again" % (e, mv.label(route[-1]))


def default_plan(g, mv, pieces, theatre_id):
    """The theatre's armies in the order they stand along its line, each weighted by its
    divisions: what the game does when a front is drawn for an army group."""
    flat = [pc["line"][i] for pc in pieces for i in pc["idx"]]
    near = _bfs(mv, flat[:1]) if flat else {}
    rows = []
    for m in theatre_armies(g, theatre_id):
        ds = division_bases(g, m)
        if not ds:
            continue
        pos = [near.get(province_of(g, d), 10 ** 6) for d in ds]
        rows.append((sum(pos) / len(pos), _hid(g, m), len(ds)))
    return [(aid, n) for _, aid, n in sorted(rows)]


# theatre id -> {army id: [provinces of its defence line]} (run.py keeps it from the
# theatres; a defence line is the marshal's choice to stand behind the border)
DEFENCE = {}
_COVERED = {}          # theatre id -> provinces on its armies' sectors, last day


def _edge(g, mv, provs):
    """The provinces of `provs` that face the enemy side: next to a land province outside
    the set that lies nearer to foreign-held land. In order along the line."""
    own, held = g.player_tag(), _ctl(g)
    foreign = [x for x in mv.land if held(x) != own]
    dist = dict.fromkeys(foreign, 0)
    q = collections.deque(foreign)
    while q:
        a = q.popleft()
        for b in mv.adj.get(a, ()):
            if b in mv.land and b not in dist:
                dist[b] = dist[a] + 1
                q.append(b)
    big = 10 ** 6
    edge = {x for x in provs if any(b in mv.land and b not in provs and
                                    dist.get(b, big) < dist.get(x, big)
                                    for b in mv.adj.get(x, ()))}
    if not edge:
        return []
    # walk it: from an end (fewest neighbours on the edge), always to the nearest unvisited
    nb = {x: [b for b in mv.adj.get(x, ()) if b in edge] for x in edge}
    start = min(edge, key=lambda x: (len(nb[x]), x))
    order, left = [start], set(edge) - {start}
    while left:
        d = {order[-1]: 0}
        q, hit = collections.deque([order[-1]]), None
        while q and hit is None:
            a = q.popleft()
            for b in mv.adj.get(a, ()):
                if b in provs and b not in d:
                    d[b] = d[a] + 1
                    if b in left:
                        hit = b
                        break
                    q.append(b)
        hit = hit if hit is not None else min(left)
        order.append(hit)
        left.discard(hit)
    return order


def forward_of_defence(g, mv, theatre_id):
    """Our provinces in front of the theatre's defence lines (between them and the enemy):
    given up by choice, not holes."""
    lines = {pr for provs in DEFENCE.get(theatre_id, {}).values() for pr in provs}
    if not lines:
        return set()
    own, held = g.player_tag(), _ctl(g)
    start = [x for x in mv.land if held(x) == own and x not in lines and
             any(b in mv.land and held(b) != own for b in mv.adj.get(x, ()))]
    seen, q = set(start), collections.deque(start)
    while q:
        a = q.popleft()
        for b in mv.adj.get(a, ()):
            if b in mv.land and b not in seen and b not in lines and held(b) == own:
                seen.add(b)
                q.append(b)
    return seen


def _spread(g, mv, holders, m, targets):
    """An army's front orders go and its divisions march to the target provinces, spread
    evenly, each to the nearest with room; with no order they hold where they stand.
    (The game's fallback-line command, COrderNewFallbackCommand, is accepted on 1.19.3
    but makes no order; with the divisions passed it crashed the call, 21.10.)"""
    ao, p = _armyops(), g.p
    divs = division_bases(g, m)
    if not divs:
        raise OpError("the army has no divisions")
    for o in list(_fronts(g, m)):
        ao.delete_order(g, m, p.i32(o + OI_ID))
    time.sleep(SETTLE)
    uo = B.module("unitorders")
    room = -(-len(divs) // len(targets))
    load = dict.fromkeys(targets, 0)
    passable = _passable(g, mv, holders)
    dist = {pr: _bfs(mv, [pr], passable) for pr in targets}
    moved, failed = 0, []
    for d in divs:
        at = province_of(g, d)
        free = [pr for pr in targets if load[pr] < room]
        to = min(free, key=lambda pr: dist[pr].get(at, 10 ** 6))
        load[to] += 1
        if at == to:
            continue
        ok, err = uo.move(g, d, to)
        if ok:
            moved += 1
        else:
            failed.append(err)
    if failed and not moved:
        raise OpError("the divisions would not move: %s" % failed[0])
    return {"divisions": len(divs), "marching": moved}


def _hq_status(g, m):
    try:
        from . import hq
        return hq.status(g, m)
    except Exception:
        return None


def deploy_hq(g, theatre_id, army_id, withdraw=False):
    """Deploy (or withdraw) the army's general with an army HQ."""
    from . import hq
    m = _army_in(g, theatre_id, army_id)
    ok, why = (hq.withdraw if withdraw else hq.deploy)(g, m)
    if not ok:
        raise OpError(why)
    return {"army": int(army_id), "hq": "withdrawing" if withdraw else
            "deploying (1-3 days; it draws some equipment and manpower from the army)"}


def _army_in(g, theatre_id, army_id):
    m = {_hid(g, x): x for x in theatre_armies(g, theatre_id)}.get(int(army_id))
    if m is None:
        raise OpError("army %s is not in this theatre" % army_id)
    return m


@still
def hold_line(g, mv, holders, theatre_id, army_id, state_ids=(), provinces=()):
    """A defence line for an army: the provinces given, in order along it (an arrow drawn
    province by province), or the side of the given states of ours that faces the
    enemy. The army leaves its stretch of the border and holds the line. Returns the
    line's provinces in order."""
    m = _army_in(g, theatre_id, army_id)
    own, held = g.player_tag(), _ctl(g)
    if provinces:
        line = [int(x) for x in provinces if int(x) in mv.land]
        bad = [x for x in line if held(x) != own]
        if bad:
            raise OpError("not held by us: %s" % ", ".join("p%d" % x for x in bad))
    else:
        provs = {x for s in state_ids for x in mv.land_provinces(int(s)) if held(x) == own}
        if not provs:
            raise OpError("none of these states is held by us: %s" % list(state_ids))
        line = _edge(g, mv, provs)
    if not line:
        raise OpError("no line: give provinces along it, or states with a side facing "
                      "the enemy")
    _spread(g, mv, holders, m, line)
    return line


POINT_KINDS = ("ports", "cities", "forts", "coast", "airfields")


@still
def guard_points(g, mv, holders, theatre_id, army_id, state_ids, kinds=(), provinces=()):
    """An army garrisons chosen points of the given states of ours instead of a line:
    ports, cities (victory points), forts, the coast, airfields (each state's main city
    stands for its air base), or the provinces given. Against landings: garrisons in
    the ports. Returns the points."""
    m = _army_in(g, theatre_id, army_id)
    own, held = g.player_tag(), _ctl(g)
    pts = [int(x) for x in provinces if int(x) in mv.land and held(int(x)) == own]
    for s in state_ids:
        prs = [x for x in mv.land_provinces(int(s)) if held(x) == own]
        for k in kinds:
            if k == "ports":
                pts += [x for x in prs if x in mv.ports]
            elif k == "cities":
                pts += [x for x in prs if x in mv.city_at]
            elif k == "forts":
                pts += [x for x in prs if mv.forts.get(x)]
            elif k == "coast":
                pts += [x for x in prs if x in mv.coast]
            elif k == "airfields":
                best = max((x for x in prs if x in mv.city_at),
                           key=lambda x: mv.city_at[x][1], default=None)
                if best is not None and mv.buildings.get(int(s), {}).get("air_base"):
                    pts.append(best)
    pts = list(dict.fromkeys(pts))
    if not pts:
        raise OpError("no such points of ours in these states (kinds: %s)"
                      % ", ".join(POINT_KINDS))
    _spread(g, mv, holders, m, pts)
    return pts


def drop_defence(g, theatre_id, army_id, order_id):
    """An army's defence line is only its divisions' places: a stretch of the border
    (cut_front) moves them on. Nothing to take away in the game."""
    return None


def keep_line(g, mv, holders, theatre_id, region):
    """Re-cut the theatre's line when part of it stands on no army's sector, with the
    armies that hold it, in the order they stand. A sector is drawn on the game's front
    with one country; when that country loses land (21.10: Germany into Belgium) its front
    shrinks, the army marches after it, and our border there goes over to the front with
    the conqueror, which no army holds. Returns None when the line is covered."""
    pieces = theatre_line(g, mv, holders, region)
    line = {pc["line"][i] for pc in pieces for i in pc["idx"]}
    if not line:
        return None
    fronts, covered, holding = _front_index(g), set(), set()
    for m in theatre_armies(g, theatre_id):
        provs = [pr for o in _fronts(g, m) for pr in _sector_line(g, fronts, o)]
        if provs:
            holding.add(_hid(g, m))
            covered.update(provs)
    before = _COVERED.get(theatre_id, set())
    _COVERED[theatre_id] = covered
    # only what a sector held before and lost (a front moved under it); a stretch the
    # marshal gave up for a defence line is his choice
    bare = (line - covered) & before
    bare -= forward_of_defence(g, mv, theatre_id)
    if not bare or not holding:
        return None
    plan = [(a, n) for a, n in default_plan(g, mv, pieces, theatre_id) if a in holding]
    if not plan:
        return None
    cut = cut_front(g, mv, holders, theatre_id, region, plan)
    return {"bare": sorted({mv.label(mv.state_of.get(pr)) for pr in bare}), "cut": cut}


COVER_REACH = 12          # provinces: a stretch farther from every free army is the marshal's


def cover_bare(g, mv, holders, theatre_id, region, dry=False):
    """The stretches of the line facing an enemy at war that no army's sector holds, each
    given to ONE army, the nearest that is not attacking, as an extra sector; every other
    sector, arrow and its planning stays as it is. (27.10, the user: liberated Ethiopia
    tore the East Africa line, and keep_line saw only what a sector had held before; a
    re-cut of the whole line would also redraw the attacks every day of an advance.)
    Returns None, or {"bare": [states], "given": {army: states}, "left": [states]}."""
    p = g.p
    pieces = theatre_line(g, mv, holders, region)
    if not pieces:
        return None
    fronts = _front_index(g)
    held, at_war = _ctl(g), set(B.module("combat").enemy_tags(g))
    covered, armies = set(), []
    for m in theatre_armies(g, theatre_id):
        fr_ = _fronts(g, m)
        covered.update(pr for o in fr_ for pr in _sector_line(g, fronts, o))
        attacking = any(B.arr(p, o, OI_CHILDREN) for o in fr_)
        ds = division_bases(g, m)
        if ds:
            armies.append((attacking, m, [province_of(g, d) for d in ds]))
    if not armies:
        return None
    fwd = forward_of_defence(g, mv, theatre_id)
    on_line = {pc["line"][i] for pc in pieces for i in pc["idx"]}
    contact = {q for st in region for q in mv.land_provinces(int(st))
               if held(q) == g.player_tag() and any(
                   b in mv.land and held(b) in at_war for b in mv.adj.get(q, ()))}
    missing = contact - on_line - fwd
    if len(missing) >= 3 and len(missing) * 4 > len(contact):
        return None                     # the game is rebuilding its front: not today

    def bare(pr):
        return (pr not in covered and pr not in fwd and any(
            b in mv.land and held(b) in at_war for b in mv.adj.get(pr, ())))
    runs = []                           # (piece, [indices]) contiguous along the piece
    for pc in pieces:
        idx = sorted(pc["idx"])
        cur = []
        for i in idx:
            if bare(pc["line"][i]) and (not cur or i == cur[-1] + 1):
                cur.append(i)
                continue
            if cur:
                runs.append((pc, cur))
            cur = [i] if bare(pc["line"][i]) else []
        if cur:
            runs.append((pc, cur))
    if not runs:
        return None
    passable = _passable(g, mv, holders)
    out = {"bare": [], "given": collections.defaultdict(list), "left": []}
    for pc, ix in runs:
        sts = sorted({mv.label(mv.state_of.get(pc["line"][i])) for i in ix})
        out["bare"] += sts
        k = len(pc["line"])
        lo, hi = min(ix), max(ix)
        while hi - lo + 1 < MIN_STRETCH and (lo > 0 or hi < k - 1):
            if hi < k - 1:
                hi += 1
            else:
                lo -= 1
        dist = _bfs(mv, [pc["line"][i] for i in range(lo, hi + 1) if passable(pc["line"][i])],
                    passable)
        best = None
        for attacking, m, provs in armies:
            near = min((dist[x] for x in provs if x in dist), default=None)
            if near is None or near > COVER_REACH or attacking:
                continue
            if best is None or near < best[0]:
                best = (near, m)
        if best is None:
            out["left"] += sts
            continue
        m = best[1]
        if dry:
            out["given"].setdefault(_hid(g, m), []).extend(sts + ["(%d provinces, %d away)"
                                                               % (hi - lo + 1, best[0])])
            continue
        ok, err = _armyops().draw_front_line(g, m, pc["front"], pc["sec"],
                                             lo * FULL // k, (hi + 1) * FULL // k)
        if not ok:
            out["left"] += sts
            continue
        time.sleep(SETTLE)
        _assign_nearest(g, mv, holders, m)
        out["given"][_hid(g, m)] += sts
    out["given"] = dict(out["given"])
    return out


def deal_front(g, mv, holders, theatre_id, region):
    """Cut the theatre's line among its armies, in the order they stand along it, each by
    its share of the divisions (cut_front). Returns {army id: its stretch (state labels),
    or [] in reserve}."""
    for og in theatre_members(g, theatre_id):
        if is_group(g, og):
            for o in list(_orders(g, og)):        # a group's own sectors, if any
                _armyops().delete_order(g, og, g.p.i32(o + OI_ID))
    pieces = theatre_line(g, mv, holders, region)
    plan = default_plan(g, mv, pieces, theatre_id)
    if not plan:
        raise OpError("the theatre has no divisions")
    out = cut_front(g, mv, holders, theatre_id, region, plan)
    return {aid: (r.get("stretch") or ("unchanged" if r.get("unchanged") else []))
            for aid, r in out.items()}


def _reach(g, mv, line, enemies, held, per_state, limit=10):
    """The enemy states an attack from this line can reach (through enemy land, as the
    attack's route goes), nearest first, then the biggest cities beyond: short lines for a
    briefing."""
    dist, q = {}, collections.deque()
    for a in line:
        for b in mv.adj.get(a, ()):
            if b in mv.land and b not in dist and held(b) in enemies:
                dist[b] = 1
                q.append(b)
    while q:
        a = q.popleft()
        for b in mv.adj.get(a, ()):
            if b in mv.land and b not in dist and held(b) in enemies:
                dist[b] = dist[a] + 1
                q.append(b)
    best = {}
    for pr, d in dist.items():
        s = mv.state_of.get(pr)
        if s is not None and d < best.get(s, 10 ** 6):
            best[s] = d
    order = sorted(best, key=lambda s: best[s])
    pick = order[:6]
    pick += sorted(order[6:], key=lambda s: -mv.vp.get(s, 0))[:limit - len(pick)]
    rows = []
    for s in sorted(pick, key=lambda s: best[s]):
        ground = "/".join(mv.ground(s)) or "?"
        txt = "%s %dp %s" % (mv.label(s), best[s], ground)
        if per_state.get(s):
            txt += " %d div" % per_state[s]
        if mv.cities.get(s):
            txt += " city %s(%d)" % mv.cities[s][0]
        rows.append(txt)
    return rows


def command_view(g, mv, holders, theatre_id, enemies, targets=False, against=None):
    """A theatre as its field marshal commands it: per army, its army group, divisions,
    sector, whether they stand on it, its attack, and (targets) where it can strike
    (in the land of `against`, by default `enemies`)."""
    p = g.p
    ours = set().union(*[front_line_states(g, mv, t) for t in enemies])
    enemies = set(against or enemies)
    fr = _front_index(g)
    held = _ctl(g)
    per_state = collections.Counter()
    if targets:
        for pid, e in B.module("unitorders").enemy_presence(g, sorted(enemies)).items():
            per_state[mv.state_of.get(pid)] += e["divisions"]
    rows = []
    for m in theatre_armies(g, theatre_id):
        ds = division_bases(g, m)
        line, on, attacks = [], set(), []
        for o in _fronts(g, m):
            line += _sector_line(g, fr, o)
            on.update(_divs(g, o))
            for c in B.arr(p, o, OI_CHILDREN):
                route = _route_states(g, mv, c)
                then, x = [], c
                while B.arr(p, x, OI_CHILDREN):          # chained arrows from its tip
                    x = B.arr(p, x, OI_CHILDREN)[0]
                    end = (_route_states(g, mv, x) or ["?"])[-1]
                    if end not in route[-1:] + then[-1:]:   # not a piece of the same arrow
                        then.append(end)
                attacks.append({"order": p.i32(c + OI_ID), "route": route,
                                "then": then, "divisions": len(B.arr(p, c, 0x210)),
                                "executing": bool(p.u8(c + OI_EXEC))})
        parent = p.u64(m + OG_PARENT)
        row = {"army": _hid(g, m), "general": leader_name(g, m), "divisions": len(ds),
               "army_group": _hid(g, parent) if parent else None,
               "mix": division_mix(g, ds),
               "org": round(sum(p.i64(d + A_ORG) / 1e5 for d in ds) / len(ds), 1) if ds else 0,
               "hp": round(sum(p.i64(d + A_STR) / 1e5 for d in ds) / len(ds), 1) if ds else 0,
               "sector": [mv.label(s) for s in _in_line_order(mv, line, ours)],
               "on_sector": len(on), "attacks": attacks,
               "plan": plan_eval_text(plan_eval(g, m)) if (line or attacks) else None,
               "hq": _hq_status(g, m)}
        if line:
            row.update(_deployment(g, mv, holders, set(line), list(on)))
            off = [d for d in ds if d not in on]
            if off:
                reach = _march(g, mv, holders, set(line))
                cut = collections.Counter(mv.label(mv.state_of.get(province_of(g, d)))
                                          for d in off if province_of(g, d) not in reach)
                if cut:
                    row["off_sector_overseas"] = cut.most_common(6)
            if targets:
                row["targets"] = _reach(g, mv, line, enemies, held, per_state)
        rows.append(row)
    return {"id": theatre_id, "divisions": sum(r["divisions"] for r in rows), "armies": rows}


def _end_line(mv, held, enemies, starts, target, width):
    """The line an arrow ends on: `width` provinces through `target`, across the
    advance. The province list of an offensive line is the line the player paints at the
    arrow's head, where the army is to stand, not the way there (the user, 10.05.1940: one
    province drew an arrow to a point, and the army crawled out to it in a single file).
    Provinces as far from our line as the target (through enemy ground), grown from the
    target to both sides along that band, in order along the line."""
    import collections as _c
    ok = lambda q: q in mv.land and (held(q) in enemies or q == target)
    dist = {s: 0 for s in starts}
    todo = _c.deque(starts)
    while todo:
        q = todo.popleft()
        for n in mv.adj.get(q, ()):
            if n not in dist and ok(n):
                dist[n] = dist[q] + 1
                todo.append(n)
    if target not in dist:
        return [target]
    d = dist[target]
    chain = _c.deque([target])
    used = {target}
    for _ in range(width - 1):
        grown = False
        for end, put in ((chain[-1], chain.append), (chain[0], chain.appendleft)):
            if len(chain) >= width:
                break
            cands = [n for n in mv.adj.get(end, ()) if n in dist and n not in used]
            cands.sort(key=lambda n: (abs(dist[n] - d), n))
            if cands and abs(dist[cands[0]] - d) <= 1:
                used.add(cands[0])
                put(cands[0])
                grown = True
        if not grown:
            break
    return list(chain)


@still
def attack(g, mv, holders, army_id, objective_state, via_states=(), then_states=(),
           enemy_tag=None, blitz=False):
    """An army's attack: one arrow from its sector's line to the objective (the state's
    main city), along the cheapest route through the via states in order, and from its tip
    on to each of then_states. It replaces the army's attack. (set_offensive with every
    province of the targets drew snakes through six states, 17.10.)"""
    ao, p = _armyops(), g.p
    og = army(g, army_id)
    if is_group(g, og):
        raise OpError("%d is an army group; attacks are given to its armies" % army_id)
    # divisions on the sector's arrows count too: an army out on a spearhead has them on
    # the arrow, none on the sector (20.05.1940: Bock's pursuing armies were refused)
    secs = [o for o in _fronts(g, og) if B.arr(p, o, 0x210) or any(
        _order_divisions_deep(p, c) for c in B.arr(p, o, OI_CHILDREN))]
    if not secs:
        raise OpError("army %d has no sector with divisions on it; give it one first "
                      "(set_sectors)" % army_id)
    # the group's enemy counts as enemy ground before the war too: attacks are drawn
    # during deployment (17.10: no route at peace, no war without attacks, deadlock)
    enemies = set(B.module("combat").enemy_tags(g)) | set(tags_of(enemy_tag or ""))
    legs = [int(s) for s in via_states] + [int(objective_state)]
    for sid in legs + [int(s) for s in then_states]:
        if sid not in mv.provinces:
            raise OpError("no state #%s" % sid)
    fr = _front_index(g)
    held = _ctl(g)
    best = None
    for o in secs:
        path = None
        for sid in legs:
            leg = _route(g, mv, set(_sector_line(g, fr, o)) if path is None else {path[-1]},
                         _objective(mv, sid, held, enemies), enemies, path is not None)
            if leg is None:
                path = None
                break
            path = leg if path is None else path + leg[1:]
        if path and (best is None or len(path) < len(best[1])):
            best = (o, path)
    if not best:
        raise OpError("army %d cannot reach %s from its sector through enemy ground (behind "
                      "our line, cut off from it, or ours already); its TARGETS in the "
                      "briefing are the states it can reach" % (army_id, mv.label(legs[-1])))
    parent, path = best
    sector_id = p.i32(parent + OI_ID)
    crossings = sum(1 for a, b in zip(path, path[1:])
                    if mv.rivers.get((a, b)) or mv.rivers.get((b, a)))
    pres = B.module("unitorders").enemy_presence(g, sorted(enemies))
    forts = [(x, mv.forts[x]) for x in path[1:] if mv.forts.get(x)]
    plan = {"provinces": len(path) - 1, "river_crossings": crossings,
            "ground": dict(collections.Counter(mv.terrain.get(x, "?") for x in path[1:])
                           .most_common(3)),
            "enemy_divisions_on_the_route": sum(pres.get(x, {}).get("divisions", 0) for x in path)}
    key = "->".join(map(str, legs)) + ("|" + "->".join(map(str, then_states))
                                       if then_states else "") + ("|blitz" if blitz else "")
    kids = B.arr(p, parent, OI_CHILDREN)
    if kids and all(_TARGETS.get(p.i32(c + OI_ID)) == key for c in kids):
        return dict(plan, army=army_id, unchanged=True,
                    note="this army already attacks along exactly this route")
    for o in _fronts(g, og):                  # the army's attack, wherever it was drawn
        for c in B.arr(p, o, OI_CHILDREN):
            ao.delete_order(g, og, p.i32(c + OI_ID))
    time.sleep(SETTLE)
    before = {p.i32(c + OI_ID) for c in B.arr(p, parent, OI_CHILDREN)}
    # The province list is the line the player drags with the right mouse button: the
    # main city of each via-state, then the objective's (see docs/02-harness.md).
    line = _sector_line(g, fr, parent)
    width = max(3, min(8, len(line) + (2 if blitz else 0)))
    end = _end_line(mv, held, enemies, line, _objective(mv, legs[-1], held, enemies), width)
    ok, err = ao.offensive_line(g, og, sector_id, end, blitz=blitz)
    if not ok:
        raise OpError("offensive line refused: %s" % err)
    time.sleep(SETTLE)
    new = [c for c in B.arr(p, parent, OI_CHILDREN) if p.i32(c + OI_ID) not in before]
    if not new:
        raise OpError("the game drew no arrow from army %d's line to %s (it does so when the "
                      "line has no way there it accepts); try a nearer objective from its "
                      "TARGETS, and go deeper with then_states" % (army_id, mv.label(legs[-1])))
    _fill_offensives(g, og, parent)
    drawn = []
    for c in new:
        _TARGETS[p.i32(c + OI_ID)] = key
        drawn.append(_route_states(g, mv, c))
    chain = []
    for c in new[:1]:                          # one chain, from the first arrow
        tip = c
        for sid in then_states:
            # the game hangs a stub under an arrow, its path just the tip province; a
            # line drawn from the arrow itself draws nothing, from the stub it draws
            # (18.10: 0 of 25 chained arrows drawn in campaign f, every army stopped at
            # its first objective)
            while B.arr(p, tip, OI_CHILDREN):
                tip = B.arr(p, tip, OI_CHILDREN)[0]
            before2 = {p.i32(x + OI_ID) for x in B.arr(p, tip, OI_CHILDREN)}
            tgt = _objective(mv, int(sid), held, enemies)
            end = _end_line(mv, held, enemies, end, tgt, width)
            ok, err = ao.offensive_line(g, og, p.i32(tip + OI_ID), end, blitz=blitz)
            time.sleep(SETTLE)
            nxt = [x for x in B.arr(p, tip, OI_CHILDREN) if p.i32(x + OI_ID) not in before2]
            if not ok or not nxt:
                chain.append("%s: not drawn (%s)" % (mv.label(int(sid)),
                                                     err or "the game drew nothing from the tip"))
                break
            tip = nxt[0]
            chain.append(mv.label(int(sid)))
    out = dict(plan, army=army_id, arrows=len(new), route=drawn[0] if len(drawn) == 1 else drawn,
               end_line=", ".join(sorted({mv.label(mv.state_of[x]) for x in end
                                          if x in mv.state_of})) + " (%d provinces)" % len(end),
               divisions=sum(len(B.arr(p, c, 0x210)) for c in new))
    if forts:
        # 18.12: Heinrici attacked the Maginot line for weeks to "pin" the French
        worst = max(f for _, f in forts)
        out["FORTS_ON_THE_ROUTE"] = ("%s; level %d costs the attacker %d%% of his strength: "
                                     "an attack here bleeds, it does not pin" % (
                                         ", ".join("p%d level %d" % x for x in forts), worst,
                                         min(100, 15 * worst)))
    if chain:
        out["then"] = chain
    # an arrow ends at its objective and the army stops there (the user, 10.05.1940: arrows
    # at Paris with divisions long within reach of it; plan to Brittany and the Pyrenees)
    if len(path) - 1 <= SHALLOW and not then_states:
        out["SHALLOW"] = ("the objective is only %d provinces from the line: the arrow ends "
                          "there and the army stops when it is taken. Aim at the far end of "
                          "the country (pursue, or then_states); what lies on the way is "
                          "taken anyway" % (len(path) - 1))
    # an ordinary attack keeps the line: back to balanced after a pursuit
    pl = B.module("plans")
    if not blitz and pl.settings(g, og)["execution"] == "rush":
        out["execution"] = pl.set_mode(g, og, execution="balanced").get("execution")
    return out


@still
def pursue(g, mv, holders, army_id, objectives, enemy_tag=None):
    """Exploitation when the enemy front has broken: a SPEARHEAD line (the game's blitz
    order: its divisions push ahead as fast as they can, taking what they find) to the
    FARTHEST of the objective states, and the army set to rush (it attacks along the plan
    without keeping a line or waiting for full supply). One target: given several points
    the game cut the line into pieces and skipped Paris (10.05.1940), given Brest alone it
    drew sixteen provinces through Burgundy and the Loire. If the farthest cannot be
    reached, the next one in the list is tried. The user, 26.04.1940: the French front had
    fallen apart and the armies still advanced as one line."""
    objectives = [int(s) for s in objectives]
    if not objectives:
        raise OpError("name the objective states, the farthest last")
    err = None
    for target in reversed(objectives):
        try:
            out = attack(g, mv, holders, army_id, target, (), (), enemy_tag, blitz=True)
            break
        except OpError as e:
            err = e
    else:
        raise err
    out["spearhead"] = True
    out.pop("SHALLOW", None)
    if target != objectives[-1]:
        out["fell_back_to"] = "%s (%s cannot be reached now)" % (mv.label(target),
                                                                 mv.label(objectives[-1]))
    out["execution"] = B.module("plans").set_mode(g, army(g, army_id),
                                                   execution="rush").get("execution")
    return out


def _order_divisions_deep(p, o):
    """Divisions on an order and the arrows chained under it."""
    n = len(B.arr(p, o, 0x210))
    return n + sum(_order_divisions_deep(p, c) for c in B.arr(p, o, OI_CHILDREN))


def spearheads(g, army_ids):
    """The armies among these whose attack is a spearhead drawn in this run."""
    p = g.p
    out = []
    for aid in army_ids:
        try:
            og = army(g, aid)
        except OpError:
            continue
        for o in _fronts(g, og):
            if any(_TARGETS.get(p.i32(c + OI_ID), "").endswith("|blitz")
                   for c in B.arr(p, o, OI_CHILDREN)):
                out.append(aid)
                break
    return out


def cancel_attack(g, army_id):
    """Remove the army's attack (its arrows); its divisions stay on its sector and hold."""
    ao, p = _armyops(), g.p
    og = army(g, army_id)
    gone = [p.i32(c + OI_ID) for o in _fronts(g, og) for c in B.arr(p, o, OI_CHILDREN)]
    for oid in gone:
        ao.delete_order(g, og, oid)
    time.sleep(SETTLE)
    if not gone:
        raise OpError("army %d has no attack" % army_id)
    return {"army": army_id, "arrows_removed": len(gone)}


STRIKE_REACH = 3


@still
def strike(g, mv, army_id, province, from_province=None, count=1):
    """A direct order to 1-3 divisions of an army: move to or attack a province at most
    STRIKE_REACH steps from where they stand (a right click). The divisions stay in their
    army and on its sector; the game puts a direct order above the battle plan, and the
    plan takes them back when it has run. Fresh divisions nearest the target go first."""
    og = army(g, army_id)
    if is_group(g, og):
        raise OpError("%d is an army group; name one of its armies" % army_id)
    if province not in mv.land:
        raise OpError("p%d is not a land province" % province)
    count = max(1, min(int(count), 3))
    dist, todo = {province: 0}, collections.deque([province])
    while todo:
        q = todo.popleft()
        if dist[q] >= STRIKE_REACH:
            continue
        for n in mv.adj.get(q, ()):
            if n in mv.land and n not in dist:
                dist[n] = dist[q] + 1
                todo.append(n)
    p = g.p
    cand = []
    for d in division_bases(g, og):
        at = province_of(g, d)
        if at in dist and (from_province is None or at == from_province):
            cand.append((dist[at], -p.i64(d + A_ORG), d, at))
    if not cand:
        where = sorted({province_of(g, d) for d in division_bases(g, og)} - {None})
        raise OpError("no division of army %d stands within %d provinces of p%d%s; it stands "
                      "in %s" % (army_id, STRIKE_REACH, province,
                                 " in p%d" % from_province if from_province else "",
                                 ", ".join("p%d" % x for x in where[:12]) or "nowhere"))
    cand.sort()
    uo, sent = B.module("unitorders"), []
    for k, _, d, at in cand[:count]:
        if k == 0:
            continue
        ok, err = uo.move(g, d, province)
        if not ok:
            raise OpError("the game refused the order: %s" % err)
        sent.append((d, at, k))
    if not sent:
        raise OpError("the nearest divisions already stand in p%d" % province)
    time.sleep(SETTLE)
    moving = [d for d, _, _ in sent if uo.movement(g, d)]
    if not moving:
        raise OpError("the game accepted the order but no division set off (no route?)")
    return {"army": army_id, "to": "p%d" % province,
            "divisions": ["%s from p%d, %d prov" % (_template(g, d), at, k)
                          for d, at, k in sent if d in moving]}


def army_stretches(g, theatre_id):
    """{army id: (divisions, provinces of its stretch of the line)} in the theatre."""
    fr = _front_index(g)
    out = {}
    for m in theatre_armies(g, theatre_id):
        provs = {pr for o in _fronts(g, m) for pr in _sector_line(g, fr, o)}
        out[_hid(g, m)] = (len(division_bases(g, m)), len(provs))
    return out


def _lopsided(g, mv, fr, arms, face, at_war, held, names, out):
    """LOPSIDED: inside a theatre at war, one army holds its stretch many times thicker
    than another, measured against what each stretch needs: a division a province plus
    the enemy divisions next to it (in his provinces or in a neutral's or an ally's he
    stands in), less what our forts take off an attack (15% a level).
    (i45, 11.1939: 29 divisions on 3 provinces at Dunkirk against 2, while the stretch by
    Luxembourg stood thin, and the Germans broke through there.)"""
    G = B.module("game")
    at = collections.Counter()
    for e in at_war:
        try:
            for d in G._arr(g.p, g.country(e).ptr, 0x290):
                q = province_of(g, d)
                if q is not None:
                    at[q] += 1
        except Exception:
            continue
    for tid, ms in arms.items():
        war = face[tid] & set(at_war)
        if not war:
            continue
        rows = []
        for m in ms:
            line = {pr for o in _fronts(g, m) for pr in _sector_line(g, fr, o)}
            n = len(division_bases(g, m))
            if not line or not n:
                continue
            his, need = set(), float(len(line))
            for a in line:
                cut = max(0.1, 1 - 0.15 * mv.forts.get(a, 0))
                for b in mv.adj.get(a, ()):
                    if b in mv.land and held(b) != g.player_tag() and b not in his:
                        his.add(b)
                        need += at[b] * cut
            foes_n = sum(at[b] for b in his)
            rows.append((n / need, _hid(g, m), n, len(line), foes_n))
        if len(rows) < 2:
            continue
        rows.sort()
        thin, thick = rows[0], rows[-1]
        # thick: divisions standing idle against nothing; thin: short of what it needs
        if thick[0] >= 2.5 and thick[0] >= 2 * thin[0]:
            out[tid].append((
                "LOPSIDED", "lopsided %d" % thin[1],
                "LOPSIDED: army %d (%s) holds %d provinces with %d divisions against %d of "
                "his; army %d (%s) holds %d with %d against %d. Give the thick army a longer "
                "stretch and the thin one a shorter (set_sectors weights), and ask Le Chonk "
                "for divisions to be moved between them (reinforce_army)"
                % (thin[1], names.get(thin[1], "?"), thin[3], thin[2], thin[4],
                   thick[1], names.get(thick[1], "?"), thick[3], thick[2], thick[4])))


def situation(g, mv, holders, commands, at_war=(), foes=()):
    """What a commander must not miss, as (kind, key, text), per theatre, and under None
    what is no theatre's. commands {theatre id: its states}.
      HOLE       a state of the theatre where a neighbour walks in: border provinces with no
                 division of ours, the state on the line but on no army's sector, the enemy
                 inside. Our states on the border of a country at war with us or that a
                 theatre expects (foes) but in no theatre: one line per country, to None.
      POCKET     enemy land cut off from its main body, to the theatre whose army is nearest.
      IDLE       at war: an army with divisions on its sector and no attack.
      NO SECTOR  an army on no stretch of the line."""
    p, own = g.p, g.player_tag()
    held = _ctl(g)
    fr = _front_index(g)
    at_war = set(at_war)
    out = {tid: [] for tid in commands}
    out[None] = []
    manned = {province_of(g, d) for d in B.arr(p, g.player().ptr, 0x290)}
    face = {tid: land_borders(g, mv, holders, reg) for tid, reg in commands.items()}
    arms = {}
    for tid in commands:
        try:
            arms[tid] = theatre_armies(g, tid)
        except OpError:
            arms[tid] = []
    # who stands where: every theatre's armies' sectors, for "nearest army"
    src, names, on_sector = {}, {}, collections.defaultdict(set)
    sector_provs = collections.defaultdict(set)
    for tid, ms in arms.items():
        for m in ms:
            names[_hid(g, m)] = leader_name(g, m) or "no general"
            # a fresh sector gets its divisions only when the clock runs (23.10: Kan'in cut
            # Chahar and Pailingmiao on a paused day, the order stood empty, and end_turn
            # called them holes three times); an army with divisions holds what it was cut
            manned_army = bool(division_bases(g, m))
            for o in _fronts(g, m):
                if not B.arr(p, o, 0x210) and not manned_army:
                    continue
                line = _sector_line(g, fr, o)
                on_sector[tid] |= {mv.state_of.get(x) for x in line}
                sector_provs[tid] |= set(line)
                for x in line:
                    src.setdefault(x, (tid, _hid(g, m)))
    dist, who = dict.fromkeys(src, 0), dict(src)
    q = collections.deque(src)
    while q:
        a = q.popleft()
        for b in mv.adj.get(a, ()):
            if b in mv.land and b not in dist:
                dist[b], who[b] = dist[a] + 1, who[a]
                q.append(b)

    def near(provs, tid=None):
        hit = [(dist[x], who[x]) for x in provs if x in dist and (tid is None or who[x][0] == tid)]
        return min(hit) if hit else None

    def hole(s, tset, line_states, sectors, pres, bare=()):
        prs = mv.land_provinces(s)
        why = []
        inside = sum(pres.get(x, {}).get("divisions", 0) for x in prs)
        if inside:
            why.append("%d enemy divisions inside" % inside)
        # only where the game runs a front (20.10: the Saharan border with Libya touches
        # Italy on the map, but the game draws no front across it and no army can hold it)
        empty = [x for x in prs if held(x) == own and x not in manned
                 and any(b in mv.land and held(b) in tset for b in mv.adj.get(x, ()))] \
            if line_states is None or s in line_states else []
        if empty:
            why.append("%d border provinces with no division of ours" % len(empty))
        if s in line_states and s not in sectors:
            why.append("on no army's sector")
        elif bare:
            # by province, not by state (21.10: one corner province of Alsace-Lorraine on a
            # sector made the whole Maginot front look held; 7 of its 9 were on none)
            why.append("%d of its line provinces on no army's sector" % len(bare))
        return why
    for tid, reg in commands.items():
        tset = face[tid]
        if not tset:
            continue
        pres = B.module("unitorders").enemy_presence(g, sorted(tset & at_war)) if tset & at_war else {}
        line_provs = {pc["line"][i] for pc in theatre_line(g, mv, holders, reg)
                      for i in pc["idx"]}
        line_states = {mv.state_of.get(x) for x in line_provs}
        fwd = forward_of_defence(g, mv, tid)
        for s in sorted(reg):
            prs = mv.land_provinces(s)
            if fwd and prs and all(x in fwd for x in prs):
                continue                    # given up for a defence line behind it
            bare = [x for x in line_provs if mv.state_of.get(x) == s
                    and x not in sector_provs[tid] and x not in fwd]
            why = hole(s, tset, line_states, on_sector[tid], pres, bare)
            if not why:
                continue
            n = near(mv.land_provinces(s), tid)
            if n is None or n[0] > 8:
                text = "HOLE %s: %s; no army of yours within 8 provinces" % (mv.label(s), ", ".join(why))
                out[None].append(("HOLE", "far hole %s" % mv.label(s), "HOLE %s (theatre %d): %s; "
                                  "the theatre has no army within 8 provinces of it"
                                  % (mv.label(s), tid, ", ".join(why))))
            else:
                text = "HOLE %s: %s; nearest army %d (%s), %d provinces away" % (
                    mv.label(s), ", ".join(why), n[1][1], names[n[1][1]], n[0])
            out[tid].append(("HOLE", "hole %s" % mv.label(s), text))
    # our states on a threatened border that no theatre holds
    covered = set().union(*commands.values()) if commands else set()
    threat = at_war | set(foes)
    loose = collections.defaultdict(list)
    if threat:
        pres = B.module("unitorders").enemy_presence(g, sorted(at_war)) if at_war else {}
        fronts = {}
        for st, (o, c) in sorted(holders.items()):
            if o != own or st in covered:
                continue
            for t in sorted(threat & land_borders(g, mv, holders, [st])):
                if t not in fronts:
                    fronts[t] = front_line_states(g, mv, t)
                why = hole(st, {t}, fronts[t], (), pres)
                if why or st in fronts[t]:
                    loose[t].append("%s (%s)" % (mv.label(st), ", ".join(why) or "manned"))
    for t, rows in sorted(loose.items()):
        out[None].append(("HOLE", "no theatre %s" % t, "IN NO THEATRE: our states on the border "
                          "with %s%s: %s. No field marshal answers for them; give them to a "
                          "theatre (redraw_theatre) or open one" % (
                              t, " (AT WAR)" if t in at_war else "", "; ".join(rows))))
    for tag in sorted(at_war):
        for pk in pockets(g, mv, {tag}):
            n = near(pk["_provs"])
            text = ("POCKET: %d enemy divisions cut off in %s (%d provinces). Keep the ring "
                    "closed and destroy it with attacks from the armies that touch it"
                    % (pk["enemy_divisions"], ", ".join(pk["states"]), pk["provinces"]))
            key = "pocket %s" % pk["states"][0]
            if n:
                text += "; nearest army %d (%s)" % (n[1][1], names[n[1][1]])
                out[n[1][0]].append(("POCKET", key, text))
            else:
                out[None].append(("POCKET", key, text))
    _lopsided(g, mv, fr, arms, face, at_war, held, names, out)
    if at_war:
        fall = falling_allies(g, mv, holders, set(at_war))
        own = g.player_tag()
        for tid, reg in commands.items():
            ours = {q for st in reg for q in mv.land_provinces(int(st)) if held(q) == own}
            for t, sp in sorted(fall.items()):
                edge = sorted(q for q in ours if any(
                    b in mv.land and held(b) == t for b in mv.adj.get(q, ())))
                out[tid].append((
                    "ALLY FALLING", "ally falling %s" % t,
                    "ALLY FALLING: %s is %d%% of the way to capitulating; when it falls its "
                    "divisions vanish, its land goes to the enemy, and any army of ours on its "
                    "front stands in occupied land. Plan for it now: bring the armies back to "
                    "our border (hold_ally_front without it, set_sectors) or a DEFENCE LINE "
                    "(hold_line)%s" % (t, round(sp * 100), (", on our border with it: provinces "
                    + " ".join("p%d" % q for q in edge)) if edge else "")))
    for tid, ms in arms.items():
        war = face[tid] & at_war
        for m in ms:
            n = len(division_bases(g, m))
            if not n:
                continue
            aid, gen = _hid(g, m), leader_name(g, m) or "no general"
            secs = _fronts(g, m)
            if not secs:
                out[tid].append(("NO SECTOR", "no sector %d" % aid, "NO SECTOR: army %d (%s), "
                                 "%d divisions, stands on no stretch of the line" % (aid, gen, n)))
            elif war and not any(B.arr(p, o, OI_CHILDREN) for o in secs):
                # an army facing forts holds them, it is not idle (18.12: told it was idle,
                # Heinrici attacked the Maginot line for weeks)
                line = {pr for o in secs for pr in _sector_line(g, fr, o)}
                facing = {b for a in line for b in mv.adj.get(a, ()) if b in mv.land
                          and held(b) in war}
                if not facing or all(mv.forts.get(b, 0) >= 5 for b in facing):
                    continue
                out[tid].append(("IDLE", "idle %d" % aid, "IDLE: army %d (%s), %d divisions, "
                                 "has no attack; it only holds" % (aid, gen, n)))
    return out


def readiness(g, mv, holders, regions, enemy_tag):
    """What still keeps the theatres facing enemy_tag from being deployed: [] when they are
    ready to open the war. regions {theatre id: its states}. (17.10: Le Chonk declared on the
    first day; the divisions were still in their garrisons and Poland broke into East
    Prussia.)"""
    if not regions:
        return ["no theatre under a field marshal faces %s" % enemy_tag]
    out = []
    for tid, reg in regions.items():
        tags = land_borders(g, mv, holders, reg)
        v = command_view(g, mv, holders, tid, tags or {enemy_tag})
        name = "theatre %d (%s)" % (tid, theatre_name(g, theatres(g)[tid]))
        n = v["divisions"]
        spare = sum(a["divisions"] for a in v["armies"] if not a["sector"])
        # armies in reserve are the second echelon when every state of the line has one;
        # they keep the war from opening only while a state of the line stands without an
        # army (18.12: five armies on three states made Belgium unready for ever)
        held = {x for a in v["armies"] for x in a["sector"]}
        line = {mv.label(mv.state_of.get(pc["line"][i]))
                for pc in theatre_line(g, mv, holders, reg) for i in pc["idx"]}
        if spare > n // 10 and line - held:
            out.append("%s: %d of %d divisions are in armies with no sector" % (name, spare, n))
        on = sum(a["on_sector"] for a in v["armies"])
        # a division two provinces back is in the second echelon, not on the road (17.10:
        # ten of Heinrici's stood so for weeks and kept the war from opening)
        line = sum(a.get("on_the_line", 0) + (a["marching_to_it"]["divisions"]
                   if a.get("marching_to_it", {}).get("farthest_provinces_away", 99) <= 2 else 0)
                   for a in v["armies"])
        if on and line < 0.85 * on:
            out.append("%s: only %d of %d divisions have reached the line" % (name, line, on))
        if not any(a["attacks"] for a in v["armies"]):
            out.append("%s: no attack drawn yet" % name)
    sit = situation(g, mv, holders, regions)
    out += [t for tid in regions for k, _, t in sit[tid]
            if k == "HOLE" and "on no army's sector" in t]
    return out


# ------------------------------------------------------------------ time
def date(g):
    return g.date()


def set_running(g, running, speed=None):
    session = B.module("session")
    if speed is not None:
        g.set_game_speed(speed)
        time.sleep(0.2)
    ok, err = session.pause(g, not running)
    if not ok or session.is_paused(g) == running:
        ok, err = session.pause(g, not running)        # once more; see docs/01
    if session.is_paused(g) == running:
        raise OpError("could not %s the game: %s" % ("resume" if running else "pause", err))
    return {"running": running}


# ------------------------------------------------------------------ events
def pending_events(g):
    """Events waiting for the player, as the event windows show them: [{window, event,
    title, text, options: [text, ...]}]. Options are in the order of the event file."""
    from . import events as E
    ao = _armyops()
    gm = B.module("game")
    out = []
    for w, ev, sc, pid in ao.event_windows(g):
        eid = gm.item_name(g.p, ev + 0x18)
        d = E.definitions().get(eid, {})
        out.append({"window": _hid_window(w), "event": eid,
                    "title": E.loc(d.get("title")), "text": (E.loc(d.get("desc")) or "")[:600],
                    "options": [(E.loc(o["name"]) or "?") + (" (THE HISTORICAL CHOICE)"
                                                              if o.get("historical") else "")
                                for o in d.get("options", [])]})
    return out


def _hid_window(w):
    return "%x" % (w & 0xFFFFFF)          # short stable handle for the model


# what each request asks, from the sender's side; the strategic ones wake Le Chonk at
# once, the rest go to his quick turn on events
REQUEST_KINDS = {
    "CRequestEquipmentPurchaseAction": (False, "{f} wants to buy from what we put on the "
                                               "market: {items}, paying {price}; accepting "
                                               "sends it and we are paid, declining keeps it "
                                               "on sale"),
    "CCallAllyAction": (True, "{f} calls us to arms against {e}: accepting puts us at war "
                              "with {e} beside {f}"),
    "COfferJoinFactionAction": (True, "{f} invites us into its faction: accepting makes us a "
                                      "member, its wars ours and ours its"),
    "CJoinFactionAction": (True, "{f} asks to join our faction: accepting makes its wars ours "
                                 "and ours its"),
    "CMilitaryAccessAction": (False, "{f} asks for military access through our territory: "
                                     "its armies may move through and fight from our land"),
    "COfferMilitaryAccessAction": (False, "{f} offers us military access through its "
                                          "territory: our armies may move through its land"),
    "CDockingRightsAction": (False, "{f} asks for docking rights: its fleets may base and "
                                    "repair in our ports"),
    "COfferDockingRightsAction": (False, "{f} offers us docking rights: our fleets may base "
                                         "and repair in its ports"),
    "CAirBaseAccessAction": (False, "{f} asks for access to our air bases for its air wings"),
    "COfferAirBaseAccessAction": (False, "{f} offers us access to its air bases"),
    "CIncomingLendLeaseAction": (False, "{f} offers us lend-lease: equipment sent to us"),
    "CLendLeaseAction": (False, "{f} asks us for lend-lease: equipment from our stockpile "
                                "sent to it"),
}


def requests(g):
    """Requests other countries sent us, waiting for our answer: [{request, what, major}]."""
    out = []
    for r in B.module("diplomacy").incoming(g):
        major, text = REQUEST_KINDS.get(
            r["kind"], (True, r["kind"].replace("Action", "").lstrip("C") + " from {f}"))
        out.append({"request": str(r["id"]), "major": major,
                    "what": text.format(
                        f=r["from"], e=", ".join(r.get("enemies") or ()),
                        items=", ".join("%g %s%s" % (a, e, " (%s)" % d if d else "")
                                        for e, d, a in r.get("items") or ()) or "?",
                        price="%.2f" % r["price"] if (r.get("price") or 0) > 0 else "?")})
    return out


def answer_request(g, request, accept):
    ok, why = B.module("diplomacy").answer_incoming(g, int(request), bool(accept))
    if not ok:
        raise OpError(why)
    time.sleep(SETTLE)
    return {"request": request, "accepted": bool(accept),
            "now_at_war_with": B.module("combat").enemy_tags(g)}


def answer_event(g, window, option):
    ao = _armyops()
    for w, ev, sc, pid in ao.event_windows(g):
        if _hid_window(w) == str(window):
            from . import events as E, planner
            opts = E.definitions().get(B.module("game").item_name(g.p, ev + 0x18),
                                       {}).get("options", [])
            hist = [i for i, o in enumerate(opts) if o.get("historical")]
            if planner.HISTORICAL and hist and int(option) not in hist:
                raise OpError("refused: this run keeps the historical course, and option %s "
                              "leaves it; the historical choice is option %s"
                              % (option, " or ".join(map(str, hist))))
            ok, err = ao.select_event_option(g, pid, ev, sc, int(option), window=w)
            if not ok:
                raise OpError("option refused: %s" % err)
            time.sleep(SETTLE)
            if any(x[0] == w for x in ao.event_windows(g)):     # still in the registry
                raise OpError("the event window is still open after choosing option %s"
                              % option)
            return {"answered": window, "option": int(option)}
    raise OpError("no event window %s is open" % window)


REGULAR = 0.30                  # NDefines UNIT_EXP_LEVELS: green .1 trained .3 regular


def exercise_front_contacts(g, mv, ids, enemies):
    """Hostile territory at/adjacent to any division, independent of theatre setup.

    Uses current province controllers, not state ownership or future planned wars.
    Army groups include their child armies through division_bases(). Pure reads.
    """
    enemies = set(enemies)
    if not enemies:
        return {}
    held = _ctl(g)
    out = {}
    for aid, og in zip(ids, _ogs(g, ids)):
        contacts = set()
        for d in division_bases(g, og):
            pr = province_of(g, d)
            for q in {pr} | set(mv.adj.get(pr, ())):
                if q in mv.land and held(q) in enemies:
                    contacts.add(held(q))
        if contacts:
            out[int(aid)] = sorted(contacts)
    return out


def exercise(g, ids, on=True):
    """Start or stop the exercises of armies (or army groups): divisions drill up to
    Regular, at 20% organisation, burning fuel and wearing equipment. Stops by itself at
    the maximum (the game's stop-at-max)."""
    out = {}
    for i, og in zip(ids, _ogs(g, ids)):
        ok, err = _armyops().set_training(g, og, on, True)
        if not ok:
            raise OpError("army %s: %s" % (i, err))
        out[int(i)] = err or ("exercising" if on else "stopped")    # err: "already ..."
    return out


def drill_picture(g):
    """{army id: (divisions, below Regular, mean experience, exercising)}."""
    from . import army
    ao = _armyops()
    mp = army.template_manpower(g)
    out = {}
    for aid, og in armies(g).items():
        ds = g.division_detail(order_group=og)["divisions"]
        xp = [float(d.get("experience") or 0) / (mp.get(d.get("template")) or 1e9)
              for d in ds]
        if xp:
            out[aid] = (len(xp), sum(1 for x in xp if x < REGULAR - 1e-6), sum(xp) / len(xp),
                        ao.training_state(g, og)["exercising"])
    return out


# ------------------------------------------------------------------ naval invasions, paradrops
# Drawn at peace and launched at war, as the player does (verified 22.10 on Japan 1936:
# the order, its targets and its divisions; the paradrop target is refused at peace).
def _ours(holders, mv, q, own):
    return holders.get(mv.state_of.get(q), (None, None))[1] == own


INVASIONS = {}      # army id -> its naval invasion operation, as planned in this run


def plan_invasion(g, mv, holders, army_id, port, targets, own):
    """The whole army lands from our port on these coastal provinces."""
    og = army(g, int(army_id))
    port = int(port)
    if port not in mv.ports or not _ours(holders, mv, port, own):
        raise OpError("province %s is not one of our naval bases" % port)
    targets = [int(t) for t in targets]
    bad = [t for t in targets if t not in mv.coast or _ours(holders, mv, t, own)]
    if bad or not targets:
        raise OpError("not enemy coastal provinces: %s" % (bad or "none given"))
    divs = division_bases(g, og)
    if not divs:
        raise OpError("army %s has no divisions" % army_id)
    oid, msg = _armyops().naval_invasion(g, og, port, targets, divs)
    if oid is None:
        raise OpError(msg)
    INVASIONS[int(army_id)] = oid
    return {"army": int(army_id), "operation": oid, "divisions": len(divs), "result": msg,
            "note": "it lands when launched (launch_operation) at war, with naval dominance "
                    "on the way and convoys to carry it"}


def plan_paradrop(g, mv, holders, army_id, airbase, targets, own):
    og = army(g, int(army_id))
    if not _ours(holders, mv, int(airbase), own):
        raise OpError("province %s is not ours" % airbase)
    divs = division_bases(g, og)
    oid, msg = _armyops().paradrop(g, og, int(airbase), [int(t) for t in targets], divs)
    if oid is None:
        raise OpError(msg)
    return {"army": int(army_id), "operation": oid, "divisions": len(divs), "result": msg}


def launch_operation(g, army_id, operation, run=True):
    og = army(g, int(army_id))
    ok, err = _armyops().execute_plan(g, og, int(operation), run=run, members=False)
    if not ok:
        raise OpError(err)
    return {"army": int(army_id), "operation": int(operation), "launched": bool(run)}


def cancel_operation(g, army_id, operation):
    ok, err = _armyops().delete_order(g, army(g, int(army_id)), int(operation))
    if not ok:
        raise OpError(err)
    return {"army": int(army_id), "operation": int(operation), "cancelled": True}


def operations(g):
    """[{army, operation, kind, source, targets, divisions, launched}] of our armies."""
    ao = _armyops()
    out = []
    for aid, og in armies(g).items():
        for o in B.arr(g.p, og, ao.OG_ORDERS):
            kind = g.p.i32(o + 0x30)
            if kind not in (3, 4):
                continue
            out.append({"army": aid, "operation": g.p.i32(o + 0x244),
                        "kind": "naval invasion" if kind == 3 else "paradrop",
                        "source": ao.order_source(g, o), "targets": ao.order_targets(g, o),
                        "divisions": g.p.i32(o + 0x21C),
                        "launched": bool(g.p.u8(o + 0x248))})
    return out


# ------------------------------------------------------------------ sea transfer
_SEA = {}


def _sea_regions(mv):
    if id(mv) not in _SEA:
        from . import navy
        _SEA[id(mv)] = navy.SeaRegions(mv)
    return _SEA[id(mv)]


def _sea_route(mv, from_land, to_land):
    """Sea provinces of the shortest crossing from one shore to another (None: no way)."""
    def shore(land):
        return {b for q in land for b in mv.adj.get(q, ()) if b not in mv.land}
    start, goal = shore(from_land), shore(to_land)
    if not start or not goal:
        return None
    prev = {q: None for q in start}
    q_ = collections.deque(start)
    while q_:
        a = q_.popleft()
        if a in goal:
            path = []
            while a is not None:
                path.append(a)
                a = prev[a]
            return path
        for b in mv.adj.get(a, ()):
            if b not in prev and b not in mv.land:
                prev[b] = a
                q_.append(b)
    return None


def _shippable(g, mv, holders, divs, land, near=None):
    """Which of `divs` can cross the sea to the land mass of `land` (provinces): from a port
    of ours on their land mass to one on the target's, over sea regions free of enemy task
    forces. Returns (go, kept reasons, routes, enemies, hostile task forces)."""
    from . import navy
    passable = _passable(g, mv, holders)
    if near is None:
        near = _bfs(mv, [q for q in land if passable(q)], passable)
    sea = _sea_regions(mv)
    enemies = list(B.module("combat").enemy_tags(g))
    hostile = []
    for t in enemies:
        try:
            hostile += [(t, tf) for tf in navy.task_forces(g, t) if sum(tf["ships"].values())]
        except Exception:
            continue
    conv = navy.convoys(g)
    if conv["free"] <= 0:
        raise OpError("no free convoys (%s): divisions cross the sea on convoys" % conv)
    groups, comp = collections.defaultdict(list), {}
    for d in divs:
        at = province_of(g, d)
        if at not in comp:
            for q in _bfs(mv, [at], passable):
                comp.setdefault(q, at)
        groups[comp[at]].append(d)
    ours = {g.player_tag()} | set(B.module("combat").subject_tags(g))

    def ports(provs):
        return [q for q in provs if q in mv.ports
                and holders.get(mv.state_of.get(q), (None, None))[1] in ours]
    kept, routes, go = [], [], []
    goal = ports(near)
    if not goal:
        raise OpError("no port of ours on that land: divisions go by sea only from port to "
                      "port")
    for origin, ds in groups.items():
        mass = [q for q, o in comp.items() if o == origin]
        where = mv.label(mv.state_of.get(origin))
        if not ports(mass):
            kept.append("%d on %s: no port of ours there" % (len(ds), where))
            continue
        # port to port (23.10: from Kanto to the inland China line there was "no sea route"
        # because the route was sought to the line's own shore)
        path = _sea_route(mv, ports(mass), goal)
        if path is None:
            kept.append("%d from %s: no sea route" % (len(ds), where))
            continue
        regs = {sea.region_of[q] for q in path if q in sea.region_of}
        danger = []
        for t, tf in hostile:
            loc = tf["location"]
            r = sea.region_of.get(loc) if loc not in mv.land else next(
                (sea.region_of.get(b) for b in mv.adj.get(loc, ())
                 if b not in mv.land and sea.region_of.get(b) in regs), None)
            if r in regs:
                danger.append("%s %s in %s" % (t, tf["name"], sea.label(r)))
        if danger:
            kept.append("%d from %s: enemy ships on the route (%s); get naval supremacy "
                        "there first (ask the admiralty)" % (len(ds), where,
                                                              "; ".join(danger[:4])))
            continue
        go += ds
        routes.append("%d from %s over %s" % (len(ds), where, ", ".join(
            sea.label(r) for r in sorted(regs)) or "the coast"))
    return go, kept, routes, enemies, hostile


def ship_by_sea(g, mv, holders, army_id, to_state, count=None):
    """Ship the divisions of an army that cannot walk to `to_state` over the sea: they are
    put on the army's sector there and the game carries them from port to port, as it
    does for a front across the sea (23.10: a plain move order across the sea is refused
    by the game, the divisions stood a month; assigned to the sector, a Taiwan division
    set off for the East Hebei port at once). At peace, or at war with enemies that have
    no ships, freely; at war with a navy, only over sea regions where no enemy task force
    is at sea or in an adjacent port."""
    from . import navy
    og = armies(g).get(int(army_id))
    if og is None:
        raise OpError("army %s does not exist" % army_id)
    to_state = int(to_state)
    land = list(mv.land_provinces(to_state))
    if not land:
        raise OpError("state %s has no land provinces" % to_state)
    passable = _passable(g, mv, holders)
    near = _bfs(mv, [q for q in land if passable(q)], passable)
    uo_ = B.module("unitorders")
    # at sea, or already sailing or marching somewhere: under way, left alone
    far = [d for d in division_bases(g, og) if province_of(g, d) not in near
           and province_of(g, d) in mv.land and not uo_.destination(g, d)]
    if not far:
        raise OpError("every division of army %s can walk to %s; no shipping needed"
                      % (army_id, mv.label(to_state)))
    if count:
        far = far[:int(count)]
    sectors = _fronts(g, og)
    if not sectors:
        raise OpError("army %s has no sector: give it one (set_sectors) first; the divisions "
                      "cross the sea to reach it" % army_id)
    fronts = _front_index(g)
    near_sec = [min((near.get(q, 10 ** 6) for q in _section_provs(g, fronts, o)),
                    default=10 ** 6) for o in sectors]
    oid = g.p.i32(sectors[near_sec.index(min(near_sec))] + OI_ID)
    go, kept, routes, enemies, hostile = _shippable(g, mv, holders, far, land, near)
    moved = 0
    if go:
        ok, err = _armyops().assign_to_order(g, og, oid, go)
        if not ok:
            raise OpError("the game refused: %s" % err)
        moved = len(go)
    out = {"army": int(army_id), "to": "the army's sector by %s" % mv.label(to_state),
           "shipping": moved, "routes": routes,
           "sea": "at peace" if not enemies else (
               "enemies have no ships at sea" if not hostile else "routes checked against "
               "enemy task forces"),
           "then": "they sail from port to port and march to the line; the game does it"}
    if kept:
        out["not_shipped"] = kept
    if not moved:
        raise OpError("nothing shipped: %s" % "; ".join(kept))
    return out


def line_weight(g, mv, holders, theatre_id, enemy_tags):
    """Where the theatre's weight stands against where the enemy's does: per army with a
    stretch, its divisions standing on the stretch and the enemy divisions within two
    provinces of it; and enemy coast near the line with nobody on it, for a landing.
    (23.10, the user: 30 divisions stood in the Gobi facing nobody while six bled on
    Beiping, and the wall of the United Front was never turned from the sea.)"""
    p, own = g.p, g.player_tag()
    held = _ctl(g)
    enemies = set(enemy_tags)
    pres = B.module("unitorders").enemy_presence(g, sorted(enemies))
    fronts = _front_index(g)
    rows, line_all = [], set()
    for m in theatre_armies(g, theatre_id):
        provs = {x for o in _fronts(g, m) for x in _sector_line(g, fronts, o)}
        if not provs:
            continue
        line_all |= provs
        ds = division_bases(g, m)
        ours = sum(1 for d in ds if province_of(g, d) in provs)
        ring1 = {b for x in provs for b in mv.adj.get(x, ()) if b in mv.land and held(b) in enemies}
        ring2 = {c for b in ring1 for c in mv.adj.get(b, ()) if c in mv.land
                 and held(c) in enemies} - ring1
        them = sum(pres.get(x, {}).get("divisions", 0) for x in ring1 | ring2)
        sts = list(dict.fromkeys(mv.state_of.get(x) for x in provs))
        rows.append({"army": _hid(g, m), "divisions": len(ds), "on_stretch": ours,
                     "provinces": len(provs), "enemy_near": them,
                     "near": sorted(ring1 | ring2),
                     "stretch": ", ".join(mv.label(s) for s in sts if s)})
    coast, seen_st = [], set()
    if line_all:
        # the whole enemy coast, nearest the line first: the wall stands where his army
        # is, a landing goes where it is not (one province a state)
        near = _bfs(mv, list(line_all), lambda q: q in mv.land)
        sea_ = _sea_regions(mv).region_of
        for q, d in sorted(near.items(), key=lambda x: (x[1], x[0])):
            if d == 0 or held(q) not in enemies or mv.state_of.get(q) in seen_st:
                continue
            if not any(b in sea_ for b in mv.adj.get(q, ())):      # the sea, not a lake
                continue
            if pres.get(q, {}).get("divisions", 0):
                continue
            around = sum(pres.get(b, {}).get("divisions", 0) for b in mv.adj.get(q, ()))
            if around > 2:
                continue
            seen_st.add(mv.state_of.get(q))
            coast.append((d, q, around))
    return {"armies": rows, "empty_coast": [
        {"province": q, "state": mv.label(mv.state_of.get(q)), "from_line": d,
         "enemy_beside": n, "port": q in mv.ports} for d, q, n in coast[:8]]}


STAGE = {}          # army id -> the front order id drawn to bring it to its theatre
STAGE_NEAR = 6      # provinces from the line: a reserve this close stands where it should


def stage_reserves(g, mv, holders, theatre_id, region):
    """Bring a theatre's reserve armies (no sector) that stand far from its line, overseas
    above all, to it: a front order on the line is drawn for them, the game marches and
    ships them there (port to port), and the order is taken away once they are near it,
    so they stand in reserve behind the line again (the user, 23.10: the Hebei theatre's
    reserve, 15 divisions, sat on the Pacific islands and in Tokyo the whole war).
    Returns {army: 'staged' | 'arrived'}."""
    ao, p = _armyops(), g.p
    out = {}
    pieces = theatre_line(g, mv, holders, region)
    if not pieces:
        return out
    line = {pc["line"][i] for pc in pieces for i in pc["idx"]}
    near = _march(g, mv, holders, line)
    uo_ = B.module("unitorders")
    big = max(pieces, key=lambda pc: len(pc["idx"]))
    k = len(big["line"])
    frm, to = min(big["idx"]) * FULL // k, (max(big["idx"]) + 1) * FULL // k
    for m in theatre_armies(g, theatre_id):
        aid = _hid(g, m)
        fr = _fronts(g, m)
        ds = division_bases(g, m)
        if not ds:
            continue
        far = [d for d in ds if near.get(province_of(g, d), 10 ** 6) > STAGE_NEAR]
        moving = [d for d in ds if province_of(g, d) not in mv.land or uo_.destination(g, d)]
        mine = STAGE.get(aid)
        if mine is not None:
            ids = {p.i32(o + OI_ID): o for o in fr}
            if mine not in ids:
                STAGE.pop(aid, None)            # re-cut by the marshal: his now
                continue
            if not far and not moving:
                ao.delete_order(g, m, mine)
                STAGE.pop(aid, None)
                out[aid] = "arrived"
            continue
        if fr or not far:
            continue
        before = {p.i32(o + OI_ID) for o in fr}
        ok, err = ao.draw_front_line(g, m, big["front"], big["sec"], frm, to)
        if not ok:
            continue
        time.sleep(SETTLE)
        new = [p.i32(o + OI_ID) for o in _fronts(g, m) if p.i32(o + OI_ID) not in before]
        if new:
            STAGE[aid] = new[0]
            out[aid] = "staged"
    return out


def landing_divisions(g):
    """Our divisions whose template is mostly marines or amphibious armour: the landing
    force, kept off the line (the user, 23.10: the marines stood on the Mongol border)."""
    from . import army
    p = g.p
    sf = {k for k in army.special_units() if k.startswith(("marine", "amphibious"))}
    cache, out = {}, set()
    for d in B.arr(p, g.player().ptr, 0x290):
        t = p.u64(d + 0x3B8)
        if not t:
            continue
        if t not in cache:
            bats = [b for b in army._battalions(g, t, army.TMPL_BATTALIONS) if b]
            cache[t] = bool(bats) and sum(b in sf for b in bats) * 2 >= len(bats)
        if cache[t]:
            out.add(d)
    return out


# ------------------------------------------------------------------ landings by the norm
# 25.10, the user: landings are the key to the Chinese line, and none was ever made. The
# norm: up to 10 marine divisions into the port (4 fill a full combat width; take what
# there is), the infantry on the beaches beside it, attacking the port from the land.
LANDING_MARINES_MAX = 10


def _centre(q):
    from . import mapimage
    st = mapimage.static()
    return st["cx"][q], st["cy"][q]


def landing_targets(g, mv, holders, enemies, top=8):
    """[{port, state, city, level, beaches, resistance, depth}] of the enemy's ports, the
    least defended and deepest behind his line first. resistance: what his divisions in
    the port, on its beaches and one province around are worth in divisions of ours."""
    from . import front
    held = _ctl(g)
    enemies = set(enemies)
    side = {g.player_tag()} | set(B.module("combat").subject_tags(g))
    worth = front.power_at(g, enemies)
    ours = [q for q in mv.land if held(q) in side]
    line = {b for a in ours for b in mv.adj.get(a, ()) if b in mv.land and held(b) in enemies}
    depth = _bfs(mv, list(line), lambda q: q in mv.land and held(q) in enemies)
    out = []
    for q, lvl in mv.ports.items():
        if q not in mv.land or held(q) not in enemies:
            continue
        beaches = [b for b in mv.adj.get(q, ()) if b in mv.coast and held(b) in enemies]
        ring = set(mv.adj.get(q, ())) | {q}
        res = sum(worth.get(x, 0.0) for x in ring)
        city = mv.city_at.get(q)
        out.append({"port": q, "state": mv.label(mv.state_of.get(q)), "level": lvl,
                    "city": "%s %d" % city if city else None, "beaches": beaches,
                    "resistance": round(res, 1), "depth": depth.get(q, 0)})
    # the thinnest first, the bigger city before the smaller; depth is shown, the marshal
    # weighs it against where his blow should fall
    out.sort(key=lambda x: (round(x["resistance"]), -(mv.city_at.get(x["port"]) or (0, 0))[1]))
    return out[:top]


def plan_landing(g, mv, holders, army_ids, port_target, own, from_port=None):
    """The landing by the norm: the marines of these armies into the enemy port, the rest
    of their divisions onto the beaches beside it. Each part is a naval invasion of its
    own; launch_operation sets them off."""
    t = int(port_target)
    held = _ctl(g)
    if t not in mv.ports or t not in mv.coast:
        raise OpError("p%d is not a port" % t)
    if held(t) == own:
        raise OpError("p%d is ours" % t)
    beaches = [b for b in mv.adj.get(t, ()) if b in mv.coast and held(b) != own]
    if from_port is None:
        ours = [q for q in mv.ports if _ours(holders, mv, q, own)]
        if not ours:
            raise OpError("we have no port")
        tx, ty = _centre(t)
        from_port = min(ours, key=lambda q: (_centre(q)[0] - tx) ** 2 + (_centre(q)[1] - ty) ** 2)
    from_port = int(from_port)
    marines_all = landing_divisions(g)
    plans, out = [], {"port": t, "from_port": from_port, "beaches": beaches, "orders": []}
    for aid in army_ids:
        og = army(g, int(aid))
        divs = division_bases(g, og)
        mar = [d for d in divs if d in marines_all][:LANDING_MARINES_MAX]
        inf = [d for d in divs if d not in marines_all]
        if mar:
            plans.append((int(aid), og, mar, [t], "marines into the port"))
        if inf:
            plans.append((int(aid), og, inf, beaches or [t], "infantry onto the beaches"))
    if not plans:
        raise OpError("these armies have no divisions")
    for aid, og, divs, targets, what in plans:
        oid, msg = _armyops().naval_invasion(g, og, from_port, targets, divs)
        if oid is None:
            out["orders"].append({"army": aid, "what": what, "refused": msg})
            continue
        INVASIONS[aid] = oid
        out["orders"].append({"army": aid, "operation": oid, "what": what,
                              "divisions": len(divs),
                              "targets": ["p%d" % x for x in targets]})
    return out
