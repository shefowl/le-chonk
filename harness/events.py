"""Event definitions from the game's files: title, description and options as the player
sees them, for the supreme command to choose from. Options are listed in file order,
which is the order of the game's option list (to verify per event: the chosen one must
match the effect seen afterwards)."""
import glob
import os
import re

from . import bridge as B

_DEFS = None
_LOC = None


def _blocks(txt):
    """Top-level `xxx_event = { ... }` blocks: yields (kind, body). A block runs to the
    next one: counting braces across a whole file breaks on the odd unbalanced brace,
    which then swallowed the rest of WUW_Germany.txt (GER_inner_circle_speer.01 among it)."""
    heads = list(re.finditer(r"(?m)^(country_event|news_event|state_event|unit_leader_event|"
                             r"operative_leader_event)\s*=\s*\{", txt))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(txt)
        yield m.group(1), txt[m.end():end]


def _options(body):
    out, d, k = [], 0, 0
    for m in re.finditer(r"#[^\n]*|[{}]|\boption\s*=\s*\{", body):
        t = m.group()
        if t.startswith("#"):
            continue
        if t == "{":
            d += 1
        elif t == "}":
            d -= 1
        elif d == 0:
            j, dd = m.end(), 1
            while dd and j < len(body):
                dd += body[j] == "{"
                dd -= body[j] == "}"
                j += 1
            ob = body[m.end():j - 1]
            nm = re.search(r"\bname\s*=\s*([\w.\-]+)", ob)
            tr = re.search(r"\btrigger\s*=\s*\{", ob)
            out.append({"name": nm.group(1) if nm else None, "has_trigger": bool(tr),
                        "historical": _historical(ob)})
            d += 1
    return out


def _historical(ob):
    """Whether the game's AI takes this option on the historical course: a modifier of its
    ai_chance on is_historical_focus_on = yes that adds or multiplies up (26.02.1936 in a
    Japan run: Le Chonk took "rein in the military", and the Kodoha purge, the historical
    road, closed for good)."""
    m = re.search(r"\bai_chance\s*=\s*\{", ob)
    if not m:
        return False
    j, d = m.end(), 1
    while d and j < len(ob):
        d += ob[j] == "{"
        d -= ob[j] == "}"
        j += 1
    ai = ob[m.end():j]
    for mm in re.finditer(r"\bmodifier\s*=\s*\{", ai):
        k, dd = mm.end(), 1
        while dd and k < len(ai):
            dd += ai[k] == "{"
            dd -= ai[k] == "}"
            k += 1
        body = ai[mm.end():k]
        if not re.search(r"is_historical_focus_on\s*=\s*yes", body):
            continue
        a = re.search(r"\badd\s*=\s*(-?[\d.]+)", body)
        f = re.search(r"\bfactor\s*=\s*(-?[\d.]+)", body)
        if (a and float(a.group(1)) > 0) or (f and float(f.group(1)) > 1):
            return True
    return False


def definitions():
    global _DEFS
    if _DEFS is None:
        _DEFS = {}
        for f in glob.glob(os.path.join(B.game_dir(), "events", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for kind, body in _blocks(txt):
                m = re.search(r"\bid\s*=\s*([\w.\-]+)", body)
                if not m:
                    continue
                t = re.search(r"\btitle\s*=\s*([\w.\-]+)", body)
                ds = re.search(r"\bdesc\s*=\s*([\w.\-]+)", body)
                _DEFS[m.group(1)] = {"kind": kind, "title": t.group(1) if t else None,
                                     "desc": ds.group(1) if ds else None,
                                     "options": _options(body)}
    return _DEFS


def loc(key):
    global _LOC
    if _LOC is None:
        _LOC = {}
        for f in glob.glob(os.path.join(B.game_dir(), "localisation", "english", "**", "*.yml"),
                           recursive=True):
            for m in re.finditer(r'^\s*([\w.\-]+):\d*\s*"(.*)"\s*$',
                                 open(f, encoding="utf-8-sig", errors="replace").read(), re.M):
                _LOC.setdefault(m.group(1), m.group(2))
    if not key:
        return None
    s = _LOC.get(key, key)
    s = re.sub(r"§.|\\n|£\w+|\[[^\]]*\]|\$[\w|]*\$", " ", s)
    return re.sub(r"\s+", " ", s).strip()
