"""Port hardcoded image addresses of Clausewitz-MCP from one HoI4 build to another.

Patch-level builds of HoI4 shift code and data in a few large blocks. Classes found by
RTTI name in both builds give (old -> new) anchor pairs: command vtables, constructors,
CanExecute and Describe slots, trigger getters, diplomatic action vtables and
constructors. Any other old address is moved by the delta of the anchors around it and
then checked against the new binary: a function address must land on a function start,
a vtable address on a vtable.

Globals in .data/.bss have too few anchors; they are reported with a candidate and must
be confirmed against the running game (read-only).

usage: port_addrs.py <bridge_dir> <pristine_src_dir> <old_cache_dir> [--json out.json]
  pristine_src_dir is an unmodified checkout, so already-ported literals are not re-read;
  old_cache_dir holds the old build's cmdsigs.pkl, cmdfields.pkl, stats.pkl,
  diploactions.pkl; the new ones are read from the bridge tree.
"""
import bisect
import json
import os
import pickle
import re
import sys

# Verified by hand against the new binary; (old, new, why).
MANUAL = [
    (0xD77A90, 0xD77320, "GetCountry: called right after lea rdi,[rsi+0x8] by 49/50 "
                         "country trigger getters; loads CGameState, +0x340, +0x310"),
]

# Hex literals that are not image addresses (constants, masks, struct offsets, flags).
NOT_ADDR = {0x3D2AAAAB, 0x40000000, 0xFFFFFFFF, 0x100000, 0x112B08, 0x112A90}


def load_bridge(bridge):
    sys.path[:0] = [os.path.join(bridge, "calling"), os.path.join(bridge, "research")]
    import config
    from elf import ELF
    from funcs import FuncIndex
    import rtti
    e = ELF(config.binary_path())
    rel, _ = rtti.all_relocs(e)
    return e, FuncIndex(e), rtti.build(), rel


def sections(e):
    out = []
    for name in (".text", ".rodata", ".data.rel.ro", ".data", ".bss"):
        s = e.sec(name)
        out.append((name, s["addr"], s["addr"] + s["size"]))
    return out


def section_of(secs, va):
    for name, lo, hi in secs:
        if lo <= va < hi:
            return name
    return None


def vt_by_class(rt):
    """Plain class name -> first vtable address, from RTTI mangled names."""
    out = {}
    for k, vps in rt["vtables"].items():
        m = re.fullmatch(r"\d+(C[A-Za-z0-9_]*)", k)
        if m:
            out[m.group(1)] = vps[0]
    return out


def collect_anchors(bridge, old_dir, rt, rel, text_lo, text_hi):
    pk = lambda *p: pickle.load(open(os.path.join(*p), "rb"))
    pairs = {}  # old -> (new, source)

    def add(o, n, src):
        if o and n:
            pairs.setdefault(o, (n, src))

    old_s, new_s = pk(old_dir, "cmdsigs.pkl"), pk(bridge, "research", "cmdsigs.pkl")
    for cls, o in old_s.items():
        n = new_s.get(cls, {})
        for key in ("ctor", "execute", "vtable"):
            add(o.get(key), n.get(key), "%s.%s" % (cls, key))

    # Describe = vtable+0xB8 on commands; old value from cmdfields, new straight from relocs.
    for cls, o in pk(old_dir, "cmdfields.pkl").items():
        d, nv = o.get("describe"), new_s.get(cls, {}).get("vtable")
        if d and nv and text_lo <= d < text_hi:
            add(d, rel.get(nv + 0xB8), "%s.describe" % cls)

    old_st, new_st = pk(old_dir, "stats.pkl"), pk(bridge, "calling", "stats.pkl")
    for name, (fo, cls) in old_st.items():
        if name in new_st:
            add(fo, new_st[name][0], "%s.getter" % cls)

    old_d, new_d = pk(old_dir, "diploactions.pkl"), pk(bridge, "research", "diploactions.pkl")
    od, nd = old_d.get("_all", {}), new_d.get("_all", {})
    for cls, o in od.items():
        n = nd.get(cls)
        if not n:
            continue
        add(o.get("vtable"), n.get("vtable"), "%s.vtable" % cls)
        oc, nc = o.get("ctors") or [], n.get("ctors") or []
        if len(oc) == len(nc):
            for i, (a, b) in enumerate(zip(oc, nc)):
                add(a["addr"], b["addr"], "%s.ctor%d" % (cls, i))

    for o, n, why in MANUAL:
        pairs[o] = (n, "manual: " + why)
    return pairs


