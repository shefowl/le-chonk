"""The operational map as a picture, for the models and for people.

Both Mistral models take images. A briefing in text names states; a map shows where the
front runs, where the troops stand, which way the arrows go and what is in their way,
the way a player sees it. Province shapes come from the game's own provinces.bmp, live
facts (who controls each province, divisions, the arrows as the game stored them) are
read from memory only: no injected calls, so drawing a map can never disturb the game.

    picture = render(collect(g, mv, ...))     # PNG bytes; also saved under logs/maps
"""
import base64
import collections
import io
import os
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import bridge as B
from .llm import ROOT

CACHE = os.path.join(ROOT, "cache-map")
MAPS = os.path.join(ROOT, "logs", "maps")
FONT_DIR = "/usr/share/fonts/google-noto"

OUR = (150, 163, 186)
SEA = (183, 205, 224)
NEUTRAL = (226, 219, 200)
ENEMY = [(214, 128, 120), (196, 150, 104), (200, 120, 160), (170, 110, 110)]
ALLY = (160, 190, 150)
ARMY_COLOURS = [(30, 70, 200), (0, 140, 70), (150, 40, 190), (220, 120, 0),
                (0, 150, 170), (120, 80, 30), (200, 30, 120), (60, 60, 60)]

_STATIC = {}


FONT_SCALE = 1.4   # Mistral reads images at up to 1536 px in 28-px patches (17.10)


def _font(size, bold=False):
    size = int(round(size * FONT_SCALE))
    name = "NotoSans-CondensedSemiBold.ttf" if bold else "NotoSans-Condensed.ttf"
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except OSError:
        return ImageFont.load_default()


# ------------------------------------------------------------------ static map
def static():
    """Province id raster (h, w), centroids, river mask. Built once (~10 s), cached."""
    if _STATIC:
        return _STATIC
    md = B.module("mapdata")
    os.makedirs(CACHE, exist_ok=True)
    f_ids = os.path.join(CACHE, "province_ids.npy")
    if os.path.exists(f_ids):
        ids = np.load(f_ids)
    else:
        rgb = md._read_bmp_rgb(os.path.join(md.GAME, "map", "provinces.bmp"))
        key = (rgb[..., 0].astype(np.int32) << 16) | (rgb[..., 1].astype(np.int32) << 8) \
            | rgb[..., 2].astype(np.int32)
        uniq, inv = np.unique(key.ravel(), return_inverse=True)
        col2id, _ = md.load_definitions()
        lut = np.array([col2id.get(((u >> 16) & 255, (u >> 8) & 255, u & 255), 0)
                        for u in uniq], dtype=np.int32)
        ids = lut[inv].reshape(key.shape)
        np.save(f_ids, ids)
    h, w = ids.shape
    flat = ids.ravel()
    n = int(flat.max()) + 1
    cnt = np.bincount(flat, minlength=n).astype(np.float64)
    ys, xs = np.divmod(np.arange(flat.size), w)
    with np.errstate(invalid="ignore", divide="ignore"):
        cx = np.bincount(flat, weights=xs, minlength=n) / cnt
        cy = np.bincount(flat, weights=ys, minlength=n) / cnt
    riv = md._read_bmp_indexed(os.path.join(md.GAME, "map", "rivers.bmp"))
    _, ptype = md.load_definitions()
    _STATIC.update(ids=ids, cx=cx, cy=cy, count=cnt, river=(riv <= 11),
                   land={p for p, t in ptype.items() if t == "land"})
    return _STATIC


