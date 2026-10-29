"""The army as its strategist sees it and orders it: manpower, divisions by template, the
training queue, the equipment the templates need. Training goes through
bridge/calling/industry.py; ready divisions are deployed by code (deploy_ready), not by
a model: there is nothing to decide once a division is trained.
"""
import collections

from . import bridge as B
from .ops import OpError

TMPL_BATTALIONS = 0x68      # CPdxArray<CSubUnitDefinition*>, combat battalions
TMPL_SUPPORT = 0x88         # CPdxArray<CSubUnitDefinition*>, support companies (0x80 was a
                            # guess and read nothing; checked 20.10 on the French templates)
SUB_NAME = 0x10
READY = 0.999               # training fraction at which a division is deployed


def _battalions(g, t, off):
    G = B.module("game")
    p = g.p
    try:
        subs = G._arr(p, t, off)
    except OSError:
        return []
    return [G.item_name(p, s + SUB_NAME) for s in subs]


def templates(g):
    """{name: battalions as 'infantry x9, artillery x1'}."""
    p = g.p
    out = {}
    for t in g.division_templates():
        ptr = int(t["ptr"], 16)
        c = collections.Counter(b for b in _battalions(g, ptr, TMPL_BATTALIONS) if b)
        out[t["name"]] = ", ".join("%s x%d" % kv for kv in c.most_common())
    return out


def picture(g):
    ind = B.module("industry")
    c = g.player()
    divs = collections.Counter(d["template"] for d in g.divisions(limit=2000)["divisions"])
    queue = collections.defaultdict(list)
    for d in ind.training(g):
        queue[d["template"]].append(d)
    stock = collections.Counter()
    for s in g.equipment_stockpile(limit=200)["stockpile"]:
        if s["equipment"]:
            stock[s["equipment"]] += s["count"]
    made = collections.Counter()
    for ln in g.production_lines():
        made[ln["equipment"]] += ln["output_per_day"]
    return {
        "manpower": c.manpower(),
        "divisions": divs.most_common(),
        "templates": templates(g),
        "training": {k: (len(v), sum(x["training"] for x in v) / len(v),
                         min(x["equipment"] for x in v)) for k, v in queue.items()},
        "stockpile": stock.most_common(30),
        "production": sorted(((k, round(v, 1)) for k, v in made.items() if v), key=lambda x: -x[1]),
    }


def alarm(g):
    """Why the army staff must act now, or None: nothing in training while there is
    manpower, divisions in no army, special forces room unused, a doctrine to buy."""
    from . import ops
    ind = B.module("industry")
    why = []
    if not ind.training(g) and g.player().manpower() > 30000:
        why.append("no division is in training and %d manpower is waiting"
                   % g.player().manpower())
    free = len(ops.unassigned(g))
    if free >= 3:
        why.append("%d divisions are in no army" % free)
    try:
        sf = special_forces(g)
        if sf["have"] + sf["training"] + 6 <= sf["cap"]:
            why.append("special forces: %d of about %d battalions (with %d in training); "
                       "the room is unused" % (sf["have"], sf["cap"], sf["training"]))
    except Exception:
        pass
    from . import doctrine_files
    for folder in ("land", "special_forces"):
        d = doctrine_files.affordable(g, folder)
        if d:
            why.append(d)
    return "; ".join(why) or None


def train(g, template, count, repeat=False, deployment_province=None):
    """Train a fresh finite batch of exactly count divisions; repeat requires opt-in."""
    ind = B.module("industry")
    t = next((x for x in g.division_templates() if x["name"] == template), None)
    if t is None:
        raise OpError("no template called %r (ours: %s)"
                      % (template, ", ".join(x["name"] for x in g.division_templates())))
    r = ind.train_batch(g, int(t["ptr"], 16), count, deployment_province, repeat=repeat)
    if not r.get("ok"):
        raise OpError(r.get("error") or "the game started no finite training batch")
    return r


def set_training_series(g, conveyor_id, series):
    return B.module("industry").set_training_series(g, conveyor_id, series)


