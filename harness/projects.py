"""Special projects: facilities (programs), the projects they work on, the scientists.

Found live on 22.10 (Japan 1936, the naval facility at Kure): see docs/04-navy.md.
- country +0x15E0: CPdxArray<NProject::CProgram*>, one per facility. CProgram: +0x08
  handle {83,id}, +0x18 province, +0x20 building key (naval_facility...), +0x50 token of
  the project it works on (0 = idle), +0x58 handle {73,id} of its scientist.
- country +0x1690: the country's projects inline, stride 0x1B8. CProject: +0x08 handle
  {86,id}, +0x14 token of its name (sp_naval_super_heavy_battleship...).
- a scientist is a character with a CScientist at +0xF8.
Commands, CanExecute at vtable slot 10:
  CStartProjectCommand 0x27939D0 (this, CProgram*, CProject*)
  CAttachScientistCommand 0x2792E70 (this, CProgram*, CCharacter* scientist, 1)
"""
import glob
import os
import re
import struct
import time

from .bridge import module as B

CO_PROGRAMS = 0x15E0
CO_PROJECTS = 0x1690
PROJECT_STRIDE = 0x1B8
PJ_TOKEN = 0x14
PG_PROVINCE, PG_BUILDING, PG_PROJECT, PG_SCIENTIST = 0x18, 0x20, 0x50, 0x58
CH_SCIENTIST = 0xF8
START_CTOR = 0x27939D0
ATTACH_CTOR = 0x2792E70

_TOKENS = None
_FILES = None


def _tokens(g):
    from .tokens import table
    return table(g)