def harvest(bridge):
    """Every hex literal >= 0x400000 in calling/ and mcp/, with where it appears."""
    found = {}
    for sub in ("calling", "mcp"):
        for root, _, files in os.walk(os.path.join(bridge, sub)):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, bridge)
                for ln, line in enumerate(open(path, encoding="utf-8"), 1):
                    code = line.split("#", 1)[0]
                    for m in re.finditer(r"\b0x[0-9A-Fa-f]{6,8}\b", code):
                        v = int(m.group(0), 16)
                        if v < 0x400000 or v in NOT_ADDR:
                            continue
                        found.setdefault(v, []).append("%s:%d" % (rel, ln))
    return found


def neighbours(anchor_list, va):
    keys = [a for a, _ in anchor_list]
    i = bisect.bisect_right(keys, va)
    lo = anchor_list[i - 1] if i > 0 else None
    hi = anchor_list[i] if i < len(anchor_list) else None
    return lo, hi


def port(bridge, src, old_dir):
    e, fi, rt, rel = load_bridge(bridge)
    secs = sections(e)
    text = e.sec(".text")
    starts = set(fi.starts)
    vt_names = {}
    for name, vps in rt["vtables"].items():
        for vp in vps:
            vt_names[vp] = name
    pairs = collect_anchors(bridge, old_dir, rt, rel, text["addr"] - 0x20000,
                            text["addr"] + text["size"])
    code_a = sorted((o, n - o) for o, (n, _) in pairs.items() if section_of(secs, n) == ".text")
    data_a = sorted((o, n - o) for o, (n, _) in pairs.items() if section_of(secs, n) != ".text")

    rows = []
    for va, where in sorted(harvest(src).items()):
        row = {"old": va, "where": where}
        if va in pairs:
            n, src = pairs[va]
            row.update(new=n, section=section_of(secs, n), ok=True, how="anchor", note=src)
            rows.append(row)
            continue
        is_code = va < text["addr"] + text["size"]
        lo, hi = neighbours(code_a if is_code else data_a, va)
        cands = sorted({d for d in (lo and lo[1], hi and hi[1]) if d is not None})
        # The section the address had in the OLD build, judged by where it lands now.
        sec = section_of(secs, va + cands[0]) if cands else None
        row.update(section=sec, how="interp lo=%s hi=%s" % (lo and hex(lo[1]), hi and hex(hi[1])),
                   span=hex((hi[0] - lo[0]) if lo and hi else 0))
        if sec == ".text":
            hits = [va + d for d in cands if va + d in starts]
            row.update(new=hits[0] if len(hits) == 1 else None,
                       ok=len(hits) == 1 and len(cands) == 1,
                       note=("" if len(hits) == 1 and len(cands) == 1 else
                             "one of %d candidates is a function start" % len(cands)
                             if len(hits) == 1 else
                             "function starts among %s: %d" % ([hex(va + d) for d in cands], len(hits))))
        elif sec == ".data.rel.ro":
            hits = [(va + d, vt_names[va + d]) for d in cands if va + d in vt_names]
            row.update(new=hits[0][0] if len(hits) == 1 else None,
                       ok=len(hits) == 1 and len(cands) == 1,
                       note=(hits[0][1] if len(hits) == 1 else
                             "not a vtable start: %s (object, or moved)" % [hex(va + d) for d in cands]))
        else:
            row.update(new=None, ok=False,
                       note="global; candidates %s, confirm at runtime" % [hex(va + d) for d in cands])
        rows.append(row)
    return rows, len(code_a), len(data_a)


def main():
    bridge, src, old_dir = sys.argv[1:4]
    rows, nc, nd = port(bridge, src, old_dir)
    print("anchors: %d code, %d data\n" % (nc, nd))
    for r in rows:
        print("%-10s -> %-10s %-13s %-3s %-30s %-6s %s | %s" % (
            hex(r["old"]), hex(r["new"]) if r.get("new") else "?", r["section"],
            "ok" if r["ok"] else "??", r["how"], r.get("span", ""), r.get("note", ""),
            ", ".join(r["where"][:2])))
    n_ok = sum(r["ok"] for r in rows)
    print("\n%d addresses, %d resolved, %d need a look" % (len(rows), n_ok, len(rows) - n_ok))
    if "--json" in sys.argv:
        json.dump(rows, open(sys.argv[sys.argv.index("--json") + 1], "w"), indent=1)


if __name__ == "__main__":
    main()