# ------------------------------------------------------------------ live facts
def collect(g, mv, holders, focus_states, enemies, armies=None, title=""):
    """Everything the picture needs, read from the game (memory reads only).
    focus_states: the states the map should show (ours on the front and the enemy's
    behind it); armies: {army id: label} whose divisions and arrows are drawn in colour."""
    from . import ops
    from .mapview import _tag
    p, own = g.p, g.player_tag()
    st = static()
    provs = {pr for s in focus_states for pr in mv.provinces.get(s, ())}
    x0, y0, x1, y1 = _bbox(st, provs, pad=60)
    sub = st["ids"][y0:y1, x0:x1]
    shown = set(np.unique(sub).tolist())
    ptrs = ops._province_ptrs(g)
    ctl = {}
    for pr in shown:
        if pr in st["land"] and pr in ptrs:
            ctl[pr] = _tag(g, p.u16(ptrs[pr] + ops.PROV_CONTROLLER))
    # our divisions by army, enemy divisions by province
    armies = armies or {}
    by_army = {}
    for aid in armies:
        try:
            og = ops.army(g, aid)
        except ops.OpError:
            continue
        by_army[aid] = collections.Counter(ops.province_of(g, d)
                                           for d in ops.division_bases(g, og))
    others = collections.Counter(ops.province_of(g, d) for d in B.arr(p, g.player().ptr, 0x290))
    for c in by_army.values():
        others.subtract(c)
    enemy = {pid: e["divisions"] for pid, e in
             B.module("unitorders").enemy_presence(g, sorted(enemies)).items() if pid in shown}
    arrows = []
    for aid in armies:
        try:
            og = ops.army(g, aid)
        except ops.OpError:
            continue
        for o in [o for owner in ops._plan_owners(g, og) for o in ops._orders(g, owner)]:
            for c in B.arr(p, o, ops.OI_CHILDREN):
                path = _i32_array(p, c + 0x70)     # the arrow's province path, as drawn
                if path:
                    arrows.append({"army": aid, "order": p.i32(c + ops.OI_ID), "path": path,
                                   "divisions": len(B.arr(p, c, 0x210))})
    cities = []
    for s in focus_states:
        for pr, vp, name in _vp_provinces(mv, s):
            if pr in shown:
                cities.append((pr, vp, name))
    return {"title": title, "date": g.date()["text"], "own": own, "enemies": sorted(enemies),
            "box": (x0, y0, x1, y1), "control": ctl, "by_army": by_army,
            "army_labels": armies, "other_ours": {k: v for k, v in others.items() if v > 0},
            "enemy": enemy, "arrows": arrows, "cities": cities,
            "states": {s: mv.label(s) for s in set(mv.state_of.get(pr) for pr in shown)
                       if s is not None},
            "state_of": {pr: mv.state_of.get(pr) for pr in shown}}


def _i32_array(p, at):
    import struct
    # {data, capacity, size}: +8 is the capacity (17.10: arrows "ended" in Somaliland)
    data, n = p.u64(at), p.i32(at + 0xC)
    if not data or not 0 < n < 4096:
        return []
    return list(struct.unpack("<%di" % n, p.read(data, 4 * n)))


_VP = {}


def _vp_provinces(mv, sid):
    """[(province, victory points, city name)] of a state, from the game files."""
    if not _VP:
        import glob
        import re
        from .mapview import _read
        loc = _read(os.path.join(mv.dir, "localisation", "english", "victory_points_l_english.yml"))
        names = {int(m.group(1)): m.group(2)
                 for m in re.finditer(r'^\s*VICTORY_POINTS_(\d+):\d*\s*"([^"]*)"', loc, re.M)}
        for path in glob.glob(os.path.join(mv.dir, "history", "states", "*.txt")):
            txt = _read(path)
            m = re.search(r"\bid\s*=\s*(\d+)", txt)
            if m:
                _VP[int(m.group(1))] = [
                    (int(pr), int(v), names.get(int(pr), "#%s" % pr)) for pr, v in
                    re.findall(r"victory_points\s*=\s*\{\s*(\d+)\s+(\d+)", txt)]
    return _VP.get(sid, [])


def _bbox(st, provs, pad):
    ids = st["ids"]
    h, w = ids.shape
    xs = [st["cx"][p] for p in provs if p < len(st["cx"]) and st["count"][p]]
    ys = [st["cy"][p] for p in provs if p < len(st["cy"]) and st["count"][p]]
    if not xs:
        return 0, 0, w, h
    # centroids are a fair box: spread a little for the province shapes themselves
    x0, x1 = int(min(xs)) - pad, int(max(xs)) + pad
    y0, y1 = int(min(ys)) - pad, int(max(ys)) + pad
    return max(0, x0), max(0, y0), min(w, x1), min(h, y1)