def training_details(g):
    """Model-visible queue controls and equipment facts; no force mix is selected here."""
    from . import army_needs
    ind = B.module("industry")
    needs = army_needs.unit_needs()
    fielded = collections.Counter(d["template"] for d in
                                  g.divisions(limit=5000)["divisions"])
    active = collections.Counter(d["template"] for d in ind.training(g))
    norms, templates_ = {}, []
    for t in g.division_templates():
        full = collections.Counter()
        bats = []
        for off in (TMPL_BATTALIONS, TMPL_SUPPORT):
            subs = _battalions(g, int(t["ptr"], 16), off)
            bats += subs
            for b in subs:
                full.update(needs.get(b, {}))
        norms[t["name"]] = full
        templates_.append({"template": t["name"], "fielded": fielded[t["name"]],
                           "in_training": active[t["name"]],
                           "battalions_and_support": dict(collections.Counter(bats)),
                           "equipment_per_division": dict(full)})
    queue_full = collections.Counter()
    for name, n in active.items():
        queue_full.update({k: v * n for k, v in norms.get(name, {}).items()})
    stock, produced = collections.Counter(), collections.Counter()
    for r in g.equipment_stockpile(limit=100000)["stockpile"]:
        if r["equipment"]:
            stock[army_needs.kind(r["equipment"])] += r["count"]
    for r in g.production_lines():
        if r["equipment"]:
            produced[army_needs.kind(r["equipment"])] += r["output_per_day"]
    cvs = []
    for c in ind.conveyors(g):
        row = {k: v for k, v in c.items() if k not in ("ptr", "template_ptr")}
        row["parallel"] = c["divisions"]
        row["current_batch_full_equipment"] = {
            k: v * sum(x["active"] for x in c["lines"])
            for k, v in norms.get(c["template"], {}).items()}
        # No finite bound exists for repeating queues; current batch remains visible.
        remaining = (None if c["series"] == 0 else
                     sum(x.get("remaining", int(x["active"])) for x in c["lines"]))
        row["remaining_divisions"] = remaining
        row["remaining_full_equipment"] = (None if remaining is None else
            {k: v * remaining for k, v in norms.get(c["template"], {}).items()})
        cvs.append(row)
    return {"conveyors": cvs, "templates": templates_, "stockpile": dict(stock),
            "produced_per_day": dict(produced), "queue_full_equipment": dict(queue_full),
            "equipment_note": "Full template requirements, not a deficit: equipment already "
            "held by training divisions is included. Repeating queues have no finite bound. "
            "set_training_series with 1 finishes current divisions without future repeats; "
            "cancel_training explicitly removes selected parallel lines and returns their resources."}


CO_CAPITAL_STATE = 0xFF0
_LOCATED = set()            # training lines given a place to deploy


def home_province(g, mv):
    """The capital's city province (a core province), else its first land province."""
    cap = g.p.i32(g.player().ptr + CO_CAPITAL_STATE)
    provs = mv.land_provinces(cap)
    return next((p for p in provs if mv.city_at.get(p)), provs[0] if provs else None)


def _handle_key(g, ptr):
    return g.p.u64(ptr + 0x08)


def deploy_ready(g, mv=None):
    """Deploy every trained division. Returns the templates deployed. A training line
    deploys only where it has a place to (19.10: six divisions waited at 100% for weeks):
    each line gets the capital's province once."""
    ind = B.module("industry")
    if mv is not None:
        prov = home_province(g, mv)
        for c in ind.conveyors(g):
            key = _handle_key(g, c["ptr"])
            if c.get("deployment_province", 0) > 0:
                _LOCATED.add(key)
            if prov and key not in _LOCATED and ind.set_location(g, c["ptr"], prov).get("ok"):
                _LOCATED.add(key)
    out = []
    for d in ind.training(g):
        if d["training"] >= READY:
            if ind.deploy_line(g, d["line"]).get("ok"):
                out.append(d["template"])
    return out