def _files():
    """{project: {specialization, available}} from common/special_projects."""
    global _FILES
    if _FILES is None:
        from .bridge import game_dir
        _FILES = {}
        for f in glob.glob(os.path.join(game_dir(), "common", "special_projects", "projects",
                                        "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^(sp_\w+) = \{", txt):
                body = txt[m.end():m.end() + 6000]
                spec = re.search(r"specialization = (\w+)", body)
                # the first has_tech is the expansions' branch (has_dlc "Man the Guns")
                techs = re.findall(r"has_tech = (\w+)", body.split("complexity")[0])
                _FILES[m.group(1)] = {"specialization": spec.group(1) if spec else "?",
                                      "needs_tech": techs[0] if techs else None}
    return _FILES


def _projects(g):
    p = g.p
    c = g.player().ptr
    dp = p.u64(c + CO_PROJECTS)
    n = p.i32(c + CO_PROJECTS + 0xC)
    tok = _tokens(g)
    return {tok.get(p.i32(dp + i * PROJECT_STRIDE + PJ_TOKEN)): dp + i * PROJECT_STRIDE
            for i in range(n)}


_SKILLS = None


def _scientist_skills():
    """{character key: {specialization: level}} from common/characters."""
    global _SKILLS
    if _SKILLS is None:
        from .bridge import game_dir
        _SKILLS = {}
        for f in glob.glob(os.path.join(game_dir(), "common", "characters", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\t(\w+) = \{", txt):
                body = txt[m.end():m.end() + 4000]
                nxt = re.search(r"(?m)^\t\w+ = \{", body)
                body = body[:nxt.start()] if nxt else body
                sc = re.search(r"scientist = \{.*?skills = \{([^}]*)\}", body, re.S)
                if sc:
                    _SKILLS[m.group(1)] = {k: int(v) for k, v in
                                           re.findall(r"(\w+) = (\d+)", sc.group(1))}
    return _SKILLS


def scientists(g):
    """[{key, name, ptr, skills}] of our scientists."""
    p = g.p
    G = B("game")
    sk = _scientist_skills()
    out = []
    for ch in g.all_characters():
        if not p.u64(ch + CH_SCIENTIST):
            continue
        key = G.item_name(p, ch + G.CH_KEY)
        out.append({"key": key, "name": G.item_name(p, ch + G.CH_NAME), "ptr": ch,
                    "handle": struct.unpack("<ii", p.read(ch + 8, 8)),
                    "skills": sk.get(key, {})})
    return out


def programs(g):
    """[{id, ptr, facility, province, project, scientist}] of our facilities."""
    p = g.p
    G = B("game")
    tok = _tokens(g)
    sci = {s["handle"]: s["name"] for s in scientists(g)}
    out = []
    for pg in G._arr(p, g.player().ptr, CO_PROGRAMS):
        t = p.i32(pg + PG_PROJECT)
        out.append({"id": p.i32(pg + 0xC), "ptr": pg,
                    "facility": G.item_name(p, pg + PG_BUILDING),
                    "province": p.i32(pg + PG_PROVINCE),
                    "project": tok.get(t) if t else None,
                    "scientist": sci.get(struct.unpack("<ii", p.read(pg + PG_SCIENTIST, 8)))})
    return out


def _post(g, ctor, args):
    api = B("api")
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        api.allow_call(ctor)
        cmd = g.malloc(0x40)
        p.write(cmd, b"\0" * 0x40)
        g.call(ctor, [cmd] + list(args))
        ce = p.u64(p.u64(cmd) + B("offsets").VT_CANEXECUTE) - p.base
        api.allow_call(ce)
        if not g.call(ce, [cmd, 0]) & 0xFF:
            return False
        g.cmd.post(cmd)
        return True
    finally:
        if own:
            g.detach()


def start(g, facility_id, project):
    pg = next((x for x in programs(g) if x["id"] == int(facility_id)), None)
    if pg is None:
        return {"error": "no facility %s" % facility_id}
    pj = _projects(g).get(project)
    if pj is None:
        return {"error": "no project %s" % project}
    if not _post(g, START_CTOR, [pg["ptr"], pj]):
        return {"error": "the game refused %s here (not available yet, or the facility "
                         "is of another kind)" % project}
    time.sleep(0.5)
    return {"facility": facility_id,
            "project": next(x["project"] for x in programs(g) if x["id"] == int(facility_id))}


def attach(g, facility_id, scientist_key):
    pg = next((x for x in programs(g) if x["id"] == int(facility_id)), None)
    if pg is None:
        return {"error": "no facility %s" % facility_id}
    s = next((x for x in scientists(g) if x["key"] == scientist_key or
              (x["name"] or "").lower() == str(scientist_key).lower()), None)
    if s is None:
        return {"error": "no scientist %s" % scientist_key}
    if not _post(g, ATTACH_CTOR, [pg["ptr"], s["ptr"], 1]):
        return {"error": "the game refused the scientist here"}
    time.sleep(0.5)
    return {"facility": facility_id,
            "scientist": next(x["scientist"] for x in programs(g) if x["id"] == int(facility_id))}


# the facility of each specialization (common/buildings/00_buildings.txt); 27.10, the
# user: the air facility stood idle with no scientist for years (only the Admiralty had
# the tools, and it saw the air facility as none of its business), no land facility at all
KIND = {"naval_facility": "specialization_naval", "air_facility": "specialization_air",
        "land_facility": "specialization_land", "nuclear_facility": "specialization_nuclear"}
COST, COST_EACH = 10000, 6000      # base_cost, per_controlled_building_extra_cost


def cost_now(g):
    return COST + COST_EACH * len(programs(g))


def lines(g, specialization=None):
    """Briefing lines: our facilities of that specialization, its projects, the scientists."""
    files = _files()
    pgs = [x for x in programs(g) if not specialization or KIND.get(x["facility"]) == specialization]
    out = ["SPECIAL PROJECT FACILITIES (id, kind, province, project, scientist): " + ("; ".join(
        "%d %s p%d: %s, scientist %s" % (x["id"], x["facility"], x["province"],
                                          x["project"] or "IDLE", x["scientist"] or "NONE")
        for x in pgs) or "none")]
    if specialization and not pgs:
        b = next(k for k, v in KIND.items() if v == specialization)
        out[0] += (". WE HAVE NO %s: its projects open only with one. The industry builds it "
                   "(build %s in a state of ours; %d construction now, about %d military "
                   "factories' worth); ask Le Chonk for it if our war needs those projects"
                   % (b.upper(), b, cost_now(g), cost_now(g) // 7200))
    have = set(g.researched())
    rows = []
    for name in sorted(files):
        f = files[name]
        if specialization and f["specialization"] != specialization:
            continue
        need = f["needs_tech"]
        rows.append("%s%s" % (name, "" if not need or need in have else " (needs %s)" % need))
    if rows:
        out.append("SPECIAL PROJECTS%s: %s" % (" (%s)" % specialization if specialization else "",
                                                "; ".join(rows)))
    busy = {x["scientist"]: x["id"] for x in programs(g) if x["scientist"]}
    sc = sorted(scientists(g), key=lambda s: (s["name"] in busy,
                                               -s["skills"].get(specialization or "", 0)))
    out.append("SCIENTISTS (specializations at the start; a project goes fastest with a "
               "scientist of its specialization; a facility with no scientist does nothing): "
               + "; ".join("%s %s, %s" % (
                   s["name"], ", ".join("%s %d" % (k.replace("specialization_", ""), v)
                                        for k, v in s["skills"].items()) or "?",
                   "at facility %d" % busy[s["name"]] if s["name"] in busy else "FREE")
                   for s in sc))
    return out


def stuck(g, specialization):
    """What holds the facilities of this specialization still, or None: a project of
    another specialization (27.10: sp_air_axial_jet_engine at the naval facility, x0.00
    with a naval scientist), no scientist, or idle with a free scientist who fits."""
    files = _files()
    busy = {x["scientist"] for x in programs(g) if x["scientist"]}
    free = [s["name"] for s in scientists(g) if s["name"] not in busy
            and s["skills"].get(specialization)]
    out = []
    for x in programs(g):
        if KIND.get(x["facility"]) != specialization:
            continue
        spec = files.get(x["project"] or "", {}).get("specialization")
        if x["project"] and spec and spec != specialization:
            out.append("facility %d works on %s, a %s project: it does not move there (x0); "
                       "start_special_project a %s one in its place" % (
                           x["id"], x["project"], spec.replace("specialization_", ""),
                           specialization.replace("specialization_", "")))
        elif not x["scientist"] and free:
            out.append("facility %d has no scientist and %s %s free: assign_scientist" % (
                x["id"], ", ".join(free), "is" if len(free) == 1 else "are"))
        elif x["scientist"] and not x["project"]:
            out.append("facility %d with %s stands IDLE: start_special_project" % (
                x["id"], x["scientist"]))
    return "; ".join(out) or None


def facilities_line(g):
    """For the industry: which facilities we have and what a new one costs."""
    have = [KIND.get(x["facility"], x["facility"]).replace("specialization_", "")
            for x in programs(g)]
    miss = [v.replace("specialization_", "") for v in KIND.values()
            if v.replace("specialization_", "") not in have]
    return ("SPECIAL PROJECT FACILITIES: we have %s; none for %s. A new one costs %d "
            "construction (about %d military factories' worth; each one makes the next dearer); "
            "build it (build land_facility, air_facility, naval_facility (coast) or "
            "nuclear_facility) only when Le Chonk or a staff asks for its projects."
            % (", ".join(have) or "none", ", ".join(miss) or "nothing", cost_now(g),
               cost_now(g) // 7200))


def tools(word):
    from .agents import tool
    return [tool("start_special_project", "Start a %s special project (SPECIAL PROJECTS) at "
                 "our %s facility (by its id); a project without a scientist does not "
                 "advance." % (word, word),
                 {"facility": {"type": "integer"}, "project": {"type": "string"}},
                 ["facility", "project"]),
            tool("assign_scientist", "Put a FREE scientist (SCIENTISTS) on our %s facility."
                 % word, {"facility": {"type": "integer"}, "scientist": {"type": "string"}},
                 ["facility", "scientist"])]


def handle(gw, fn, a, specialization):
    """The two tools, for the staff of one specialization: its own facilities only."""
    mine = gw.run(lambda g: {x["id"]: x["facility"] for x in programs(g)})
    fid = int(a["facility"])
    if fid not in mine:
        return {"error": "no facility %s" % fid}
    if KIND.get(mine[fid]) != specialization:
        return {"error": "facility %d is a %s: not yours" % (fid, mine[fid])}
    if fn == "start_special_project":
        spec = _files().get(a["project"], {}).get("specialization")
        if spec and spec != specialization:
            return {"error": "%s is a %s project: at a %s facility it does not move (the "
                             "scientist counts as inadequate, x0)" % (
                                 a["project"], spec.replace("specialization_", ""), mine[fid])}
        return gw.run(start, fid, a["project"])
    return gw.run(attach, fid, a["scientist"])
