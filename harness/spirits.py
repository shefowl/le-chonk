"""What removes a national spirit, from the game's own files: the national focuses and
decisions whose effects remove it (remove_ideas, swap_ideas), and for a focus the
prerequisites still to be taken before it. Knowledge of the game's mechanics, given to
the minister of state as data; which spirit to fight first is his decision (the user,
19.10: a newcomer to HoI4 ruins his first games for not knowing the mechanics).
"""
import glob
import os
import re

from . import bridge as B

_FOCUS = None       # id -> {"prereq": [[ids], ...], "removes": set(), "days": int}
_DECISION = None    # idea -> [decision keys]


def _block_end(txt, i):
    """Index just past the brace that closes the block opened at txt[i] == '{'."""
    d = 0
    n = len(txt)
    while i < n:
        c = txt[i]
        if c == "#":
            j = txt.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "{":
            d += 1
        elif c == "}":
            d -= 1
            if d == 0:
                return i + 1
        i += 1
    return n


def _removed(body):
    out = set()
    for m in re.finditer(r"\bremove_ideas\s*=\s*(\{[^}]*\}|[\w.\-]+)", body):
        out.update(re.findall(r"[\w.\-]+", m.group(1)))
    for m in re.finditer(r"\bremove_idea\s*=\s*([\w.\-]+)", body):
        out.add(m.group(1))
    return out


def focuses():
    global _FOCUS
    if _FOCUS is None:
        _FOCUS = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "national_focus", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^\s*(?:focus|shared_focus)\s*=\s*\{", txt):
                body = txt[m.end() - 1:_block_end(txt, m.end() - 1)]
                fid = re.search(r"\bid\s*=\s*([\w.\-]+)", body)
                if not fid:
                    continue
                pre = [re.findall(r"\bfocus\s*=\s*([\w.\-]+)", p)
                       for p in re.findall(r"\bprerequisite\s*=\s*\{([^}]*)\}", body)]
                cost = re.search(r"\bcost\s*=\s*([\d.]+)", body)
                _FOCUS[fid.group(1)] = {"prereq": [p for p in pre if p],
                                        "removes": _removed(body),
                                        "days": int(float(cost.group(1)) * 7) if cost else None}
    return _FOCUS


_EFFECTS = None
_TAGS = None


def scripted_effects():
    """{effect name: ideas it removes} from common/scripted_effects (23.10: the naval
    treaty is left through remove_naval_treaty_effect, not a remove_ideas of its own)."""
    global _EFFECTS
    if _EFFECTS is None:
        _EFFECTS = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "scripted_effects", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            for m in re.finditer(r"(?m)^([\w.\-]+)\s*=\s*\{", txt):
                body = txt[m.end() - 1:_block_end(txt, m.end() - 1)]
                gone = _removed(body)
                if gone:
                    _EFFECTS[m.group(1)] = gone
    return _EFFECTS


def country_tags():
    global _TAGS
    if _TAGS is None:
        _TAGS = set()
        for f in glob.glob(os.path.join(B.game_dir(), "common", "country_tags", "*.txt")):
            _TAGS.update(re.findall(r"(?m)^([A-Z][A-Z0-9]{2})\s*=",
                                    open(f, encoding="utf-8-sig", errors="replace").read()))
    return _TAGS


def _removed_deep(body):
    out = _removed(body)
    effects = scripted_effects()
    # Find referenced keys once instead of compiling thousands of regexes for every
    # decision. Overlapping matches preserve the old word-boundary suffix semantics
    # for dotted/hyphenated keys (e.g. both prefix.effect and effect).
    for match in re.finditer(r"(?=\b([\w.\-]+)\s*=\s*yes)", body):
        out.update(effects.get(match.group(1), ()))
    return out


def decisions():
    global _DECISION
    if _DECISION is None:
        _DECISION = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "decisions", "*.txt")):
            txt = open(f, encoding="utf-8-sig", errors="replace").read()
            # category = { decision = { ... } }: the decisions are the depth-1 blocks
            for cat in re.finditer(r"(?m)^[\w.\-]+\s*=\s*\{", txt):
                end = _block_end(txt, cat.end() - 1)
                inner = txt[cat.end():end - 1]
                for d in re.finditer(r"(?m)^\t[\w.\-]+\s*=\s*\{", inner):
                    key = d.group().strip().split("=")[0].strip()
                    body = inner[d.end() - 1:_block_end(inner, d.end() - 1)]
                    for idea in _removed_deep(body):
                        _DECISION.setdefault(idea, []).append(key)
    return _DECISION


def _path(fid, done, seen=None):
    """The focuses still to take before fid, in order (the first option of each OR)."""
    seen = seen or set()
    out = []
    for opts in focuses().get(fid, {}).get("prereq", []):
        if any(o in done for o in opts) or not opts or opts[0] in seen:
            continue
        seen.add(opts[0])
        out += _path(opts[0], done, seen) + [opts[0]]
    return out


def removers(ideas, done, tree=None, tag=None):
    """{idea: [text]} for our ideas in force that some focus or decision removes. `done`
    are the completed focuses, `tree` the focus ids of our own tree (others are left out:
    a remove_ideas in another country's tree is no road for us)."""
    from .events import loc
    out = {}
    for idea in ideas:
        ways = []
        for fid, f in focuses().items():
            if idea not in f["removes"] or fid in done or (tree is not None and fid not in tree):
                continue
            path = _path(fid, set(done))
            days = sum(focuses().get(x, {}).get("days") or 0 for x in path + [fid])
            ways.append((len(path), "focus %s \"%s\"%s (about %d days of focuses)" % (
                fid, loc(fid) or fid,
                (", after " + " > ".join(path)) if path else ", startable or next", days)))
        for d in decisions().get(idea, []):
            pre = d.split("_", 1)[0]
            if tag and pre in country_tags() and pre != tag:
                continue        # another country's decision (MTG_ is a DLC's, not a tag)
            from .politics import decision_terms
            terms = decision_terms(d)
            ways.append((0, "decision %s \"%s\"%s" % (
                d, loc(d) or d, (", opens when: %s" % terms[:160])
                if terms and not terms.startswith("-") else "")))
        if ways:
            out[idea] = [w for _, w in sorted(ways)[:4]]
    return out
