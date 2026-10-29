"""Persistent, read-only warnings matching the game's incoming-war tooltip.

The current tooltip (0x2844E80) combines diplomacy's timed-justification list
with active national focus targets. This module never invokes its predicate
0xF11320 or the target resolver: only direct targets confirmed against our own
country are exposed. Scripted targets and failed reads leave explicit gaps.
"""
from . import bridge as B


def cached_snapshot(g, own=None):
    """Share one fresh-per-day snapshot between the alert poll and briefings."""
    own = own or g.player_tag()
    key = (g.date_raw() // 24, own)
    cached = getattr(g, "_war_threat_cache", (None, None))
    if cached[0] != key:
        cached = (key, snapshot(g, own))
        g._war_threat_cache = cached
    return cached[1]


def snapshot(g, own=None):
    """Return pairs, threat records, unresolved descriptors and coverage_errors.

    All manual pairs are retained for the watcher's existing enemy filtering.
    Foreign focus targets are included only when they resolve directly to own.
    No timer is inferred from a focus's advertised duration.
    """
    result = {"pairs": [], "threats": [], "unresolved": [], "coverage_errors": []}
    errors = result["coverage_errors"]
    own = own or g.player_tag()
    try:
        manual = B.module("diplomacy").justifications(g)
        for target, sources in manual.items():
            for source in sources:
                result["threats"].append({"from": source, "target": target,
                                          "source": "justification", "days_left": None})
    except Exception as exc:
        errors.append("manual justifications: %s" % exc)
    try:
        countries = list(g.countries())
    except Exception as exc:
        errors.append("country list: %s" % exc)
        countries = []
    own_ids = {c.tag_id for c in countries if c.tag == own}
    if not own_ids:
        errors.append("own country tag could not be resolved")
    p = g.p
    tag_remap = None

    def direct_target(key):
        nonlocal tag_remap
        if tag_remap is None:
            # 0xDA24C0 -> 0x1D1B0B0: definition tag -> current CCountryTag.
            db = p.u64(p.base + 0x458A088)
            size = p.i32(db + 0x3C)
            if not 0 < size <= 65536:
                raise ValueError("invalid country tag remap count")
            data = p.u64(db + 0x30)
            tag_remap = (data, size)
        data, size = tag_remap
        if not 0 <= key < size:
            raise ValueError("invalid focus target tag index")
        return p.i32(data + key * 4)

    for country in countries:
        if not country.ptr:
            continue
        # diplomacy.justifications deliberately swallows OSError and invalid
        # counts. Audit its source so an unreadable list cannot mean 'none'.
        try:
            status = p.u64(country.ptr + 0xF60)
            if not status:
                raise ValueError("missing diplomacy status")
            count = p.i32(status + 0xD4)
            if not 0 <= count < 256:
                raise ValueError("unsupported justification list count %s" % count)
            if count:
                p.read(p.u64(status + 0xC8), count * 4)
        except Exception as exc:
            errors.append("%s manual justifications: %s" % (country.tag, exc))
        try:
            if country.tag == own or p.i32(country.ptr + 0x484) <= 0:
                continue
            progress = p.u64(country.ptr + 0x1348)
            focus = p.u64(progress + 0x10) if progress else 0
            if not focus:
                continue
            count = p.i32(focus + 0x5CC)
            if not 0 <= count <= 1024:
                raise ValueError("invalid active focus target count %s" % count)
            if not count:
                continue
            data = p.u64(focus + 0x5C0)
            incoming = False
            for i in range(count):
                try:
                    descriptor = data + i * 16
                    key, script = p.i32(descriptor), p.u64(descriptor + 8)
                    if script:
                        result["unresolved"].append({"from": country.tag, "kind": "scripted",
                                                     "descriptor_index": i, "tag_key": key,
                                                     "script": hex(script)})
                        errors.append("%s active focus has a scripted target; direction unknown"
                                      % country.tag)
                    elif direct_target(key) in own_ids:
                        incoming = True
                except Exception as exc:
                    errors.append("%s active focus target %d: %s" % (country.tag, i, exc))
            if incoming:
                try:
                    info = g.current_focus(country.tag) or {}
                except Exception as exc:
                    info = {}
                    errors.append("%s active focus label: %s" % (country.tag, exc))
                result["threats"].append({"from": country.tag, "target": own,
                                          "source": "focus", "focus_key": info.get("key"),
                                          "focus_name": info.get("name"), "days_left": None})
        except Exception as exc:
            errors.append("%s active focus: %s" % (country.tag, exc))
    result["pairs"] = sorted({(t["from"], t["target"]) for t in result["threats"]})
    return result


def lines(report, own, enemy=None):
    """Persistent lines for supreme/army or a marshal watching ``enemy``.

    Every role sees incoming threats against own; a marshal may additionally
    see its enemy's manual justifications against other countries.
    """
    out = []
    for threat in report["threats"]:
        target, source = threat["target"], threat["from"]
        if target != own and source != enemy:
            continue
        if threat["source"] == "focus":
            focus = threat.get("focus_key") or threat.get("focus_name") or "unnamed focus"
            out.append("WAR THREAT: %s is pursuing focus %s that leads to war with US; "
                       "completion date unknown." % (source, focus))
        else:
            out.append("WAR THREAT: %s is justifying a war goal against %s; "
                       "completion date unknown." % (source, "US" if target == own else target))
    if report["coverage_errors"]:
        out.append("WAR THREAT COVERAGE: incomplete (%d unresolved targets or read errors); "
                   "absence of a warning does not establish safety." % len(report["coverage_errors"]))
    return out
