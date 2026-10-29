"""Doctrines as the game's files describe them: grand doctrines (folder, XP cost, tracks)
and subdoctrines (track, XP cost), keyed by their loc name as the game's objects carry
it (GRAND_DOCTRINE_GRAND_BATTLEPLAN)."""
import glob
import os
import re

from . import bridge as B
from .spirits import _block_end

_DATA = None


def _blocks(path):
    txt = open(path, encoding="utf-8-sig", errors="replace").read()
    for m in re.finditer(r"(?m)^([\w.\-]+)\s*=\s*\{", txt):
        yield m.group(1), txt[m.end() - 1:_block_end(txt, m.end() - 1)]


def data():
    """{"grand": {loc: {...}}, "sub": {loc: {...}}}"""
    global _DATA
    if _DATA is None:
        from .events import loc
        base = os.path.join(B.game_dir(), "common", "doctrines")
        grand, sub = {}, {}
        for f in glob.glob(os.path.join(base, "grand_doctrines", "*.txt")):
            for key, body in _blocks(f):
                nm = re.search(r"\bname\s*=\s*([\w.\-]+)", body)
                tr = re.search(r"\btracks\s*=\s*\{([^}]*)\}", body)
                grand[nm.group(1) if nm else key] = {
                    "key": key, "folder": _get(body, "folder"), "cost": _num(body, "xp_cost"),
                    "xp": _get(body, "xp_type"),
                    "tracks": re.findall(r"[\w.\-]+", tr.group(1)) if tr else [],
                    "title": loc(nm.group(1)) if nm else key}
        for f in glob.glob(os.path.join(base, "subdoctrines", "*", "*.txt")):
            for key, body in _blocks(f):
                nm = re.search(r"\bname\s*=\s*([\w.\-]+)", body)
                sub[nm.group(1) if nm else key] = {
                    "key": key, "track": _get(body, "track"), "cost": _num(body, "xp_cost"),
                    "xp": _get(body, "xp_type"), "title": loc(nm.group(1)) if nm else key}
        _DATA = {"grand": grand, "sub": sub}
    return _DATA


def _get(body, k):
    m = re.search(r"\b%s\s*=\s*([\w.\-]+)" % k, body)
    return m.group(1) if m else None


def _num(body, k):
    m = re.search(r"\b%s\s*=\s*([\d.]+)" % k, body)
    return float(m.group(1)) if m else None


XP_OF = {"land": "army", "air": "air", "naval": "navy", "special_forces": "army"}


def picture(g, folder):
    """Lines for a staff's briefing: its folder's grand doctrine, its tracks and what can
    go in them, the experience it has."""
    D = B.module("doctrines")
    d = data()
    xp = B.module("industry").experience(g)[XP_OF[folder]]
    f = next((x for x in D.status(g) if x["folder"] == folder), None)
    if f is None:
        return []
    grand = d["grand"].get(f["grand"]) if f["grand"] else None
    gd, sd = D.templates(g)
    price = {}
    for k in list(d["grand"]) + list(d["sub"]):
        t = gd.get(k) or sd.get(k)
        price[k] = D.cost(g, folder, t) if t else None
    lines = ["DOCTRINES (%s; %s experience %.0f; the price of each is in brackets, what it costs "
             "us now; adopt_doctrine takes one): grand doctrine %s" % (
                 folder, XP_OF[folder], xp, grand["title"] if grand else "NONE yet")]
    if grand:
        lines.append("  tracks: " + "; ".join(
            "%s: %s" % (t, d["sub"].get(s, {}).get("title", s) if s else "EMPTY")
            for t, s in zip(grand["tracks"], f["tracks"])))
        for t, s in zip(grand["tracks"], f["tracks"]):
            if not s:
                lines.append("  for the %s track: %s" % (t, ", ".join(
                    "%s (%.0f)" % (v["title"], price.get(k) or v["cost"] or 0)
                    for k, v in d["sub"].items() if v["track"] == t)))
    others = [(k, v) for k, v in d["grand"].items() if v["folder"] == folder and k != f["grand"]]
    lines.append("  %s grand doctrines: %s" % ("other" if grand else "possible", ", ".join(
        "%s (%.0f)" % (v["title"], price.get(k) or v["cost"] or 0) for k, v in others)))
    return lines


