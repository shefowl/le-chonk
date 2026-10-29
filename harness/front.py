"""The front in numbers, province by province: what stands on each side of the line of
contact, how hard each enemy province is to take and each of ours to hold, where the
enemy's rear is empty, and which provinces, taken, cut him apart (or us).

Pure reads on the gateway thread. No judgement: the models get the numbers and choose.

Combat value is counted in infantry battalions (9-battalion infantry division = 9 + its
support), scaled by the division's strength and organisation against the best of its
template, and by entrenchment for the defender. Attack is multiplied by the game's own
penalties (terrain, river crossing, forts: bridge/calling/doctrine.py).
"""
import collections

from . import bridge as B
from . import ops

# battalion -> (attack, defence) in infantry battalions; 1939 equipment, rough
BATTALION = {
    "infantry": (1.0, 1.0), "motorized": (1.0, 1.0), "mechanized": (1.5, 1.6),
    "cavalry": (0.8, 0.7), "mountaineers": (1.1, 1.1), "paratrooper": (1.0, 0.9),
    "marine": (1.0, 1.0), "light_armor": (2.5, 0.6), "medium_armor": (3.5, 0.9),
    "heavy_armor": (4.5, 1.2), "modern_armor": (5.0, 1.3),
}
SUPPORT = (0.5, 0.3)          # artillery and support companies (their names do not read)
SUB_NAME = 0x10               # CSubUnitDefinition -> token name ("infantry")
A_TMPL, A_STR, A_ORG, A_DIGIN = 0x3B8, 0x420, 0x428, 0x458
A_SUPPLY = 0x610              # the division's supply, percent (100 = fully supplied)
G_GAME, GAME_SUPPLY = 0x459E9F0, 0x3D0     # the game -> its supply system (CSupplySystem)
SS_PROVINCES, SS_PROV_SIZE = 0x130, 0xD8    # its province records
CO_CAPITAL_STATE = 0xFF0      # the country's capital STATE id
HUB_TRUCKS = 60                # SUPPLY_HUB_FULL_MOTORIZATION_TRUCK_COST
MOTORIZED = {}                 # province -> motorization level, read from the game
HUB_REACH = 6                 # provinces from our line within which enemy hubs are shown
RAIL_REACH = 3                # provinces from our line where a railway can be cut
LOW_SUPPLY = 50
A_FUEL = 0x620                # the division's fuel (CArmy 'fuel'); infantry carries none
_FUEL_FULL = {}               # division -> the most fuel seen: its tank, full in peace
LOW_FUEL = 0.3
# a broken front (the user, 14.05.1940): holes in his line, provinces facing ours with
# no division of his, and fewer divisions than ours in all
HOLES_MIN, HOLES_SHARE, OUTNUMBER = 3, 0.2, 1.5
REGION = 8                    # provinces deep on each side that the cut search looks at
GAP_CAP = 80


def _doctrine():
    return B.module("doctrine")


_MAX_STR = {}
FAST = {"motorized", "mechanized", "light_armor", "medium_armor", "heavy_armor",
        "modern_armor", "cavalry", "amphibious_mechanized", "amphibious_armor",
        "motorized_rocket_brigade", "armored_car"}
_TERRAIN = {}       # battalion -> {terrain: (attack, defence)} from common/units
TERRAINS = ("plains", "forest", "jungle", "hills", "mountain", "desert", "marsh", "urban")