def template_grids(g):
    """{template: "col1 | col2 ... ; support: ..."} as the designer shows it."""
    T = B.module("templates")
    out = {}
    for t in g.division_templates():
        r = T.read(g, t["name"])
        if r:
            W = unit_widths()
            w = sum(W.get(b, 0) for c in r["grid"] for b in c)
            out[t["name"]] = "%s; support: %s; width %g" % (
                " | ".join(" ".join(c) for c in r["grid"]), " ".join(r["support"]) or "none", w)
    return out


_WIDTHS = None

SPECIAL = {"marine", "mountaineers", "paratrooper"}
FAST = {"motorized", "mechanized", "light_armor", "medium_armor", "heavy_armor",
        "modern_armor", "amtrac"}


def below_standard(g, e=None):
    """Briefing lines: the templates we field or train that fall short of the reference
    (26.10, the user: mountaineers of 3 battalions, marines of 4, no support companies).
    A holding garrison, the HQ and screens of cavalry or armoured cars are left alone."""
    T = B.module("templates")
    W = unit_widths()
    using = {}
    if e:
        using.update({n: c for n, c in e.get("divisions", [])})
        for n, (c, _, _) in (e.get("training") or {}).items():
            using[n] = using.get(n, 0) + c
    out = []
    for t in g.division_templates():
        name = t["name"]
        r = T.read(g, name)
        if not r or (e and not using.get(name)):
            continue
        bats = [b for c in r["grid"] for b in c]
        kinds = set(bats)
        if not bats or any(b.startswith("hq_") for b in bats) or "garrison" in name.lower() \
                or kinds <= {"cavalry", "armored_car"}:
            continue
        w = sum(W.get(b, 0) for b in bats)
        need = 12 if kinds & {"paratrooper"} else 18
        short = []
        if w < need:
            short.append("width %g (the standard is %d-24)" % (w, need))
        if len(r["support"]) < 2:
            short.append("%d support companies (engineer and at least one more)"
                         % len(r["support"]))
        if short:
            kind = ("special forces" if kinds & SPECIAL else "fast" if kinds & FAST
                    else "line")
            out.append("%s (%s; %s divisions): %s" % (name, kind, using.get(name, "?"),
                                                      ", ".join(short)))
    if not out:
        return []
    return ["TEMPLATES BELOW THE STANDARD (army experience goes here first; edit_template, "
            "see REFERENCE TEMPLATES): " + "; ".join(out)]


def unit_widths():
    """{land battalion key: combat width} from common/units (support companies: 0)."""
    global _WIDTHS
    if _WIDTHS is None:
        import glob
        import os
        import re
        _WIDTHS = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "units", "*.txt")):
            s = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"\n\t(\w+)\s*=\s*\{", s):
                body = s[m.end():m.end() + 4000]
                end = re.search(r"\n\t\}", body)
                body = body[:end.start()] if end else body
                w = re.search(r"\n\t\tcombat_width\s*=\s*([\d.]+)", body)
                if w:
                    _WIDTHS[m.group(1)] = float(w.group(1))
    return _WIDTHS


_LAND = None


