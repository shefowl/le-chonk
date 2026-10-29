"""What the army's templates use in full, by kind of equipment, against the stockpile and
the day's production: the industry minister's picture of the army's needs.

The battalions' needs come from the game's unit files (sub_units, need = {...}). What
each division holds now is not read (20.10: the battalions' equipment arrays were found,
CUnit +0x448, but not yet tied to our divisions), so this is the army in full, not the
shortfall.
"""
import collections
import glob
import os
import re

from . import bridge as B
from .spirits import _block_end

_NEEDS = None


def unit_needs():
    """{battalion or company name: {equipment kind: amount}}, from common/units."""
    global _NEEDS
    if _NEEDS is None:
        out = {}
        for path in glob.glob(os.path.join(B.game_dir(), "common", "units", "*.txt")):
            txt = open(path, encoding="utf-8-sig", errors="replace").read()
            m = re.search(r"\bsub_units\s*=\s*\{", txt)
            if not m:
                continue
            body = txt[m.end() - 1:_block_end(txt, m.end() - 1)]
            for u in re.finditer(r"(?m)^\t([\w\-]+)\s*=\s*\{", body):
                blk = body[u.end() - 1:_block_end(body, u.end() - 1)]
                n = re.search(r"\bneed\s*=\s*\{([^}]*)\}", blk)
                if n:
                    out[u.group(1)] = {k: float(v) for k, v in
                                       re.findall(r"([\w\-]+)\s*=\s*([\d.]+)", n.group(1))}
        _NEEDS = out
    return _NEEDS


def kind(equipment):
    """'infantry_equipment_1' -> 'infantry_equipment' (the kind a battalion needs)."""
    return re.sub(r"_\d+$", "", equipment or "")


def picture(g, lines, stockpile):
    """Lines for the minister: each kind of equipment the templates of our divisions use,
    in full, against the stock and what the lines make a day."""
    from . import army
    needs = unit_needs()
    per_div = collections.Counter(d["template"] for d in g.divisions(limit=2000)["divisions"])
    G = B.module("game")
    p = g.p
    full = collections.Counter()
    for t in g.division_templates():
        n = per_div.get(t["name"], 0)
        if not n:
            continue
        ptr = int(t["ptr"], 16)
        for off in (army.TMPL_BATTALIONS, army.TMPL_SUPPORT):
            for b in army._battalions(g, ptr, off):
                for k, v in needs.get(b, {}).items():
                    full[k] += v * n
    if not full:
        return []
    made = collections.Counter()
    for _, eq, _, out, naval in lines:
        if not naval:
            made[kind(eq.split(" ")[0].strip('"'))] += out
    stock = collections.Counter()
    for eq, n in stockpile:
        stock[kind(eq)] += n
    rows = sorted(full.items(), key=lambda kv: -kv[1])
    return ["THE ARMY'S EQUIPMENT (what the templates of our %d divisions use in full | in "
            "stock | made a day; what the divisions hold now is not shown): %s" % (
                sum(per_div.values()), "; ".join(
                    "%s %d | %d | %s" % (k, v, stock.get(k, 0),
                                         ("%.1f" % made[k]) if made.get(k) else "NO LINE WORKS")
                    for k, v in rows))]


def unmade(g):
    """Kinds of equipment the templates of our divisions use, that no line makes and the
    stock does not cover (20.10, 1939: anti-tank companies in the infantry template, no
    anti-tank line, the briefing said NO LINE WORKS and nobody acted)."""
    from . import army
    needs = unit_needs()
    per_div = collections.Counter(d["template"] for d in g.divisions(limit=2000)["divisions"])
    full = collections.Counter()
    for t in g.division_templates():
        n = per_div.get(t["name"], 0)
        if not n:
            continue
        ptr = int(t["ptr"], 16)
        for off in (army.TMPL_BATTALIONS, army.TMPL_SUPPORT):
            for b in army._battalions(g, ptr, off):
                for k, v in needs.get(b, {}).items():
                    full[k] += v * n
    made = {kind(x["equipment"]) for x in g.production_lines()
            if x["factories"] > 0 and "Naval" not in (x.get("class") or "")}
    stock = collections.Counter()
    for s in g.equipment_stockpile(limit=200)["stockpile"]:
        if s["equipment"]:
            stock[kind(s["equipment"])] += s["count"]
    return sorted("%s (templates use %d, %d in stock)" % (k, v, stock.get(k, 0))
                  for k, v in full.items() if k not in made and stock.get(k, 0) < v)


# The game's alert "Missing Equipment Production" (the user, 24.10: air reinforcements
# for carrier fighters, tactical bombers, CAS and carrier CAS that no line made). A
# wing's kind comes from its missions, carrier or land from where it is based; a line's
# from its airframe.
AIRFRAMES = {("fighter", False): "small_plane_airframe", ("fighter", True): "cv_small_plane_airframe",
             ("close air support", False): "small_plane_cas_airframe",
             ("close air support", True): "cv_small_plane_cas_airframe",
             ("naval bomber", False): "small_plane_naval_bomber_airframe",
             ("naval bomber", True): "cv_small_plane_naval_bomber_airframe",
             ("tactical bomber", False): "medium_plane_airframe",
             ("strategic bomber", False): "large_plane_airframe",
             ("transport", False): "transport_plane_equipment"}
NAMES = {"cv_small_plane_airframe": "carrier fighter", "cv_small_plane_cas_airframe": "carrier CAS",
         "cv_small_plane_naval_bomber_airframe": "carrier naval bomber"}


def air_unmade(g):
    """[(airframe, what it is, wings flying it)] that no production line builds."""
    from . import air, navy
    afloat = navy.wings_afloat(g)
    made = {re.sub(r"_\d+$", "", x["equipment"] or "") for x in g.production_lines()
            if x["factories"] > 0}
    need = collections.Counter()
    for w in air.wings(g):
        f = AIRFRAMES.get((w["kind"], w["id"] in afloat))
        if f:
            need[f] += 1
    return [(f, NAMES.get(f) or next(k for (k, cv), v in AIRFRAMES.items() if v == f), n)
            for f, n in sorted(need.items()) if f not in made]
