"""The map as the models see it: states with names, victory points and neighbours.

Models choose targets as STATES (a name and an id they can reason about); the executor
turns states into province lists. Static facts come from the game files, live facts
(who owns and controls what, where the fronts are) from the bridge, read-only.
"""
import collections
import glob
import os
import re

from . import bridge as B


def _read(path):
    return open(path, encoding="utf-8-sig", errors="replace").read()


class MapView:
    def __init__(self, game_dir=None):
        self.dir = game_dir or B.game_dir()
        self.names = self._names()
        self.provinces, self.vp, self.buildings = self._history()
        mapdata = B.module("mapdata")
        self.adj = mapdata.build_adjacency()
        _, ptype = mapdata.load_definitions()
        self.land = {p for p, t in ptype.items() if t == "land"}
        self.sea = {p for p, t in ptype.items() if t == "sea"}
        self.state_of = {p: s for s, ps in self.provinces.items() for p in ps}
        self.neighbours = self._state_adjacency()
        self.terrain = mapdata.load_terrain()
        self.rivers = mapdata.build_rivers()
        self.cities = self._cities()
        self.city_at, self.forts = self._province_facts()
        self.ports = self._ports()
        self.coast = {p for p in self.land if any(b not in self.land for b in self.adj.get(p, ()))}
        self.supply_nodes, self.rail = self._supply_network()

    # ------------------------------------------------------------ static
    def _names(self):
        loc = os.path.join(self.dir, "localisation", "english")
        out = {}
        for m in re.finditer(r'^\s*STATE_(\d+):\d*\s*"([^"]*)"',
                             _read(os.path.join(loc, "state_names_l_english.yml")), re.M):
            out[int(m.group(1))] = m.group(2)
        return out

    def _cities(self):
        """state id -> [(city, victory points)], biggest first."""
        loc = _read(os.path.join(self.dir, "localisation", "english",
                                 "victory_points_l_english.yml"))
        names = {int(m.group(1)): m.group(2)
                 for m in re.finditer(r'^\s*VICTORY_POINTS_(\d+):\d*\s*"([^"]*)"', loc, re.M)}
        out = {}
        for path in glob.glob(os.path.join(self.dir, "history", "states", "*.txt")):
            txt = _read(path)
            m = re.search(r"\bid\s*=\s*(\d+)", txt)
            if not m:
                continue
            vps = [(names.get(int(pr), "#%s" % pr), int(v)) for pr, v in
                   re.findall(r"victory_points\s*=\s*\{\s*(\d+)\s+(\d+)", txt)]
            if vps:
                out[int(m.group(1))] = sorted(vps, key=lambda x: -x[1])
        return out

    def _province_facts(self):
        """province -> (city, victory points), and province -> land fort level, from the
        1936 history (forts built later are not seen)."""
        loc = _read(os.path.join(self.dir, "localisation", "english",
                                 "victory_points_l_english.yml"))
        names = {int(m.group(1)): m.group(2)
                 for m in re.finditer(r'^\s*VICTORY_POINTS_(\d+):\d*\s*"([^"]*)"', loc, re.M)}
        city, forts = {}, {}
        for path in glob.glob(os.path.join(self.dir, "history", "states", "*.txt")):
            txt = _read(path)
            for pr, v in re.findall(r"victory_points\s*=\s*\{\s*(\d+)\s+(\d+)", txt):
                city[int(pr)] = (names.get(int(pr), "#%s" % pr), int(v))
            for pr, lvl in re.findall(r"\b(\d+)\s*=\s*\{[^{}]*?\bbunker\s*=\s*(\d+)", txt):
                forts[int(pr)] = int(lvl)
        return city, forts

    def _ports(self):
        """province -> naval base level, from the 1936 history."""
        out = {}
        for path in glob.glob(os.path.join(self.dir, "history", "states", "*.txt")):
            for pr, lvl in re.findall(r"\b(\d+)\s*=\s*\{[^{}]*?\bnaval_base\s*=\s*(\d+)",
                                      _read(path)):
                out[int(pr)] = int(lvl)
        return out

    def _supply_network(self):
        """province -> supply hub level, and the railway graph {province: {neighbour:
        level}}, from map/supply_nodes.txt and map/railways.txt (the map as shipped:
        hubs and railways built in the game are not seen)."""
        nodes, rail = {}, collections.defaultdict(dict)
        for ln in _read(os.path.join(self.dir, "map", "supply_nodes.txt")).splitlines():
            f = ln.split()
            if len(f) >= 2 and f[0].isdigit() and f[1].isdigit():
                nodes[int(f[1])] = int(f[0])
        for ln in _read(os.path.join(self.dir, "map", "railways.txt")).splitlines():
            f = [int(x) for x in ln.split() if x.isdigit()]
            if len(f) < 4:
                continue
            lvl, path = f[0], f[2:2 + f[1]]
            for a, b in zip(path, path[1:]):
                rail[a][b] = max(rail[a].get(b, 0), lvl)
                rail[b][a] = max(rail[b].get(a, 0), lvl)
        return nodes, dict(rail)

    def ground(self, sid):
        """Terrain of a state's land provinces, as shares: {'plains': 0.6, 'forest': 0.4}."""
        c = collections.Counter(self.terrain.get(p, "?") for p in self.land_provinces(sid))
        n = sum(c.values()) or 1
        return {t: round(k / n, 1) for t, k in c.most_common(3) if k / n >= 0.15}

    def _history(self):
        provinces, vp, buildings = {}, {}, {}
        self.vp_top = {}             # the largest victory points of one province in the state
        for path in glob.glob(os.path.join(self.dir, "history", "states", "*.txt")):
            txt = _read(path)
            m = re.search(r"\bid\s*=\s*(\d+)", txt)
            pm = re.search(r"provinces\s*=\s*\{([^}]*)\}", txt)
            if not m or not pm:
                continue
            sid = int(m.group(1))
            provinces[sid] = {int(x) for x in re.findall(r"\d+", pm.group(1))}
            vps = [int(v) for _, v in re.findall(r"victory_points\s*=\s*\{\s*(\d+)\s+(\d+)", txt)]
            vp[sid] = sum(vps)
            self.vp_top[sid] = max(vps, default=0)
            b = {}
            bm = re.search(r"buildings\s*=\s*\{(.*?)\n\t\t\}", txt, re.S)
            if bm:
                for k in ("arms_factory", "industrial_complex", "infrastructure"):
                    km = re.search(r"\b%s\s*=\s*(\d+)" % k, bm.group(1))
                    if km:
                        b[k] = int(km.group(1))
            buildings[sid] = b
        return provinces, vp, buildings

    def _state_adjacency(self):
        out = collections.defaultdict(set)
        for p, ns in self.adj.items():
            a = self.state_of.get(p)
            if a is None or p not in self.land:
                continue
            for n in ns:
                b = self.state_of.get(n)
                if b is not None and b != a and n in self.land:
                    out[a].add(b)
        return out

    def name(self, sid):
        return self.names.get(sid, "state %d" % sid)

    def label(self, sid):
        if sid is None:              # a division at sea, on its way (23.10)
            return "at sea"
        return "%s (#%d)" % (self.name(sid), sid)

    def land_provinces(self, sid):
        return sorted(p for p in self.provinces.get(sid, ()) if p in self.land)

    # ------------------------------------------------------------ live
    def holders(self, g):
        """state id -> (owner tag, controller tag), every state some country owns."""
        p, tags = g.p, g.tags()
        out = {}
        for tag in tags:
            try:
                c = g.country(tag)
            except Exception:
                continue
            for s in B.arr(p, c.ptr, B.CO_STATES):
                try:
                    sid, o, ctl = p.i32(s + 0x48), p.i32(s + 0xC8), p.i32(s + 0xCC)
                except OSError:
                    continue
                out[sid] = (_tag(g, o), _tag(g, ctl))
        return out

    def front_states(self, holders, own, enemies):
        """Our states touching enemy-controlled ones, and those enemy states."""
        ours, theirs = set(), set()
        for sid, (_, ctl) in holders.items():
            if ctl != own:
                continue
            for n in self.neighbours.get(sid, ()):
                if holders.get(n, (None, None))[1] in enemies:
                    ours.add(sid)
                    theirs.add(n)
        return ours, theirs

    def describe_state(self, sid, holders=None):
        o, c = (holders or {}).get(sid, (None, None))
        b = self.buildings.get(sid, {})
        d = {"id": sid, "name": self.name(sid), "vp": self.vp.get(sid, 0)}
        if o:
            d["owner"] = o
        if c and c != o:
            d["controller"] = c
        if b.get("arms_factory") or b.get("industrial_complex"):
            d["factories"] = b.get("arms_factory", 0) + b.get("industrial_complex", 0)
        return d


def _tag(g, idx):
    tags = g._tags
    return tags[idx] if tags and 0 <= idx < len(tags) and tags[idx] else None
