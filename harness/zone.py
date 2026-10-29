"""A theatre is an area, not a border: the picture of its zone for the field marshal.

The zone is the theatre's own states and the allies and neutrals beside them. The field
marshal sees what is happening there: each ally's front with the enemy and how near it is
to capitulating, each neutral and whether the enemy gathers on it, and every enemy
division our side can see in and around the zone.

What we see is approximated: the game's fog of war is not read yet, so an enemy division
counts as seen when it stands within SEEN provinces of land our side controls (ours or an
ally's). From our own border that is what the game shows; through an ally we see further,
as in the game.
"""
import collections

from . import front_threats, ops
from .bridge import module as B

SEEN = 2            # provinces from land our side controls
REACH = 2           # how far around our and our allies' land enemy divisions are reported
FALLING = 0.90      # an ally this near capitulation is about to fall


def friends(g):
    """We, our faction and our subjects (25.10: Mengjiang, Japan's puppet, was a neutral)."""
    own = g.player_tag()
    out = {own}
    try:
        out |= {t for t in g.faction_members(own)["members"] if isinstance(t, str)}
    except Exception:
        pass
    try:
        out |= set(B("combat").subject_tags(g, own))
    except Exception:
        pass
    return out


def zone_tags(g, mv, holders, region):
    """The allies and neutrals whose land touches the theatre's states."""
    own = g.player_tag()
    at_war = set(B("combat").enemy_tags(g))
    owner = lambda q: holders.get(mv.state_of.get(q), (None, None))[1]
    out = set()
    for st in region:
        for q in mv.land_provinces(int(st)):
            for b in mv.adj.get(q, ()):
                t = owner(b) if b in mv.land else None
                if t and t != own and t not in at_war:
                    out.add(t)
    return out


def _divisions_at(g, tags):
    G = B("game")
    at = collections.Counter()
    for t in tags:
        try:
            for d in G._arr(g.p, g.country(t).ptr, 0x290):
                q = ops.province_of(g, d)
                if q is not None:
                    at[q] += 1
        except Exception:
            continue
    return at


def seen_provinces(g, mv, holders):
    """Provinces our side sees (approximation, see the module note)."""
    side = friends(g)
    mine = [q for st, (_, c) in holders.items() if c in side for q in mv.land_provinces(st)]
    dist = ops._bfs(mv, mine, lambda q: q in mv.land)
    return {q for q, k in dist.items() if k <= SEEN}