# ------------------------------------------------------------------ drawing
def render(data, width=1536, save_as=None):
    """PNG bytes of the map. North is up (the game's own projection)."""
    st = static()
    x0, y0, x1, y1 = data["box"]
    ids = st["ids"][y0:y1, x0:x1]
    h, w = ids.shape
    own, enemies = data["own"], set(data["enemies"])
    ctl = data["control"]
    etint = {t: ENEMY[i % len(ENEMY)] for i, t in enumerate(sorted(enemies))}

    uniq, inv = np.unique(ids, return_inverse=True)
    pal = np.zeros((len(uniq), 3), np.uint8)
    for i, pr in enumerate(uniq.tolist()):
        t = ctl.get(pr)
        if pr not in st["land"]:
            pal[i] = SEA
        elif t == own:
            pal[i] = OUR
        elif t in enemies:
            pal[i] = etint[t]
        else:
            pal[i] = NEUTRAL
    img = pal[inv].reshape(h, w, 3)

    # borders: province (faint), state, control (strong); computed on the raster
    sid = np.vectorize(lambda pr: data["state_of"].get(pr) or 0, otypes=[np.int32])(uniq)[inv] \
        .reshape(h, w)
    ctag = {pr: (ctl.get(pr) or ("~sea" if pr not in st["land"] else "?")) for pr in uniq.tolist()}
    tags = sorted(set(ctag.values()))
    tix = np.array([tags.index(ctag[pr]) for pr in uniq.tolist()], np.int32)[inv].reshape(h, w)

    def edges(a):
        e = np.zeros(a.shape, bool)
        e[:, 1:] |= a[:, 1:] != a[:, :-1]
        e[1:, :] |= a[1:, :] != a[:-1, :]
        return e
    img[edges(ids)] = (img[edges(ids)] * 0.82).astype(np.uint8)
    img[edges(sid)] = (90, 90, 90)
    img[st["river"][y0:y1, x0:x1] & (pal[inv].reshape(h, w, 3)[..., 2] != SEA[2])] = (40, 110, 220)
    img[edges(tix)] = (20, 20, 20)
    # the front: where our provinces touch an enemy's
    ours = np.isin(ids, [pr for pr, t in ctl.items() if t == own])
    theirs = np.isin(ids, [pr for pr, t in ctl.items() if t in enemies])
    front = np.zeros((h, w), bool)
    for a, b in ((ours, theirs), (theirs, ours)):
        front[:, 1:] |= a[:, 1:] & b[:, :-1]
        front[1:, :] |= a[1:, :] & b[:-1, :]
        front[:, :-1] |= a[:, :-1] & b[:, 1:]
        front[:-1, :] |= a[:-1, :] & b[1:, :]
    img[front] = (200, 0, 0)

    scale = width / float(w)
    big = Image.fromarray(img).resize((width, int(h * scale)), Image.NEAREST)
    d = ImageDraw.Draw(big)

    def at(pr):
        return ((st["cx"][pr] - x0) * scale, (st["cy"][pr] - y0) * scale)

    def spot(prs):
        """Mean position of provinces, weighted by their size."""
        prs = [p for p in prs if p is not None and p < len(st["cx"]) and st["count"][p]]
        if not prs:
            return None
        wgt = [st["count"][p] for p in prs]
        return (sum((st["cx"][p] - x0) * c for p, c in zip(prs, wgt)) / sum(wgt) * scale,
                sum((st["cy"][p] - y0) * c for p, c in zip(prs, wgt)) / sum(wgt) * scale)

    inside = lambda xy: xy and 0 <= xy[0] < big.width and 0 <= xy[1] < big.height
    # state labels: "Name #id", the handle every order uses; small states stay unlabelled
    f_state = _font(14, bold=True)
    by_state = collections.defaultdict(list)
    for pr, s in data["state_of"].items():
        if s and pr in st["land"]:
            by_state[s].append(pr)
    for s, prs in by_state.items():
        if sum(st["count"][p] for p in prs) * scale * scale < 6000:
            continue
        xy = spot(prs)
        if inside(xy):
            name = data["states"].get(s, str(s)).replace(" (#", " #").rstrip(")")
            d.text((xy[0], xy[1] + 16), name, font=f_state, fill=(40, 40, 40), anchor="mm",
                   stroke_width=3, stroke_fill=(250, 250, 245))
    # cities: names only (victory point numbers were read as division counts, 17.10)
    f_city = _font(14)
    for pr, vp, name in data["cities"]:
        if vp < 5:
            continue
        x, y = at(pr)
        r = 3 + min(vp, 25) / 6
        d.ellipse((x - r, y - r, x + r, y + r), fill=(0, 0, 0), outline=(255, 255, 255), width=2)
        d.text((x + r + 3, y - 8), name, font=f_city, fill=(0, 0, 0),
               stroke_width=3, stroke_fill=(255, 255, 255))
    # arrows, labelled with their army at the head
    colour = {aid: ARMY_COLOURS[i % len(ARMY_COLOURS)] for i, aid in enumerate(data["army_labels"])}
    f_arrow = _font(17, bold=True)
    for a in data["arrows"]:
        pts = [at(pr) for pr in a["path"] if pr < len(st["cx"]) and st["count"][pr]]
        if len(pts) < 2:
            continue
        c = colour.get(a["army"], (0, 0, 0))
        d.line(pts, fill=(255, 255, 255), width=12, joint="curve")
        d.line(pts, fill=c, width=7, joint="curve")
        _head(d, pts[-2], pts[-1], c)
        d.text((pts[-1][0] + 10, pts[-1][1] + 4), "%s: %d div" % (a["army"], a["divisions"]),
               font=f_arrow, fill=c, stroke_width=3, stroke_fill=(255, 255, 255))
    # divisions per state: ours per command, the enemy's
    f_n = _font(15, bold=True)
    for aid, cnt in data["by_army"].items():
        per = collections.defaultdict(list)
        for pr, n in cnt.items():
            if pr in data["state_of"]:
                per[data["state_of"][pr]] += [pr] * n
        for s, prs in per.items():
            xy = spot(prs)
            if inside(xy):
                _badge(d, xy, "%s:%d" % (aid, len(prs)), colour[aid], f_n)
    per = collections.defaultdict(list)
    for pr, n in data["other_ours"].items():
        if pr in data["state_of"]:
            per[data["state_of"][pr]] += [pr] * n
    for s, prs in per.items():
        xy = spot(prs)
        if inside(xy):
            _badge(d, xy, str(len(prs)), (100, 100, 100), f_n)
    per = collections.defaultdict(list)
    for pr, n in data["enemy"].items():
        per[data["state_of"].get(pr)] += [pr] * n
    for s, prs in per.items():
        xy = spot(prs)
        if inside(xy):
            _badge(d, xy, str(len(prs)), (190, 0, 0), f_n, shape="diamond")
    # legend
    f_leg = _font(14, bold=True)
    lines = [("%s  %s" % (data["date"], data["title"]), (0, 0, 0), None)]
    for aid, label in data["army_labels"].items():
        n = sum(data["by_army"].get(aid, {}).values())
        lines.append(("%s: %d divisions" % (label, n), colour[aid], "box"))
    lines.append(("enemy divisions in a state", (190, 0, 0), "diamond"))
    lines.append(("our other divisions", (100, 100, 100), "box"))
    lines.append(("red line: front; blue: rivers", (200, 0, 0), None))
    y = 8
    box_w = max(d.textlength(t, font=f_leg) for t, _, _ in lines) + 50
    d.rectangle((6, 4, 6 + box_w, 10 + 28 * len(lines)), fill=(255, 255, 255), outline=(0, 0, 0))
    for t, c, mark in lines:
        if mark == "box":
            d.rectangle((14, y + 6, 32, y + 24), fill=c)
        elif mark == "diamond":
            d.polygon([(23, y + 4), (32, y + 15), (23, y + 26), (14, y + 15)], fill=c)
        d.text((42 if mark else 14, y), t, font=f_leg, fill=c)
        y += 28
    buf = io.BytesIO()
    big.save(buf, "PNG", optimize=True)
    png = buf.getvalue()
    if save_as:
        os.makedirs(MAPS, exist_ok=True)
        with open(os.path.join(MAPS, save_as), "wb") as f:
            f.write(png)
    return png


