"""Our subjects and their autonomy (26.10, the user: lowering a subject's autonomy is
"most often useful": a colony made a puppet gives 90% of its manpower to us, its trade,
and lets us build on its land)."""
import glob
import os
import re

from . import bridge as B

_STATES = None


def states():
    """{autonomy state id: {"freedom": min level, "rules": text, "modifier": text}}."""
    global _STATES
    if _STATES is None:
        from .spirits import _block_end
        _STATES = {}
        for f in glob.glob(os.path.join(B.game_dir(), "common", "autonomous_states", "*.txt")):
            txt = re.sub(r"#[^\n]*", "", open(f, encoding="utf-8-sig", errors="replace").read())
            for m in re.finditer(r"autonomy_state\s*=\s*\{", txt):
                body = txt[m.end() - 1:_block_end(txt, m.end() - 1)]
                i = re.search(r"\bid\s*=\s*(\w+)", body)
                if not i:
                    continue

                def part(k):
                    mm = re.search(r"\b%s\s*=\s*\{" % k, body)
                    if not mm:
                        return ""
                    s = body[mm.end():_block_end(body, mm.end() - 1) - 1]
                    s = re.sub(r'desc\s*=\s*"[^"]*"', "", s)
                    return re.sub(r"\s+", " ", s).strip()
                fl = re.search(r"\bmin_freedom_level\s*=\s*([\d.]+)", body)
                _STATES[i.group(1)] = {"freedom": float(fl.group(1)) if fl else None,
                                       "rules": part("rule"), "modifier": part("modifier")}
    return _STATES


def lines(g):
    """Briefing lines: each subject, its level, whether the game lets us lower it now."""
    D = B.module("diplomacy")
    subs = D.subjects(g)
    if not subs:
        return []
    S = states()
    out = ["OUR SUBJECTS (change_autonomy lowers or raises a subject one level, 50 political "
           "power; the lower the level, the more of its manpower and trade is ours and the "
           "less it may act alone; then 30 days without another change):"]
    own = g._inj is None
    if own:
        g.attach()
    try:
        for tag, st in subs:
            ok, why = D.change_autonomy(g, tag, reduce=True, dry_run=True)
            out.append("  %s: %s%s; lowering it now: %s" % (
                tag, (st or "?").replace("autonomy_", ""),
                " (freedom from %s)" % S[st]["freedom"] if st in S and S[st]["freedom"]
                is not None else "", "allowed" if ok else "refused (%s)" % re.sub(
                    r"\bpol_power\b ?", "", why.split(" now: ", 1)[-1].replace(
                        "autonomy_texticon", "autonomy points"))[:200]))
    finally:
        if own:
            g.detach()
    seen = sorted({st for _, st in subs if st} | {"autonomy_puppet", "autonomy_integrated_puppet"},
                  key=lambda s: -(S.get(s, {}).get("freedom") or 0))
    out.append("WHAT THE LEVELS MEAN (from the game's files; rules for the subject, modifiers "
               "on it): " + "; ".join(
                   "%s: %s | %s" % (s.replace("autonomy_", ""), S[s]["rules"][:160],
                                    S[s]["modifier"][:240]) for s in seen if s in S))
    return out