def picture(g, mv, holders, region, forward=(), expected=None):
    """Lines for the field marshal's briefing: the allies, the neutrals and the enemy seen
    in and around the zone."""
    own = g.player_tag()
    side = friends(g)
    at_war = set(B("combat").enemy_tags(g))
    seeds = set(ops.tags_of(expected or ""))
    anticipated = front_threats.coalition(g, seeds) - side
    owner = lambda q: holders.get(mv.state_of.get(q), (None, None))[1]
    tags = zone_tags(g, mv, holders, region) - seeds
    zone_states = set(int(s) for s in region) | {
        st for st, (_, c) in holders.items() if c in tags & side}
    lines = []
    foes = at_war | anticipated
    foes_at = _divisions_at(g, foes)
    from .front import power_at, ours_of
    worth = power_at(g, foes)
    seen = seen_provinces(g, mv, holders)
    for t in sorted(tags & side):
        land = [q for st, (_, c) in holders.items() if c == t for q in mv.land_provinces(st)]
        front = [q for q in land if any(b in mv.land and owner(b) in at_war
                                        for b in mv.adj.get(q, ()))]
        his = {b for q in front for b in mv.adj.get(q, ()) if b in mv.land and owner(b) in at_war}
        mine = _divisions_at(g, [t])
        try:
            sp = g.surrender_progress(t)
        except Exception:
            sp = None
        lines.append(
            "ALLY %s%s: %d divisions in all, %d on its front with the enemy (%d provinces) "
            "against %d of his next to it (%s); surrender progress %s%s" % (
                t, " (YOUR ARMIES HOLD ITS FRONT: hold_ally_front)" if t in forward else "",
                sum(mine.values()), sum(mine[q] for q in front), len(front),
                sum(foes_at[b] for b in his), ours_of(sum(worth[b] for b in his)),
                "%d%%" % round(sp * 100) if sp is not None else "?",
                ": ABOUT TO CAPITULATE, its divisions vanish and its land falls to the enemy"
                if sp is not None and sp >= FALLING else ""))
    for tag in sorted((anticipated - at_war) & (tags | seeds)):
        theirs = {q for st, (_, c) in holders.items() if c == tag
                  for q in mv.land_provinces(st)}
        ours = {q for st in region for q in mv.land_provinces(int(st))}
        border = {q for q in theirs if any(b in ours for b in mv.adj.get(q, ()))}
        near = {q for q in theirs if any(b in border for b in mv.adj.get(q, ()))} | border
        label = ("THE ENEMY EXPECTED, %s (at peace with us)" % tag if tag in seeds else
                 "EXPECTED COALITION %s (at peace with us; same faction as %s)" % (
                     tag, "+".join(sorted(seeds))))
        lines.append("%s: %d divisions seen on its "
                     "border with your theatre (%d provinces), %d more one province behind"
                     % (label, sum(foes_at[q] for q in border & seen), len(border),
                        sum(foes_at[q] for q in (near - border) & seen)))
    for t in sorted(tags - side - anticipated):
        theirs = {q for st, (_, c) in holders.items() if c == t for q in mv.land_provinces(st)}
        near = collections.Counter()
        for q, k in foes_at.items():
            if q in seen and any(b in theirs for b in mv.adj.get(q, ())):
                near[q] = k
        lines.append("NEUTRAL %s: enemy divisions seen on its border %d%s" % (
            t, sum(near.values()), "" if B("combat").enemy_tags(g, t) == [] else
            "; at war with %s" % ", ".join(B("combat").enemy_tags(g, t))))
    by_state = seen_by_state(g, mv, holders, region, expected, (foes_at, seen, zone_states))
    w_state = seen_by_state(g, mv, holders, region, expected, (worth, seen, zone_states))
    if by_state:
        lines.append("ENEMY SEEN IN AND AROUND YOUR ZONE (divisions by state, and what they "
                     "are worth in divisions of ours: weak, half-armed divisions are worth "
                     "little; what our side can see, about two provinces beyond land we or "
                     "our allies hold): %d, worth %d of ours: %s"
                     % (sum(by_state.values()), round(sum(w_state.values())), "; ".join(
                         "%s %d (worth %d)" % (mv.label(s), n, round(w_state[s]))
                         for s, n in by_state.most_common(14))))
    return lines


def seen_by_state(g, mv, holders, region, expected=None, _have=None, worth=False):
    """Counter {enemy state: divisions seen} in and around the theatre's zone; worth: what
    they are worth in divisions of ours (front.power_at), not heads."""
    if _have:
        foes_at, seen, zone_states = _have
    else:
        side = friends(g)
        at_war = set(B("combat").enemy_tags(g))
        seeds = set(ops.tags_of(expected or ""))
        tags = zone_tags(g, mv, holders, region) - seeds
        zone_states = set(int(s) for s in region) | {
            st for st, (_, c) in holders.items() if c in tags & side}
        foes = at_war | (front_threats.coalition(g, seeds) - side)
        if worth:
            from .front import power_at
            foes_at = power_at(g, foes)
        else:
            foes_at = _divisions_at(g, foes)
        seen = seen_provinces(g, mv, holders)
    zone_provs = [q for st in zone_states for q in mv.land_provinces(st)]
    around = ops._bfs(mv, zone_provs, lambda q: q in mv.land)
    by_state = collections.Counter()
    for q, k in foes_at.items():
        if q in seen and around.get(q, 99) <= REACH:
            by_state[mv.state_of.get(q)] += k
    return by_state