def _badge(d, xy, text, colour, font, shape="box"):
    x, y = xy
    tw = d.textlength(text, font=font)
    r = max(14, tw / 2 + 7)
    if shape == "diamond":
        d.polygon([(x, y - r - 4), (x + r + 4, y), (x, y + r + 4), (x - r - 4, y)],
                  fill=colour, outline=(255, 255, 255))
    else:
        d.rectangle((x - r, y - 14, x + r, y + 14), fill=colour, outline=(255, 255, 255), width=2)
    d.text((x, y), text, font=font, fill=(255, 255, 255), anchor="mm")


def _head(d, a, b, colour):
    import math
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    L = 30
    p1 = (b[0] - L * math.cos(ang - 0.45), b[1] - L * math.sin(ang - 0.45))
    p2 = (b[0] - L * math.cos(ang + 0.45), b[1] - L * math.sin(ang + 0.45))
    d.polygon([b, p1, p2], fill=colour)


def data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _rings(mv, holders, states, enemies, n):
    out = set(states)
    for _ in range(n):
        out |= {x for s in list(out) for x in mv.neighbours.get(s, ())
                if holders.get(x, (None, None))[1] in enemies}
    return out


def group_map(g, mv, holders, theatre_id, region, title, save_as=None):
    """A field marshal's map: his theatre's front and the neighbours' ground a state deep,
    his armies in colour, every arrow of his plan."""
    from . import ops
    own = g.player_tag()
    tags = ops.land_borders(g, mv, holders, region)
    ours, theirs = mv.front_states(holders, own, tags)
    ours = set(ours) & set(region)
    theirs = {n for s in ours for n in mv.neighbours.get(s, ()) if n in theirs}
    arm = {ops._hid(g, m): "army %d %s" % (ops._hid(g, m), ops.leader_name(g, m) or "")
           for m in ops.theatre_armies(g, theatre_id)}
    focus = _rings(mv, holders, (ours | theirs) or set(region), tags, 1)
    # a marshal's map shows a few states: 1024 px reads as well and costs half (17.10)
    return render(collect(g, mv, holders, focus, tags, arm, title=title), width=1024,
                  save_as=save_as)


