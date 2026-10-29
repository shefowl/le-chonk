"""Design presets: the game's own designs per role and frame (common/ai_equipment, the
"Presets" of the in-game designer), so a model picks "a fighter on the 1936 airframe"
instead of counting machine guns (the user, 19.10)."""
import glob
import os
import re

from . import bridge as B
from .spirits import _block_end

_PRESETS = None


def _list(body, key):
    m = re.search(r"\b%s\s*=\s*\{([^}]*)\}" % key, body)
    return re.findall(r"[\w.\-]+", m.group(1)) if m else []


def _modules(body):
    """{slot: [module, ...]}: 'slot = module' or 'slot = { any_of = { a b } }' (26.10: the
    any_of slots were dropped, and the Hurricane preset came out with an engine and no
    gun). The choices keep the file's order, the best first."""
    out, i = {}, 0
    for m in re.finditer(r"([\w.\-]+)\s*=\s*(\{|[\w.\-]+)", body):
        if m.start() < i:
            continue
        if m.group(2) == "{":
            end = _block_end(body, m.end() - 1)
            out[m.group(1)] = [x for x in re.findall(r"[\w.\-]+", body[m.end():end - 1])
                               if x not in ("any_of", "all_of", "upgrade")]
            i = end
        else:
            out[m.group(1)] = [m.group(2)]
    return out


_CATS = None


def module_categories():
    """{category: [module, ...]} from common/units/equipment/modules, newest first (a
    preset may name a category, as 'ship_anti_air': the game takes the best module of it
    that we have; 26.10 the escort preset was refused as 'no module called ship_anti_air')."""
    global _CATS
    if _CATS is None:
        _CATS = {}
        for f in sorted(glob.glob(os.path.join(B.game_dir(), "common", "units", "equipment",
                                               "modules", "*.txt"))):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\t([\w.\-]+)\s*=\s*\{", txt):
                body = txt[m.end() - 1:_block_end(txt, m.end() - 1)]
                c = re.search(r"\bcategory\s*=\s*([\w.\-]+)", body)
                if c:
                    _CATS.setdefault(c.group(1), []).append(m.group(1))
        for k in _CATS:
            _CATS[k].reverse()
    return _CATS


_UNLOCK = None


def module_unlocks():
    """{module: {tech, ...}} from common/technologies (enable_equipment_modules)."""
    global _UNLOCK
    if _UNLOCK is None:
        _UNLOCK = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "technologies", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\t([\w.\-]+)\s*=\s*\{", txt):
                body = txt[m.end() - 1:_block_end(txt, m.end() - 1)]
                for e in re.finditer(r"\benable_equipment_modules\s*=\s*\{([^}]*)\}", body):
                    for mod in re.findall(r"[\w.\-]+", re.sub(r"#[^\n]*", "", e.group(1))):
                        _UNLOCK.setdefault(mod, set()).add(m.group(1))
    return _UNLOCK


def expand(choices, researched=None):
    """{slot: [module]} with categories replaced by their modules (newest first), and,
    given our researched techs, the modules no tech of ours unlocks left out (26.10: the
    game's check let a 1937 escort take ship_anti_air_4)."""
    cats = module_categories()
    unl = module_unlocks() if researched is not None else {}
    out = {}
    for slot, cands in choices.items():
        lst = []
        for c in cands:
            lst += cats.get(c, [c]) if c not in ("empty",) else [c]
        if researched is not None:
            lst = [x for x in lst if x == "empty" or x not in unl or unl[x] & researched]
        out[slot] = lst
    return out


def presets():
    """[{name, group, roles, type, modules, history, blocked, available}]"""
    global _PRESETS
    if _PRESETS is None:
        _PRESETS = []
        for f in sorted(glob.glob(os.path.join(B.game_dir(), "common", "ai_equipment", "*.txt"))):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for g in re.finditer(r"(?m)^([\w.\-]+)\s*=\s*\{", txt):
                body = txt[g.end() - 1:_block_end(txt, g.end() - 1)]
                roles, blocked, avail = (_list(body, "roles"), _list(body, "blocked_for"),
                                         _list(body, "available_for"))
                for d in re.finditer(r"(?m)^\t([\w.\-]+)\s*=\s*\{", body):
                    db = body[d.end() - 1:_block_end(body, d.end() - 1)]
                    tv = re.search(r"\btarget_variant\s*=\s*\{", db)
                    if not tv:
                        continue
                    tvb = db[tv.end() - 1:_block_end(db, tv.end() - 1)]
                    ty = re.search(r"\btype\s*=\s*([\w.\-]+)", tvb)
                    mb = re.search(r"\bmodules\s*=\s*\{", tvb)
                    if not ty or not mb:
                        continue
                    mods = _modules(tvb[mb.end():_block_end(tvb, mb.end() - 1) - 1])
                    _PRESETS.append({"name": d.group(1), "group": g.group(1), "roles": roles,
                                     "type": ty.group(1), "modules": mods,
                                     "history": bool(re.search(r"\bhistory\s*=\s*yes", db)),
                                     "blocked": blocked, "available": avail})
    return _PRESETS


def for_frame(equipment, tag):
    """Presets for a frame type that the game offers our country."""
    return [x for x in presets() if x["type"] == equipment and tag not in x["blocked"]
            and (not x["available"] or tag in x["available"])]