def terrain_mods(name):
    """A battalion's attack and defence modifiers by terrain (common/units: armour loses
    30% attacking into forest, mountaineers gain 35% in the mountains). 25.10, the user:
    tanks climbing mountains were weighed like tanks on the plains."""
    if not _TERRAIN:
        import glob
        import os
        import re
        for path in glob.glob(os.path.join(B.game_dir(), "common", "units", "*.txt")):
            txt = open(path, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\t(\w+)\s*=\s*\{", txt):
                end = txt.find("\n\t}", m.end())
                body = txt[m.end():end]
                mods = {}
                for t in re.finditer(r"\b(%s)\s*=\s*\{([^{}]*)\}" % "|".join(TERRAINS), body):
                    a = re.search(r"\battack\s*=\s*(-?[\d.]+)", t.group(2))
                    dfn = re.search(r"\bdefence\s*=\s*(-?[\d.]+)", t.group(2))
                    if a or dfn:
                        mods[t.group(1)] = (float(a.group(1)) if a else 0.0,
                                            float(dfn.group(1)) if dfn else 0.0)
                _TERRAIN.setdefault(m.group(1), mods)
        _TERRAIN.setdefault("", {})
    return _TERRAIN.get(name, {})


def max_strength(name):
    """A battalion's full strength (max_strength in common/units): a division's strength in
    the game is the sum of its battalions', less what it lacks in equipment and men."""
    if not _MAX_STR:
        import glob
        import os
        import re
        for path in glob.glob(os.path.join(B.game_dir(), "common", "units", "*.txt")):
            txt = open(path, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\t(\w+)\s*=\s*\{", txt):
                end = txt.find("\n\t}", m.end())
                v = re.search(r"\bmax_strength\s*=\s*([\d.]+)", txt[m.end():end])
                if v:
                    _MAX_STR.setdefault(m.group(1), float(v.group(1)))
        _MAX_STR.setdefault("", 0.0)
    return _MAX_STR.get(name, 1.0)


def unit_values(g, own, tags):
    """[(side, province, attack, defence, division, width)] for our divisions ('us') and
    those of `tags` ('them'). The values are the game's own: each division's computed
    stats (CArmy+0x138: soft and hard attack, defence, after equipment, technology,
    doctrines and the country's modifiers), scaled by strength against its full strength
    and by organisation against the best maximum organisation of ours; defence with
    entrenchment. Attack is put on the scale of defence (our divisions' sums), so that two
    equal divisions read 1:1 as before. 25.10, the user: "our divisions are 3-4 times the
    Chinese" -- the battalion table read them equal (7.07 against 6.95), the game reads a
    width-9 Japanese division at soft attack 38 against 13. The battalion table stays as
    the fallback for a division whose stats do not read."""
    p = g.p
    G = B.module("game")
    weights, fulls, tmods = {}, {}, {}
    rows = []
    for tag in [own] + sorted(set(tags) - {own}):
        try:
            c = g.country(tag)
        except Exception:
            continue
        side = "us" if tag == own else "them"
        for d in G._arr(p, c.ptr, 0x290):
            pid = ops.province_of(g, d)
            if pid is None:
                continue
            t = p.u64(d + A_TMPL)
            if t not in weights:
                a = df = full = 0.0
                ma, md = collections.Counter(), collections.Counter()
                for su in G._arr(p, t, 0x68):
                    name = G.item_name(p, su + SUB_NAME)
                    wa, wd = BATTALION.get(name, SUPPORT)
                    a, df = a + wa, df + wd
                    full += max_strength(name)
                    for ter, (xa, xd) in terrain_mods(name).items():
                        ma[ter] += wa * xa
                        md[ter] += wd * xd
                weights[t] = (a or 1.0, df or 1.0)
                fulls[t] = full
                tmods[t] = {ter: (ma[ter] / (a or 1.0), md[ter] / (df or 1.0))
                            for ter in set(ma) | set(md)}
                names = {G.item_name(p, su + SUB_NAME) for su in G._arr(p, t, 0x68)}
                if names & FAST:
                    tmods[t]["fast"] = (1.0, 1.0)     # not a ground: a mark
            rows.append((side, pid, t, d, p.i64(d + A_STR) / 1e5,
                         p.i64(d + A_ORG) / 1e5, p.i64(d + A_DIGIN) / 1e5, game_stats(p, d)))
    best = collections.defaultdict(lambda: [1e-6, 1e-6])
    for _, _, t, _, hp, org, _, _ in rows:
        b = best[t]
        b[0], b[1] = max(b[0], hp), max(b[1], org)
    # the scale of attack against defence, and the reference organisation: our divisions'
    mine = [st for r in rows if r[0] == "us" and (st := r[7])]
    k_att = (sum(st[1] for st in mine) / sum(st[0] for st in mine)) if mine else 1.0
    org_ref = max((st[2] for st in mine), default=0.0)
    out = []
    for side, pid, t, d, hp, org, dig, st in rows:
        dig_f = 1 + _doctrine().DIG_IN_FACTOR * dig
        if st and mine:
            att, dfn, mxo, mxs = st
            f = min(hp / mxs, 1.0) * min(org / (org_ref or mxo), 1.0)
            out.append((side, pid, att * k_att * f, dfn * f * dig_f, d,
                        _doctrine().division_width(g, d), tmods.get(t, {})))
            continue
        hmax, omax = best[t]
        # the template's full strength, not its best division's: a whole army short of
        # equipment is weak, though each division matches the others (24.10: China's
        # half-armed divisions counted as full)
        hmax = max(hmax, fulls.get(t) or 0.0)
        f = min(hp / hmax, 1.0) * min(org / omax, 1.0)
        wa, wd = weights[t]
        out.append((side, pid, wa * f, wd * f * dig_f, d,
                    _doctrine().division_width(g, d), tmods.get(t, {})))
    return out


# CArmy+0x138 -> the division's computed stats (found 25.10 from CArmy's strength method,
# vtable slot 0x210, which the battle plan evaluation sums): fixed point 1e5
A_STATS = 0x138
ST_DEFENCE, ST_BREAKTHROUGH, ST_SOFT, ST_HARD = 0xA0, 0xA8, 0xB8, 0xC0
ST_WIDTH, ST_MAX_ORG, ST_MAX_STR = 0xF8, 0x278, 0x280


def game_stats(p, d):
    """(attack = soft + hard, defence, max organisation, max strength) of a division at
    full strength and organisation, as the game computes them; None if they do not read."""
    try:
        s = p.u64(d + A_STATS)
        if not s:
            return None
        v = [p.i64(s + o) / 1e5 for o in (ST_SOFT, ST_HARD, ST_DEFENCE, ST_MAX_ORG, ST_MAX_STR)]
    except Exception:
        return None
    soft, hard, dfn, mxo, mxs = v
    if dfn <= 0 or mxs <= 0 or mxo <= 0 or soft + hard <= 0:
        return None
    return soft + hard, dfn, mxo, mxs


def power_at(g, tags):
    """Counter {province: the divisions of `tags` there, in our average division}."""
    rows = unit_values(g, g.player_tag(), tags)
    ours = [(a + d) / 2 for side, _, a, d, _, _, _ in rows if side == "us"]
    unit = (sum(ours) / len(ours)) if ours else 1.0
    out = collections.Counter()
    for side, pid, a, d, _, _, _ in rows:
        if side == "them":
            out[pid] += (a + d) / 2 / unit
    return out


def ground(mv, q):
    """(railway level through q, its state's infrastructure, terrain): what an advance
    through q lives on (the map's railways and the 1936 infrastructure)."""
    rl = max((mv.rail.get(q) or {}).values(), default=0)
    infra = (mv.buildings.get(mv.state_of.get(q)) or {}).get("infrastructure", 0)
    return rl, infra, mv.terrain.get(q, "plains")


def axis_value(mv, q):
    """How well an advance through q is fed and moves, about 0.5..1.5: a railway and good
    infrastructure on open ground best, mountains without a railway worst (25.10, the
    user: the north of China is mountains with poor roads, no breakthrough lives there)."""
    rl, infra, ter = ground(mv, q)
    v = 1.0 + (0.25 if rl else 0.0) + 0.05 * (infra - 2)
    if ter in ("mountain", "jungle", "marsh"):
        v -= 0.2 if rl else 0.35
    elif ter in ("hills", "forest"):
        v -= 0.05 if rl else 0.15
    return max(0.4, min(1.6, v))


def ground_label(mv, q):
    rl, infra, _ = ground(mv, q)
    return "%sinfra %d" % ("rail, " if rl else "no rail, ", infra)


def ours_of(n):
    """'about 7 of ours' for a worth."""
    return "worth %d of ours" % round(n) if n >= 0.5 else "worth under one of ours"


class Front:
    def __init__(self, g, mv, enemies, own=None):
        self.g, self.mv = g, mv
        self.own = own or g.player_tag()
        self.enemies = set(enemies)
        self.held = ops._ctl(g)
        self.rivers = mv.rivers
        self.div_army = {}
        for aid, og in ops.armies(g).items():
            for d in ops.division_bases(g, og):
                self.div_army[d] = aid
        self.units = {}                          # province -> [(side, att, def, division)]
        self.supply = {}                         # our division -> supply percent
        self._sup_arr = None                     # the game's province supply records
        self.fuel = {}                           # our division -> fuel left, 0..1
        self.n_div = collections.Counter()       # (side, province) -> divisions
        self._read_units()
        land, adj = mv.land, mv.adj
        # our side: we, our subjects and our faction. A subject's land is ours to hold;
        # a faction member's only where our divisions stand on it (25.10, n3: 32 of
        # Japan's divisions stood on Mengjiang's land, the line read JAP's provinces only,
        # and the marshal saw neither the soft Suiyuan front nor the danger at Pailingmiao)
        self.subjects, self.allies = set(), set()
        try:
            self.subjects = set(B.module("combat").subject_tags(g, self.own))
        except Exception:
            pass
        try:
            self.allies = {t for t in g.faction_members(self.own)["members"]
                           if isinstance(t, str)} - {self.own}
        except Exception:
            pass
        self.side = {self.own} | self.subjects | self.allies
        self.our_line, self.their_line = set(), set()
        for pr in land:
            h = self.held(pr)
            if h != self.own and h not in self.subjects and not (
                    h in self.allies and self.n_div[("us", pr)]):
                continue
            foes = [b for b in adj.get(pr, ()) if b in land and self.held(b) in self.enemies]
            if foes:
                self.our_line.add(pr)
                self.their_line.update(foes)

    # ------------------------------------------------------------ units
    def _read_units(self):
        g, p = self.g, self.g.p
        for side, pid, att, dfn, d, width, mods in unit_values(g, self.own, self.enemies):
            self.units.setdefault(pid, []).append((side, att, dfn, d, width, mods))
            self.n_div[(side, pid)] += 1
            if side == "us":
                self.supply[d] = p.i64(d + A_SUPPLY) / 1e5
                f = p.i64(d + A_FUEL) / 1e5
                full = _FUEL_FULL[d] = max(_FUEL_FULL.get(d, 0.0), f)
                if full > 1:
                    self.fuel[d] = f / full
        ours = [(u[1] + u[2]) / 2 for us in self.units.values() for u in us if u[0] == "us"]
        self.unit = (sum(ours) / len(ours)) if ours else 1.0

    def worth(self, side, provinces):
        """Divisions of `side` on these provinces, in our average division (attack and
        defence after strength and organisation). 25.10, n3: Le Chonk read "Beiping 40
        divisions" and forbade the blow; the 64 there a week later were worth 30 of ours."""
        return sum((u[1] + u[2]) / 2 for pr in provinces for u in self.units.get(pr, ())
                   if u[0] == side) / self.unit

    def power(self, side, provinces, kind):
        i = 1 if kind == "att" else 2
        return sum(u[i] for pr in provinces for u in self.units.get(pr, ()) if u[0] == side)

    def divisions(self, side, provinces):
        return sum(self.n_div[(side, pr)] for pr in provinces)

    # ------------------------------------------------------------ attack maths
    def attack_mult(self, target, sources):
        """The game's penalties on an attack into `target` from `sources` (best river)."""
        D = _doctrine()
        mult = 1.0 + D.TERRAIN.get(self.mv.terrain.get(target, "plains"), D.DEFAULT_TERRAIN)["attack"]
        pens = [D.RIVER_PENALTY.get(self.rivers.get((min(s, target), max(s, target))), 0.0)
                for s in sources]
        if pens:
            mult += max(pens)
        mult += D.FORT_PENALTY * self.mv.forts.get(target, 0)
        return max(mult, 0.1)

    def strike(self, side, sources, target):
        """The attack `side` can put into `target` from `sources`: the strongest divisions
        that fit the combat width of the target's terrain, after the game's penalties."""
        D = _doctrine()
        dirs = [s for s in sources if any(u[0] == side for u in self.units.get(s, ()))]
        ter = self.mv.terrain.get(target, "plains")
        cap = D.width_limit(ter, len(dirs))
        att, width = 0.0, 0.0
        for u in sorted((u for s in dirs for u in self.units.get(s, ()) if u[0] == side),
                        key=lambda u: -self.att_on(u, ter)):
            if width and width + u[4] > cap:
                continue
            att, width = att + self.att_on(u, ter), width + u[4]
        return att * self.attack_mult(target, dirs)

    @staticmethod
    def att_on(u, ter):
        """A division's attack into this ground, with its battalions' terrain modifiers."""
        return u[1] * (1 + (u[5].get(ter, (0.0, 0.0))[0] if len(u) > 5 else 0.0))

    @staticmethod
    def def_on(u, ter):
        return u[2] * (1 + (u[5].get(ter, (0.0, 0.0))[1] if len(u) > 5 else 0.0))

    RESERVE = 0.25              # a defender beyond the combat width: it relieves the line

    def hold(self, side, target, attackers):
        """What defends `target`: the defender fills the same combat width as the attack
        (the strongest first); the rest stand in reserve and count a quarter, for the
        relief they bring, up to one relay of the width. (24.10: 63 Chinese divisions in Beiping were summed whole, the
        ratio read 0.2:1, and two armies with ripe arrows stood for two months.)"""
        D = _doctrine()
        dirs = [s for s in attackers if any(u[0] != side for u in self.units.get(s, ()))]
        ter = self.mv.terrain.get(target, "plains")
        cap = D.width_limit(ter, max(1, len(dirs)))
        line, rest, width = 0.0, 0.0, 0.0
        for u in sorted((u for u in self.units.get(target, ()) if u[0] == side),
                        key=lambda u: -self.def_on(u, ter)):
            if width and width + u[4] > cap:
                rest += self.def_on(u, ter)
                continue
            line, width = line + self.def_on(u, ter), width + u[4]
        # the reserve relieves the line, it does not fight beside it: at most one relay
        # of the width counts, however deep the stack
        return line + self.RESERVE * min(rest, line)

    def attack_ratio(self, target):
        """Our attack into an enemy province of the line against what defends it."""
        src = [a for a in self.mv.adj.get(target, ()) if a in self.our_line]
        att = self.strike("us", src, target)
        dfn = self.hold("them", target, src)
        return att, dfn, src

    def threat_ratio(self, ours):
        """The enemy's attack into one of our line provinces against what holds it."""
        src = [b for b in self.mv.adj.get(ours, ()) if b in self.their_line]
        att = self.strike("them", src, ours)
        return att, self.hold("us", ours, src), src

    # ------------------------------------------------------------ the enemy's rear
    def empty_rear(self, max_depth=6):
        """Enemy provinces no enemy division stands in, reachable from our line through
        such provinces only: province -> (steps, our line province it starts from)."""
        land, adj, held = self.mv.land, self.mv.adj, self.held
        free = lambda q: (q in land and held(q) in self.enemies
                          and not self.n_div[("them", q)])
        seen, todo = {}, collections.deque()
        for a in self.our_line:
            for b in adj.get(a, ()):
                if b not in seen and free(b):
                    seen[b] = (1, a)
                    todo.append(b)
        while todo:
            q = todo.popleft()
            k, a = seen[q]
            if k >= max_depth or len(seen) >= 4000:
                continue
            for n in adj.get(q, ()):
                if n not in seen and free(n):
                    seen[n] = (k + 1, a)
                    todo.append(n)
        return seen

    # ------------------------------------------------------------ supply
    def army_supply(self, zone=None):
        """army -> (divisions, mean supply %, divisions under LOW_SUPPLY), for our armies
        with divisions in or next to `zone` (our line provinces)."""
        near = None
        if zone is not None:
            near = set(zone) | {n for a in zone for n in self.mv.adj.get(a, ())}
        acc = collections.defaultdict(list)
        for pr, us in self.units.items():
            if near is not None and pr not in near:
                continue
            for u in us:
                if u[0] == "us" and u[3] in self.supply:
                    acc[self.div_army.get(u[3])].append(self.supply[u[3]])
        acc.pop(None, None)
        return {a: (len(v), sum(v) / len(v), sum(1 for x in v if x < LOW_SUPPLY))
                for a, v in acc.items()}

    def prov_supply(self, pr):
        """(supply the troops in province pr use, supply that reaches it) per day, as the
        game's supply system keeps them (found 25.10: CSupplySystem+0x130, a record of 0xd8
        bytes per province, +0x0 the troops' demand, +0x8 what the hubs bring in)."""
        if self._sup_arr is None:
            p = self.g.p
            try:
                ss = p.u64(p.u64(p.va(G_GAME)) + GAME_SUPPLY)
                self._sup_arr = p.u64(ss + SS_PROVINCES) or 0
            except Exception:
                self._sup_arr = 0
        if not self._sup_arr:
            return None
        p, rec = self.g.p, self._sup_arr + int(pr) * SS_PROV_SIZE
        return p.i64(rec) / 1e5, p.i64(rec + 8) / 1e5

    def per_division_supply(self):
        """What one of our divisions uses a day, on average (from the provinces they hold)."""
        dem = n = 0
        for pr in {pr for (side, pr), k in self.n_div.items() if side == "us" and k}:
            s = self.prov_supply(pr)
            if s and s[0] > 0:
                dem, n = dem + s[0], n + self.n_div[("us", pr)]
        return dem / n if n else 0.6

    def army_room(self, zone=None):
        """army -> (divisions on its stretch, divisions the stretch's supply feeds): the
        provinces of `zone` where the army stands. The supply reaching a province is the
        game's; a division uses about per_division_supply(). 25.10, the user: a shock
        army is as big as the supply in its region allows."""
        per = self.per_division_supply()
        prov = collections.defaultdict(set)
        for pr, us in self.units.items():
            if zone is not None and pr not in zone:
                continue
            for u in us:
                if u[0] == "us" and self.div_army.get(u[3]) is not None:
                    prov[self.div_army[u[3]]].add(pr)
        out = {}
        for a, prs in prov.items():
            divs = [u[3] for pr in prs for u in self.units.get(pr, ()) if u[0] == "us"]
            # what reaches the divisions now (their supply ratio): the hub and the railway
            # behind them bind before the province does (25.10: stretches whose provinces
            # "fed" 31 held 21 divisions at 50% supply)
            fed = sum(min(self.supply.get(d, 100.0), 100.0) / 100 for d in divs)
            cap = int(fed)
            if fed >= 0.95 * len(divs):
                got = [self.prov_supply(pr) for pr in prs]
                if per and all(x is not None for x in got):
                    cap = max(len(divs), int(sum(x[1] for x in got) / per))
            out[a] = (len(divs), cap)
        return out

    def army_fuel(self, zone=None):
        """army -> (divisions that burn fuel, mean fuel left 0..1, of them under LOW_FUEL)."""
        near = None
        if zone is not None:
            near = set(zone) | {n for a in zone for n in self.mv.adj.get(a, ())}
        acc = collections.defaultdict(list)
        for pr, us in self.units.items():
            if near is not None and pr not in near:
                continue
            for u in us:
                if u[0] == "us" and u[3] in self.fuel:
                    acc[self.div_army.get(u[3])].append(self.fuel[u[3]])
        acc.pop(None, None)
        return {a: (len(v), sum(v) / len(v), sum(1 for x in v if x < LOW_FUEL))
                for a, v in acc.items()}

    def collapse(self, zone=None):
        """Is his front before `zone` broken? {holes, line, ours, his, broken}: holes are
        his line provinces facing ours with none of his divisions; ours and his count
        every division of both sides."""
        line = [a for a in self.our_line if zone is None or a in zone]
        facing = {b for a in line for b in self.mv.adj.get(a, ()) if b in self.their_line}
        holes = [b for b in facing if not self.n_div[("them", b)]]
        ours = sum(n for (s, _), n in self.n_div.items() if s == "us")
        his = sum(n for (s, _), n in self.n_div.items() if s == "them")
        broken = (len(holes) >= HOLES_MIN and len(holes) >= HOLES_SHARE * len(facing)
                  and ours >= OUTNUMBER * max(his, 1))
        return {"holes": len(holes), "line": len(facing), "ours": ours, "his": his,
                "broken": broken}

    def far_ends(self, line):
        """Enemy states with cities, by how far they lie from our line through enemy
        ground: [(state, provinces)], farthest first (Brittany, the Pyrenees, not
        Paris already in reach)."""
        dist = self._region(self._enemy_land, [b for a in line for b in self.mv.adj.get(a, ())
                                               if self._enemy_land(b)], 60)
        best = {}
        for q, k in dist.items():
            sid = self.mv.state_of.get(q)
            if sid is not None and q in self.mv.city_at:
                best[sid] = max(best.get(sid, 0), k + 1)
        return sorted(best.items(), key=lambda x: (-x[1], -sum(
            v for _, v in self.mv.cities.get(x[0], []))))

    def _enemy_land(self, q):
        return q in self.mv.land and self.held(q) in self.enemies

    def enemy_hubs(self, zone=None):
        """Enemy-held supply hubs within HUB_REACH of our line: [(steps, province, level,
        enemy divisions on it)], nearest first."""
        line = [a for a in self.our_line if zone is None or a in zone]
        dist = self._region(self._enemy_land, [b for a in line for b in self.mv.adj.get(a, ())
                                               if self._enemy_land(b)], HUB_REACH - 1)
        return sorted((k + 1, q, self.mv.supply_nodes[q], self.n_div[("them", q)])
                      for q, k in dist.items() if q in self.mv.supply_nodes)

    def our_hubs(self, zone=None):
        """Our supply hubs within HUB_REACH behind our line: [(steps, province, level)]."""
        line = [a for a in self.our_line if zone is None or a in zone]
        dist = self._region(lambda q: q in self.mv.land and self.held(q) in self.side, line,
                            HUB_REACH)
        return sorted((k, q, self.mv.supply_nodes[q]) for q, k in dist.items()
                      if q in self.mv.supply_nodes)

    def _capital(self, tag):
        g = self.g
        try:
            sid = g.p.i32(g.country(tag).ptr + CO_CAPITAL_STATE)
        except Exception:
            return None
        provs = [q for q in self.mv.provinces.get(sid, ()) if q in self.mv.rail]
        return max(provs, key=lambda q: self.mv.city_at.get(q, ("", 0))[1], default=None)

    def rail_cuts(self, zone=None, top=3):
        """Enemy railway provinces near our line whose loss cuts enemy hubs off the
        railway from his capital: [(province, [hub provinces cut off], steps)]."""
        rail = self.mv.rail
        line = [a for a in self.our_line if zone is None or a in zone]
        near = self._region(self._enemy_land, [b for a in line for b in self.mv.adj.get(a, ())
                                               if self._enemy_land(b)], RAIL_REACH - 1)
        out = []
        for tag in self.enemies:
            cap = self._capital(tag)
            if cap is None or self.held(cap) != tag:
                continue
            ok = lambda q: q in rail and self.held(q) == tag

            def reach(removed):
                seen, todo = {cap}, [cap]
                while todo:
                    q = todo.pop()
                    for n in rail.get(q, ()):
                        if n not in seen and n != removed and ok(n):
                            seen.add(n)
                            todo.append(n)
                return seen
            base = reach(None)
            hubs = {q for q in base if q in self.mv.supply_nodes}
            for q, k in near.items():
                if q not in base or q == cap:
                    continue
                lost = sorted(hubs - reach(q) - {q})
                if lost:
                    out.append((q, lost, k + 1))
        out.sort(key=lambda x: (-len(x[1]), x[2]))
        return out[:top]

    # ------------------------------------------------------------ cuts
    def _region(self, side_test, start, depth=REGION):
        adj, land = self.mv.adj, self.mv.land
        dist = {q: 0 for q in start}
        todo = collections.deque(start)
        while todo:
            q = todo.popleft()
            if dist[q] >= depth:
                continue
            for n in adj.get(q, ()):
                if n not in dist and n in land and side_test(n):
                    dist[n] = dist[q] + 1
                    todo.append(n)
        return dist

    @staticmethod
    def _parts(nodes, adj, removed):
        seen, out = set(removed), []
        for s in nodes:
            if s in seen:
                continue
            comp, todo = [], [s]
            seen.add(s)
            while todo:
                q = todo.pop()
                comp.append(q)
                for n in adj.get(q, ()):
                    if n in nodes and n not in seen:
                        seen.add(n)
                        todo.append(n)
            out.append(comp)
        return out

    def cuts(self, side, max_candidates=24, pairs=12):
        """Provinces of `side` ('them': what we can take; 'us': what we can lose) whose
        loss splits that side's land near the front: [{'take': [provinces], 'cut':
        [provinces], 'divisions': n, 'vp': [(city, vp)]}], best first."""
        mine = (lambda q: self.held(q) in self.enemies) if side == "them" \
            else (lambda q: self.held(q) in self.side)
        line = self.their_line if side == "them" else self.our_line
        dist = self._region(mine, line)
        nodes = set(dist)
        adj = self.mv.adj
        base = self._parts(nodes, adj, ())
        comp_of = {q: i for i, c in enumerate(base) for q in c}
        cands = sorted((q for q in nodes if dist[q] <= 3),
                       key=lambda q: (dist[q], self.n_div[(side, q)]))[:max_candidates]

        def split(removed):
            touched = {comp_of[q] for q in removed}
            sub = set().union(*(base[i] for i in touched)) - set(removed)
            parts = self._parts(sub, adj, ())
            if len(parts) < 2:
                return None
            parts.sort(key=len, reverse=True)
            cut = [q for c in parts[1:] for q in c]
            n = self.divisions(side, cut)
            vp = sorted((self.mv.city_at[q] for q in cut if q in self.mv.city_at),
                        key=lambda x: -x[1])
            if not n and not vp and len(cut) < 5:
                return None
            return {"take": sorted(removed), "cut": cut, "divisions": n, "vp": vp}

        found, single = [], set()
        for q in cands:
            r = split([q])
            if r:
                found.append(r)
                single.add(q)
        rest = [q for q in cands if q not in single][:pairs]
        for i, a in enumerate(rest):
            near = self._region(lambda q: q in nodes, [a], 2)
            for b in rest[i + 1:]:
                if near.get(b, 99) > 2:
                    continue
                r = split([a, b])
                if r:
                    found.append(r)
        found.sort(key=lambda r: (-(r["divisions"] * 3 + sum(v for _, v in r["vp"])),
                                  len(r["take"])))
        out, used = [], set()
        for r in found:                     # one entry per cut-off area
            key = frozenset(r["cut"])
            if key in used:
                continue
            used.add(key)
            out.append(r)
        return out


# ------------------------------------------------------------------ the picture as text
def _p(mv, pr):
    c = mv.city_at.get(pr)
    s = mv.names.get(mv.state_of.get(pr), "?")
    return "p%d %s%s" % (pr, s, (" [%s %d]" % c) if c else "")


def _armies_at(fr, provinces):
    c = collections.Counter(fr.div_army.get(u[3]) for pr in provinces
                            for u in fr.units.get(pr, ()) if u[0] == "us")
    c.pop(None, None)
    return "/".join("a%d" % a for a, _ in c.most_common(2)) or "no army"


def picture(fr, zone=None, top=6):
    """Text for a briefing. zone: our line provinces to report (an army group's); None =
    the whole front. Ratios are attack against defence after the game's penalties."""
    mv = fr.mv
    line = [a for a in fr.our_line if zone is None or a in zone]
    lines = []
    # by our state along the line
    by_state = collections.defaultdict(list)
    for a in line:
        by_state[mv.state_of.get(a)].append(a)
    lines.append("LINE BY STATE (ours: divisions on the line | facing: enemy divisions next "
                 "to it, and what they are worth in divisions of ours: count the enemy by "
                 "worth, not by heads | weakest province of ours, enemy attack:our defence):")
    for sid, provs in sorted(by_state.items(), key=lambda kv: mv.names.get(kv[0], "")):
        foes = {b for a in provs for b in mv.adj.get(a, ()) if b in fr.their_line}
        worst = max(provs, key=lambda a: _ratio(*fr.threat_ratio(a)[:2]))
        wr = _ratio(*fr.threat_ratio(worst)[:2])
        empty = sum(1 for a in provs if not fr.n_div[("us", a)])
        lines.append("- %s (#%d): %d prov, %d div%s | facing %d div (%s) | weakest p%d %s" % (
            mv.names.get(sid, "?"), sid, len(provs), fr.divisions("us", provs),
            (", %d EMPTY" % empty) if empty else "", fr.divisions("them", foes),
            ours_of(fr.worth("them", foes)), worst, _fmt(wr)))
    # weak points of the enemy on the line
    targets = [b for b in fr.their_line
               if zone is None or any(a in zone for a in mv.adj.get(b, ()))]
    rear = fr.empty_rear()
    scored = []
    for b in targets:
        att, dfn, src = fr.attack_ratio(b)
        r = _ratio(att, dfn)
        behind = _gap(fr, b, rear)
        scored.append(((min(r, 9) + min(len(behind), 30) / 10 + sum(
            mv.city_at[q][1] for q in behind if q in mv.city_at) / 10),
            b, r, src, behind))
    scored.sort(key=lambda x: -x[0])
    lines.append("WEAK POINTS OF THE ENEMY ON THE LINE (our attack:his defence, terrain "
                 "already in it; behind = empty enemy provinces beyond it):")
    for _, b, r, src, behind in scored[:top]:
        cities = sorted((mv.city_at[q] for q in behind if q in mv.city_at), key=lambda x: -x[1])
        lines.append("- %s, %s%s: %s, %d div (%s); from %s%s" % (
            _p(mv, b), mv.terrain.get(b, "?"),
            ", FORT %d" % mv.forts[b] if mv.forts.get(b) else "", _fmt(r), fr.divisions("them", [b]),
            ours_of(fr.worth("them", [b])),
            _armies_at(fr, [a for a in src if zone is None or a in zone]),
            ("; behind %d empty prov%s" % (len(behind), (", cities " + ", ".join(
                "%s(%d)" % c for c in cities[:3])) if cities else "")) if behind else ""))
    # his front broken: holes in his line, and fewer divisions than ours
    c = fr.collapse(zone)
    if c["broken"]:
        ends = fr.far_ends(line)
        lines.append("HIS FRONT HAS BROKEN: %d of the %d provinces of his line facing you "
                     "hold no division of his, and he has %d divisions in all against our "
                     "%d. Pursue (pursue: a spearhead to the far end of the country, each "
                     "fast army its own) instead of advancing as a line; the cities on the "
                     "way fall by themselves. THE FAR END, farthest first: %s" % (
                         c["holes"], c["line"], c["his"], c["ours"], "; ".join(
                             "%s %d prov" % (mv.label(s), k) for s, k in ends[:8])))
    # empty cities in his rear
    cities = sorted(((k, q, a) for q, (k, a) in rear.items() if q in mv.city_at
                     and (zone is None or a in zone)), key=lambda x: (-mv.city_at[x[1]][1], x[0]))
    if cities:
        lines.append("UNDEFENDED ENEMY CITIES (no enemy division on the way): " + "; ".join(
            "%s %d prov from p%d (%s)" % (_p(mv, q), k, a, _armies_at(fr, [a]))
            for k, q, a in cities[:top]))
    # cuts
    cuts = [c for c in fr.cuts("them")
            if zone is None or any(a in zone for t in c["take"] for a in _near(fr, t))]
    if cuts:
        lines.append("CUTS (take these and the enemy beyond is cut off):")
        for c in cuts[:3]:
            lines.append("- take %s: cuts off %d div in %d prov%s" % (
                " + ".join(_p(mv, t) for t in c["take"]), c["divisions"], len(c["cut"]),
                (", cities " + ", ".join("%s(%d)" % v for v in c["vp"][:3])) if c["vp"] else ""))
    # supply: ours, and his hubs and railways in reach
    sup = fr.army_supply(line)
    if sup:
        lines.append("SUPPLY OF OUR ARMIES HERE (divisions, mean supply, divisions under %d%%): "
                     % LOW_SUPPLY + "; ".join(
                         "a%d %d div %.0f%%%s" % (a, n, m, (", %d LOW" % low) if low else "")
                         for a, (n, m, low) in sorted(sup.items())))
        over = [a for a, (n, m, low) in sup.items() if m < 60 or low * 3 >= n]
        if over:
            lines.append("OVERSTRETCHED SUPPLY: %s. More divisions here starve them all; "
                         "thin the stretch out, take a hub or wait for the railway."
                         % ", ".join("a%d" % a for a in sorted(over)))
    # how big a mass each stretch feeds: the cap of a shock army there (the user, 25.10)
    room = fr.army_room(line)
    if room:
        lines.append("SUPPLY ROOM (army: divisions on its stretch / divisions the supply "
                     "that reaches that stretch feeds in full; a shock army is massed up to "
                     "the second number: more starve them all; the hubs and railways behind "
                     "set it, a taken hub raises it): " + "; ".join(
                         "a%d %d/%d" % (a, n, cap) for a, (n, cap) in sorted(room.items())))
    fuel = fr.army_fuel(line)
    if fuel:
        lines.append("FUEL OF OUR ARMOUR AND MOTORISED HERE (army: divisions that burn fuel, "
                     "fuel left): " + "; ".join(
                         "a%d %d div %.0f%%%s" % (a, n, m * 100, (", %d NEARLY DRY" % low)
                                                  if low else "")
                         for a, (n, m, low) in sorted(fuel.items())))
        dry = [a for a, (n, m, low) in fuel.items() if m < LOW_FUEL]
        if dry:
            lines.append("ARMOUR OUT OF FUEL: %s. Tanks and lorries without fuel crawl and "
                         "fight badly; an attack with them now stalls." %
                         ", ".join("a%d" % a for a in sorted(dry)))
    hubs = fr.enemy_hubs(line)
    if hubs:
        lines.append("ENEMY SUPPLY HUBS IN REACH (taken, his divisions around them starve "
                     "and ours are fed): " + "; ".join(
                         "%s, hub %d, %d prov away%s" % (_p(mv, q), lvl, k,
                                                         ", %d div on it" % n if n else "")
                         for k, q, lvl, n in hubs[:4]))
    rails = fr.rail_cuts(line)
    if rails:
        lines.append("RAILWAYS TO CUT (take this province and these hubs lose the railway "
                     "from his capital): " + "; ".join(
                         "%s, %d prov away: %s" % (_p(mv, q), k, ", ".join(
                             _p(mv, h) for h in lost[:3]) + (" +%d" % (len(lost) - 3)
                                                             if len(lost) > 3 else ""))
                         for q, lost, k in rails))
    # our danger
    danger = []
    for a in line:
        att, dfn, src = fr.threat_ratio(a)
        if att > 0 and (dfn == 0 or att / dfn >= 1.5):
            danger.append((_ratio(att, dfn), a, src))
    danger.sort(key=lambda x: -x[0])
    if danger:
        lines.append("DANGER TO US (enemy attack:our defence):")
        for r, a, src in danger[:top]:
            lines.append("- %s: %s, ours %d div (%s), his %d div next to it (%s)" % (
                _p(mv, a), _fmt(r), fr.divisions("us", [a]), _armies_at(fr, [a]),
                fr.divisions("them", src), ours_of(fr.worth("them", src))))
    ours = [c for c in fr.cuts("us") if c["divisions"]
            and (zone is None or any(t in zone for t in c["take"]))]
    if ours:
        lines.append("OUR NECKS (lose these and our divisions beyond are cut off):")
        for c in ours[:3]:
            lines.append("- %s: %d of our div beyond" % (
                " + ".join(_p(mv, t) for t in c["take"]), c["divisions"]))
    return "\n".join(lines)


def _near(fr, enemy_prov, depth=3):
    """Our line provinces within `depth` steps of an enemy province."""
    seen, todo, out = {enemy_prov}, [enemy_prov], set()
    for _ in range(depth):
        nxt = []
        for q in todo:
            for n in fr.mv.adj.get(q, ()):
                if n in seen:
                    continue
                seen.add(n)
                if n in fr.our_line:
                    out.add(n)
                nxt.append(n)
        todo = nxt
    return out


def _gap(fr, b, rear):
    """The empty enemy provinces adjoining b's far side and beyond: what opens if b falls."""
    start = [q for q in fr.mv.adj.get(b, ()) if q in rear]
    seen, todo = set(start), list(start)
    while todo and len(seen) < GAP_CAP:
        q = todo.pop()
        for n in fr.mv.adj.get(q, ()):
            if n in rear and n not in seen:
                seen.add(n)
                todo.append(n)
    return seen


def _ratio(att, dfn):
    if dfn <= 0:
        return float("inf") if att > 0 else 0.0
    return att / dfn


def _fmt(r):
    if r == float("inf"):
        return "undefended"
    return "%.1f:1" % r


def group_zone(g, fr, theatre_id):
    """Our line provinces a theatre answers for: those on its armies' sector lines
    (where its divisions stand only when no army has a sector: marching divisions
    passed through other groups' stretches, 18.10)."""
    idx = ops._front_index(g)
    zone, stand = set(), set()
    for m in ops.theatre_armies(g, theatre_id):
        for o in ops._fronts(g, m):
            zone.update(ops._sector_line(g, idx, o))
        stand.update(ops.province_of(g, d) for d in ops.division_bases(g, m))
    return (zone or stand) & fr.our_line


def responsibility_zone(fr, region):
    """All contact provinces in the theatre's states, including unassigned borders."""
    states = set(region)
    return {pr for pr in fr.our_line if fr.mv.state_of.get(pr) in states}


def of(ctx, enemy):
    """One read snapshot per enemy in this briefing context, never across turns."""
    cache = ctx.setdefault("_fronts", {})
    if enemy not in cache:
        from .ops import tags_of
        cache[enemy] = ctx["gw"].run(Front, ctx["mv"], set(tags_of(enemy)))
    return cache[enemy]


def counts(fr, zone=None):
    """(line provinces, our divisions on them, his divisions next to them, empty ones)."""
    line = [a for a in fr.our_line if zone is None or a in zone]
    foes = {b for a in line for b in fr.mv.adj.get(a, ()) if b in fr.their_line}
    return (len(line), fr.divisions("us", line), fr.divisions("them", foes),
            sum(1 for a in line if not fr.n_div[("us", a)]))


def summary(fr, zone=None):
    """One line for the supreme commander: the balance on a stretch of the line, its
    weakest province, the best opening into the enemy and the best cut."""
    mv = fr.mv
    line = [a for a in fr.our_line if zone is None or a in zone]
    if not line:
        return "no line of contact"
    foes = {b for a in line for b in mv.adj.get(a, ()) if b in fr.their_line}
    parts = ["line %d prov: ours %d div, his %d div next to it (%s)" % (
        len(line), fr.divisions("us", line), fr.divisions("them", foes),
        ours_of(fr.worth("them", foes)))]
    empty = [a for a in line if not fr.n_div[("us", a)]]
    if empty:
        parts.append("%d of our line provinces EMPTY" % len(empty))
    worst = max(line, key=lambda a: _ratio(*fr.threat_ratio(a)[:2]))
    wr = _ratio(*fr.threat_ratio(worst)[:2])
    if wr >= 1.5:
        parts.append("most threatened %s %s" % (_p(mv, worst), _fmt(wr)))
    # the best blow, so that the supreme sees where an attack wins (25.10, n3: Le Chonk
    # kept the north on hold while Suiyuan stood at 4:1 beside it, a number only the
    # marshal saw)
    blows = sorted(((_ratio(*fr.attack_ratio(b)[:2]), b) for b in foes),
                   key=lambda x: -x[0])
    blows = [(r, b) for r, b in blows if r >= 1.5]
    if blows:
        parts.append("best blows %s" % ", ".join(
            "%s %s (%s)" % (_p(mv, b), _fmt(r), mv.terrain.get(b, "?"))
            for r, b in blows[:2]))
    rear = fr.empty_rear()
    best = max(foes, key=lambda b: len(_gap(fr, b, rear)), default=None)
    if best is not None:
        gap = _gap(fr, best, rear)
        if gap:
            parts.append("widest opening %s (%s), %d empty prov behind" % (
                _p(mv, best), _fmt(_ratio(*fr.attack_ratio(best)[:2])), len(gap)))
    cuts = [c for c in fr.cuts("them") if any(a in line for t in c["take"] for a in _near(fr, t))]
    if cuts:
        c = cuts[0]
        parts.append("a cut of %d prov traps %d div" % (len(c["take"]), c["divisions"]))
    return "; ".join(parts)


def supply_lines(fr, line, name):
    """For the army staff: one front's supply, its armies and our hubs behind it."""
    out = []
    sup = fr.army_supply(line)
    if sup:
        out.append("%s, armies (divisions, mean supply, under %d%%): " % (name, LOW_SUPPLY)
                   + "; ".join("a%d %d div %.0f%%%s" % (a, n, m, (", %d LOW" % low) if low
                                                        else "")
                               for a, (n, m, low) in sorted(sup.items())))
    mine = fr.our_hubs(line)
    if mine:
        out.append("%s, our hubs behind the line: " % name + "; ".join(
            "%s, hub %d, %d prov back, motorization %s" % (
                _p(fr.mv, q), lvl, k, MOTORIZED.get(q, 0))
            for k, q, lvl in mine[:6]))
    return out


def low_supply(fr, line):
    """Armies on this line whose supply runs low: [(army, mean %, divisions low)]."""
    return [(a, m, low) for a, (n, m, low) in sorted(fr.army_supply(line).items())
            if m < 60 or low * 3 >= n]