def _land_groups():
    """{land sub-unit key: its group} from common/units (air and naval have none)."""
    global _LAND
    if _LAND is None:
        import glob, os, re
        from .spirits import _block_end
        _LAND = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "units", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\t([\w.\-]+)\s*=\s*\{", txt):
                body = txt[m.end() - 1:_block_end(txt, m.end() - 1)]
                gr = re.search(r"\bgroup\s*=\s*([\w]+)", body)
                if gr:
                    _LAND[m.group(1)] = gr.group(1)
    return _LAND


def battalion_types(g):
    """Land battalion keys and support company keys of our country, as "line: ...;
    support: ..." (the game refuses the locked ones)."""
    T = B.module("templates")
    land = _land_groups()
    ours = [k for k in sorted(T.subunit_defs(g)) if k in land]
    line = [k for k in ours if land[k] != "support"]
    sup = [k for k in ours if land[k] == "support"]
    return ["line: " + " ".join(line), "support companies: " + " ".join(sup)]


def remove_template(g, name):
    """Delete a template no division uses (26.10: "Marine Landing Division", an infantry
    clone made in 1936 while marines were not researched, never raised a division)."""
    T = B.module("templates")
    used = sum(1 for d in g.divisions(limit=5000)["divisions"] if d.get("template") == name)
    if used:
        raise OpError("%d divisions still use %s" % (used, name))
    r = T.remove(g, name)
    if not r.get("ok"):
        raise OpError(r.get("error") or "refused")
    return {"removed": name}


def edit_template(g, name, columns, support, new_name=None):
    """Change a template (or, with new_name, make a new one from it): columns of battalion
    keys, support company keys. Costs army experience, the designer's own price."""
    T = B.module("templates")
    columns = [[str(x) for x in c] for c in columns if c]
    support = [str(x) for x in support or []]
    r = (T.create(g, name, new_name, columns, support) if new_name
         else T.update(g, name, columns, support))
    if not r.get("ok"):
        raise OpError(r.get("error") or "the game refused the template")
    import time
    time.sleep(0.5)
    r["template"] = T.read(g, new_name or name)
    return r


def cancel_training(g, template, count, conveyor_id=None):
    """Take up to `count` divisions of a template out of training, the least trained first
    (19.10: Le Chonk ordered 14 Brigade Coloniale stopped eight times; nobody could)."""
    ind = B.module("industry")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise OpError("count must be a positive integer")
    selected = None
    if conveyor_id is not None:
        selected = next((c["ptr"] for c in ind.conveyors(g)
                         if c["id"] == int(conveyor_id)), None)
        if selected is None:
            raise OpError("no training conveyor with id %s" % conveyor_id)
    lines = sorted((d for d in ind.training(g) if d["template"] == template
                    and (selected is None or d["conveyor"] == selected)),
                   key=lambda d: d["training"])
    if not lines:
        raise OpError("no %s in training" % template)
    attempted, error = set(), None
    for d in lines[:count]:
        attempted.add(d["line"])
        result = ind.remove_line(g, d["line"])
        if not result.get("ok"):
            error = result.get("error", "remove command refused")
            break
    import time
    time.sleep(0.5)
    after = [d for d in ind.training(g) if d["template"] == template]
    done = len(attempted - {d["line"] for d in after})
    result = {"ok": error is None and done == len(attempted), "cancelled": done,
              "still_training": len(after)}
    if not result["ok"]:
        result["error"] = error or "training cancellation not confirmed; inspect queue before retrying"
    return result


_UNIT_MP = {}


def unit_manpower():
    """{battalion or company key: manpower} from the game's unit files."""
    if not _UNIT_MP:
        import glob, os, re
        d = os.path.expanduser("~/.local/share/Steam/steamapps/common/Hearts of Iron IV/"
                               "common/units")
        for f in glob.glob(d + "/*.txt"):
            s = open(f, encoding="utf-8-sig", errors="ignore").read()
            for m in re.finditer(r"^\t(\w+)\s*=\s*\{(.*?)^\t\}", s, re.S | re.M):
                n = re.search(r"\bmanpower\s*=\s*(\d+)", m.group(2))
                if n:
                    _UNIT_MP[m.group(1)] = int(n.group(1))
    return _UNIT_MP


def template_manpower(g):
    """{template: manpower}. A division's experience is stored as its level (0..1) times
    this (21.10: start_experience_factor 0.3 read 3600 on a 12000-man alpine division)."""
    import re
    mp = unit_manpower()
    return {t: sum(mp.get(k, 0) for k in re.findall(r"[a-z_]+", v))
            for t, v in template_grids(g).items()}


_SF = None


def special_units():
    """Battalion keys with special_forces = yes in the game's unit files."""
    global _SF
    if _SF is None:
        import glob, os, re
        _SF = set()
        for f in glob.glob(os.path.join(B.game_dir(), "common", "units", "*.txt")):
            unit = None
            for ln in open(f, encoding="utf-8-sig", errors="replace"):
                m = re.match(r"^\t([A-Za-z0-9_]+)\s*=\s*\{", ln)
                if m:
                    unit = m.group(1)
                elif unit and re.match(r"^\s*special_forces\s*=\s*yes", ln):
                    _SF.add(unit)
    return _SF


def special_forces(g):
    """Special forces battalions in our divisions and in training against the cap: the
    other battalions x SPECIAL_FORCES_CAP_BASE (0.05), at least SPECIAL_FORCES_CAP_MIN
    (24); a country modifier (doctrine, focus) may raise it, which is not read here."""
    import collections
    sf = special_units()
    ind = B.module("industry")
    tm, kinds = {}, {}
    for t in g.division_templates():
        bats = [b for b in _battalions(g, int(t["ptr"], 16), TMPL_BATTALIONS) if b]
        tm[t["name"]] = (sum(b in sf for b in bats), sum(b not in sf for b in bats))
        kinds[t["name"]] = collections.Counter(sf_kind(b) for b in bats if b in sf)
    have = other = 0
    by_have, by_train = collections.Counter(), collections.Counter()
    for d in g.divisions(limit=2000)["divisions"]:
        s, o = tm.get(d["template"], (0, 0))
        have += s
        other += o
        by_have.update(kinds.get(d["template"], {}))
    queued = 0
    for d in ind.training(g):
        queued += tm.get(d["template"], (0, 0))[0]
        by_train.update(kinds.get(d["template"], {}))
    cap = max(24, int(other * 0.05))
    # 23.10: by kind, so the staff weighs the mix against the war plan (it filled the
    # whole room with paratroopers while the plan's landings wanted marines)
    return {"have": have, "training": queued, "cap": cap,
            "by_kind": {k: (by_have[k], by_train[k]) for k in set(by_have) | set(by_train)},
            "templates": {k: v[0] for k, v in tm.items() if v[0]}}


def sf_kind(unit):
    if unit.startswith(("marine", "amphibious")):
        return "marines"
    if "paratrooper" in unit:
        return "paratroopers"
    if "mountain" in unit:
        return "mountaineers"
    return unit


_NEEDS = {}
GENS = ("light", "medium", "heavy", "modern")


def unit_needs():
    """{battalion key: {equipment archetype: count}} from the game's unit files."""
    if not _NEEDS:
        import glob, os, re
        for f in glob.glob(os.path.join(B.game_dir(), "common", "units", "*.txt")):
            unit, inneed = None, False
            for ln in open(f, encoding="utf-8-sig", errors="replace"):
                m = re.match(r"^\t([A-Za-z0-9_]+)\s*=\s*\{", ln)
                if m:
                    unit, inneed = m.group(1), False
                elif unit and re.match(r"^\s*need\s*=\s*\{", ln):
                    inneed = True
                elif inneed:
                    m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(\d+)", ln)
                    if m:
                        _NEEDS.setdefault(unit, {})[m.group(1)] = int(m.group(2))
                    elif "}" in ln:
                        inneed = False
    return _NEEDS


def outdated_templates(g, made):
    """Templates whose battalions need a chassis of an older generation than one our
    lines make (the user, 23.10: light tanks to medium, then to modern):
    [(template, old battalion, old chassis, new battalion, new chassis)]."""
    import re
    needs = unit_needs()
    arch = {re.sub(r"_\d+$", "", k) for k in made}
    by_need = {}
    for u, n in needs.items():
        for e in n:
            by_need.setdefault(e, u)
    out = []
    for t in g.division_templates():
        bats = {b for b in _battalions(g, int(t["ptr"], 16), TMPL_BATTALIONS) if b}
        for b in sorted(bats):
            for e in needs.get(b, {}):
                m = re.match(r"(%s)_(.+)$" % "|".join(GENS), e)
                if not m or e in arch:
                    continue
                newer = [gn for gn in GENS[GENS.index(m.group(1)) + 1:]
                         if "%s_%s" % (gn, m.group(2)) in arch]
                if newer:
                    ne = "%s_%s" % (newer[-1], m.group(2))
                    if by_need.get(ne):
                        out.append((t["name"], b, e, by_need[ne], ne))
    return out