def adopt(g, folder, name):
    """Take a grand doctrine or a subdoctrine of this folder, by its title or key, if our
    experience pays for it (the game's own check lets a doctrine through without the XP)."""
    D = B.module("doctrines")
    d = data()
    want = name.strip().lower()
    hit = lambda k, v: want in (k.lower(), v["key"].lower(), (v["title"] or "").lower())
    xp = B.module("industry").experience(g)[XP_OF[folder]]
    f = next(x for x in D.status(g) if x["folder"] == folder)
    for k, v in d["grand"].items():
        if hit(k, v):
            if v["folder"] != folder:
                return {"ok": False, "error": "%s is a %s doctrine, not yours" % (v["title"], v["folder"])}
            c = _price(g, D, folder, k, v)
            if xp < c:
                return {"ok": False, "error": "%s costs %.0f %s experience; we have %.0f"
                        % (v["title"], c, XP_OF[folder], xp)}
            r = D.unlock_grand(g, k)
            r["adopted"] = v["title"]
            break
    else:
        for k, v in d["sub"].items():
            if hit(k, v):
                grand = d["grand"].get(f["grand"]) if f["grand"] else None
                if not grand:
                    return {"ok": False, "error": "take a grand doctrine first"}
                if v["track"] not in grand["tracks"]:
                    return {"ok": False, "error": "%s belongs to the %s track, which %s has not"
                            % (v["title"], v["track"], grand["title"])}
                c = _price(g, D, folder, k, v)
                if xp < c:
                    return {"ok": False, "error": "%s costs %.0f %s experience; we have %.0f"
                            % (v["title"], c, XP_OF[folder], xp)}
                r = D.unlock_sub(g, folder, grand["tracks"].index(v["track"]), k)
                r["adopted"] = v["title"]
                break
        else:
            return {"ok": False, "error": "no doctrine called %s" % name}
    import time
    time.sleep(0.5)
    r["experience_after"] = B.module("industry").experience(g)[XP_OF[folder]]
    return r


def folder_of(name, folders):
    """Which of these folders a doctrine (title or key) belongs to, or None."""
    d = data()
    want = name.strip().lower()
    hit = lambda k, v: want in (k.lower(), v["key"].lower(), (v["title"] or "").lower())
    for k, v in d["grand"].items():
        if hit(k, v) and v["folder"] in folders:
            return v["folder"]
    for k, v in d["sub"].items():
        if hit(k, v):
            for gv in d["grand"].values():
                if v["track"] in gv["tracks"] and gv["folder"] in folders:
                    return gv["folder"]
    return None


def adopt_any(g, folders, name):
    """adopt() in whichever of these folders the doctrine belongs to (the army staff's land
    and special forces folders, 26.10)."""
    f = folder_of(name, folders)
    if f is None:
        return {"ok": False, "error": "no %s doctrine called %s" % (" or ".join(folders), name)}
    return adopt(g, f, name)


def _price(g, D, folder, key, v):
    """What the doctrine costs us now, as the game's unlock check counts it (the file's
    cost is only the base: the folder's cost modifier and one-off reductions apply)."""
    gd, sd = D.templates(g)
    t = gd.get(key) or sd.get(key)
    c = D.cost(g, folder, t) if t else None
    return c if c is not None else (v["cost"] or 0)


def affordable(g, folder):
    """A doctrine choice our experience now pays for, or None (for the staff's alarm)."""
    D = B.module("doctrines")
    d = data()
    xp = B.module("industry").experience(g)[XP_OF[folder]]
    f = next((x for x in D.status(g) if x["folder"] == folder), None)
    if f is None:
        return None
    if not f["grand"]:
        c = min(_price(g, D, folder, k, v) for k, v in d["grand"].items() if v["folder"] == folder)
        return "no %s grand doctrine yet and %.0f experience pays for one" % (folder, xp) if xp >= c else None
    grand = d["grand"].get(f["grand"])
    empty = [t for t, s in zip(grand["tracks"] if grand else [], f["tracks"]) if not s]
    cheapest = min([_price(g, D, folder, k, v) for k, v in d["sub"].items() if v["track"] in empty]
                   or [1e9])
    if empty and xp >= cheapest:
        return "%.0f %s experience pays for a subdoctrine in an empty track (%s)" % (
            xp, XP_OF[folder], ", ".join(empty))
    return None