def theatre_map(g, mv, holders, groups, enemies, title, save_as=None):
    """The supreme commander's map: every front, each army group in its colour."""
    from . import ops
    own = g.player_tag()
    against = set(enemies) or {t for s, (o, c) in holders.items() for t in (o,)
                               if t and t != own and any(holders.get(n, (None,))[0] == own
                                                         for n in mv.neighbours.get(s, ()))}
    ours, theirs = mv.front_states(holders, own, against)
    focus = _rings(mv, holders, set(ours) | set(theirs), against, 1)
    return render(collect(g, mv, holders, focus, against, groups, title=title),
                  save_as=save_as)


# ------------------------------------------------------------------ the front map
# 25.10, the user: the marshal's map showed the whole theatre with Mongolia, the front a
# knot of labels in a corner, counts by state. Now the front only, province by province,
# with the ground under it (the fog hides what lies deeper anyway).
TERRAIN_TINT = {"plains": None, "desert": (232, 214, 150), "forest": (110, 160, 95),
                "jungle": (70, 140, 80), "hills": (175, 150, 115), "mountain": (120, 100, 85),
                "urban": (140, 140, 150), "marsh": (120, 170, 175)}
TERRAIN_SHARE = 0.45          # how much of a province's colour is its ground


def front_collect(g, mv, theatre_id, armies, enemies, title=""):
    """The theatre's line and the enemy provinces facing it, province by province."""
    from . import front, ops
    fr = front.Front(g, mv, enemies)
    zone = front.group_zone(g, fr, theatre_id) or set()
    if not zone:
        return None
    facing = {b for a in zone for b in mv.adj.get(a, ()) if b in fr.their_line}
    show = zone | facing
    st = static()
    x0, y0, x1, y1 = _bbox(st, show, pad=45)
    sub = st["ids"][y0:y1, x0:x1]
    shown = set(np.unique(sub).tolist())
    side = fr.side
    ctl = {pr: fr.held(pr) for pr in shown if pr in st["land"]}
    by_prov = collections.defaultdict(collections.Counter)   # province -> {army: n}
    for pr, us in fr.units.items():
        if pr not in zone:
            continue
        for u in us:
            if u[0] == "us":
                by_prov[pr][fr.div_army.get(u[3]) or 0] += 1
    enemy = {b: fr.n_div[("them", b)] for b in facing}
    arrows = []
    p = g.p
    for aid in armies:
        try:
            og = ops.army(g, aid)
        except ops.OpError:
            continue
        for o in [o for owner in ops._plan_owners(g, og) for o in ops._orders(g, owner)]:
            for c in B.arr(p, o, ops.OI_CHILDREN):
                path = _i32_array(p, c + 0x70)
                if path:
                    arrows.append({"army": aid, "path": path,
                                   "divisions": len(B.arr(p, c, 0x210))})
    cities = [(pr, vp, name) for s in {mv.state_of.get(q) for q in show}
              if s is not None for pr, vp, name in _vp_provinces(mv, s) if pr in shown]
    return {"title": title, "date": g.date()["text"], "box": (x0, y0, x1, y1),
            "control": ctl, "side": sorted(side), "enemies": sorted(enemies),
            "zone": sorted(zone), "facing": sorted(facing), "by_prov": by_prov,
            "enemy": enemy, "arrows": arrows, "cities": cities, "army_labels": armies,
            "terrain": {pr: mv.terrain.get(pr) for pr in shown},
            "states": {s: mv.label(s) for s in {mv.state_of.get(q) for q in show} if s},
            "state_of": {pr: mv.state_of.get(pr) for pr in shown}}


def front_render(data, width=1280, save_as=None):
    st = static()
    x0, y0, x1, y1 = data["box"]
    ids = st["ids"][y0:y1, x0:x1]
    h, w = ids.shape
    side, enemies = set(data["side"]), set(data["enemies"])
    ctl, show = data["control"], set(data["zone"]) | set(data["facing"])
    uniq, inv = np.unique(ids, return_inverse=True)
    pal = np.zeros((len(uniq), 3), np.uint8)
    for i, pr in enumerate(uniq.tolist()):
        t = ctl.get(pr)
        if pr not in st["land"]:
            c = SEA
        elif t in side:
            c = OUR
        elif t in enemies:
            c = ENEMY[0]
        else:
            c = NEUTRAL
        tint = TERRAIN_TINT.get(data["terrain"].get(pr))
        if tint and pr in st["land"]:
            c = tuple(int(a * (1 - TERRAIN_SHARE) + b * TERRAIN_SHARE) for a, b in zip(c, tint))
        if pr in st["land"] and pr not in show:          # off the front: washed out
            c = tuple(int(a * 0.35 + 235 * 0.65) for a in c)
        pal[i] = c
    img = pal[inv].reshape(h, w, 3)

    def edges(a):
        e = np.zeros(a.shape, bool)
        e[:, 1:] |= a[:, 1:] != a[:, :-1]
        e[1:, :] |= a[1:, :] != a[:-1, :]
        return e
    img[edges(ids)] = (img[edges(ids)] * 0.7).astype(np.uint8)
    img[st["river"][y0:y1, x0:x1] & np.isin(ids, list(st["land"]))] = (40, 110, 220)
    ours = np.isin(ids, data["zone"])
    theirs = np.isin(ids, data["facing"])
    front_ = np.zeros((h, w), bool)
    for a, b in ((ours, theirs), (theirs, ours)):
        front_[:, 1:] |= a[:, 1:] & b[:, :-1]
        front_[1:, :] |= a[1:, :] & b[:-1, :]
        front_[:, :-1] |= a[:, :-1] & b[:, 1:]
        front_[:-1, :] |= a[:-1, :] & b[1:, :]
    img[front_] = (200, 0, 0)
    scale = width / float(w)
    big = Image.fromarray(img).resize((width, int(h * scale)), Image.NEAREST)
    d = ImageDraw.Draw(big)

    def at(pr):
        return ((st["cx"][pr] - x0) * scale, (st["cy"][pr] - y0) * scale)
    f_city, f_n, f_id = _font(13), _font(13, bold=True), _font(11)
    for pr, vp, name in data["cities"]:
        if vp < 2:
            continue
        x, y = at(pr)
        d.text((x, y - 30), name, font=f_city, fill=(0, 0, 0), anchor="mm",
               stroke_width=3, stroke_fill=(255, 255, 255))
    colour = {aid: ARMY_COLOURS[i % len(ARMY_COLOURS)]
              for i, aid in enumerate(data["army_labels"])}
    for a in data["arrows"]:
        pts = [at(pr) for pr in a["path"] if pr < len(st["cx"]) and st["count"][pr]]
        if len(pts) < 2:
            continue
        c = colour.get(a["army"], (0, 0, 0))
        d.line(pts, fill=(255, 255, 255), width=10, joint="curve")
        d.line(pts, fill=c, width=6, joint="curve")
        _head(d, pts[-2], pts[-1], c)
    # each province of the front: ours by army, his in red, its id beneath
    for pr, cnt in data["by_prov"].items():
        x, y = at(pr)
        parts = [(aid, n) for aid, n in cnt.most_common(2)]
        for k, (aid, n) in enumerate(parts):
            _badge(d, (x, y - 9 + 22 * k - 11 * (len(parts) - 1)),
                   "%s:%d" % (aid, n) if aid else str(n),
                   colour.get(aid, (100, 100, 100)), f_n)
    for pr in data["zone"]:
        if not data["by_prov"].get(pr):
            _badge(d, at(pr), "0", (0, 0, 0), f_n)
    for pr in data["facing"]:
        n = data["enemy"].get(pr, 0)
        _badge(d, at(pr), str(n), (190, 0, 0) if n else (230, 150, 150), f_n, shape="diamond")
    for pr in show:
        x, y = at(pr)
        d.text((x, y + 20), "p%d" % pr, font=f_id, fill=(30, 30, 30), anchor="mm",
               stroke_width=2, stroke_fill=(255, 255, 255))
    f_leg = _font(13, bold=True)
    lines = [("%s  %s" % (data["date"], data["title"]), (0, 0, 0), None)]
    for aid, label in data["army_labels"].items():
        n = sum(c.get(aid, 0) for c in data["by_prov"].values())
        lines.append(("%s: %d on the line" % (label, n), colour[aid], "box"))
    lines.append(("enemy divisions in a province (pale: none)", (190, 0, 0), "diamond"))
    lines.append(("black 0: our line province with no division", (0, 0, 0), "box"))
    lines.append(("ground: green forest, brown hills, dark mountain, sand desert, "
                  "grey city, teal marsh, plain colour plains", (60, 60, 60), None))
    lines.append(("red line: front; blue: rivers; pale land: off the front", (200, 0, 0), None))
    # the legend in a band under the map, not over the front
    band = 12 + 24 * len(lines)
    out = Image.new("RGB", (big.width, big.height + band), (255, 255, 255))
    out.paste(big, (0, 0))
    d = ImageDraw.Draw(out)
    y = big.height + 6
    for t, c, mark in lines:
        if mark == "box":
            d.rectangle((12, y + 4, 28, y + 20), fill=c)
        elif mark == "diamond":
            d.polygon([(20, y + 2), (29, y + 12), (20, y + 22), (11, y + 12)], fill=c)
        d.text((38 if mark else 12, y), t, font=f_leg, fill=c)
        y += 24
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    png = buf.getvalue()
    if save_as:
        os.makedirs(MAPS, exist_ok=True)
        with open(os.path.join(MAPS, save_as), "wb") as f:
            f.write(png)
    return png


def front_map(g, mv, holders, theatre_id, region, title, enemies, save_as=None):
    """A field marshal's map at war: his front only; before the war, group_map."""
    from . import ops
    arm = {ops._hid(g, m): "army %d %s" % (ops._hid(g, m), ops.leader_name(g, m) or "")
           for m in ops.theatre_armies(g, theatre_id)}
    data = front_collect(g, mv, theatre_id, arm, enemies, title) if enemies else None
    if not data:
        return group_map(g, mv, holders, theatre_id, region, title, save_as)
    return front_render(data, save_as=save_as)
