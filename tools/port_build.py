"""Port every hardcoded HoI4 image address from one build to the next, automatically.

usage: port_build.py OLD_HOI4 NEW_HOI4 [--apply] [--out cache-<ver>/port.json]

Needs both ELF files (an old build: Steam console `download_depot 394360 394363 <manifest>`,
the manifest id is in ~/.local/share/Steam/depotcache/394363_*.manifest).

1. Functions. Both builds' function starts come from .eh_frame_hdr. Each function is hashed
   with its relative operands masked (call/jmp rel32, rip-relative disp32), so a function
   whose code did not change hashes the same wherever it moved. Hashes unique in both
   builds are anchors; a function between two anchors is matched by its position when
   both builds hold the same number of functions there. Matched but not identical
   functions are reported as CHANGED: their arguments or the fields they touch may differ.
2. Data. Every rip-relative operand of an identical function pair gives old target -> new
   target. A data address takes the delta of an operand that points exactly at it, or else
   of the operands on both sides of it when they agree. Vtables also match by RTTI name.
3. The source. Hex literals inside the old image in harness/, bridge/calling/, bridge/mcp/
   and the addresses inside bridge/calling/*.pkl are mapped; --apply rewrites them and
   offsets.py's VERSION, BUILD_ID and section bounds. Anything unresolved is listed and
   stays as it was.
"""
import bisect
import collections
import hashlib
import json
import os
import pickle
import re
import struct
import sys

import numpy as np

ROOT = os.environ.get("PORT_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge", "research"))
from elf import ELF            # noqa: E402

SRC_DIRS = ["harness", "bridge/calling", "bridge/mcp", "bridge/clausewitz.py"]
# literals in the image range that are not addresses
NOT_ADDR = {0x3D2AAAAB, 0x40000000, 0xFFFFFFFF, 0x100000, 0x112B08, 0x112A90, 0x1000000,
            0x7FFFFFFF, 0x186A0, 0xFFFFFF}
SECS = (".text", ".rodata", ".data.rel.ro", ".data", ".bss", ".eh_frame_hdr", ".eh_frame",
        ".gcc_except_table", ".tbss", ".tdata", ".init_array", ".fini_array", ".got",
        ".got.plt", ".plt", ".plt.got", ".plt.sec", ".init", ".fini")


def build_id(e):
    s = e.sections.get(".note.gnu.build-id")
    d = e.data[s["off"]:s["off"] + s["size"]]
    nsz, dsz, _ = struct.unpack_from("<III", d, 0)
    o = 12 + ((nsz + 3) & ~3)
    return d[o:o + dsz].hex()


def version_of(path):
    try:
        j = json.load(open(os.path.join(os.path.dirname(path), "launcher-settings.json")))
        return j.get("rawVersion")
    except (OSError, ValueError):
        return None


class Image:
    def __init__(self, path):
        self.e = e = ELF(path)
        self.path = path
        self.bid = build_id(e)
        t = e.sec(".text")
        self.tlo, self.thi = t["addr"], t["addr"] + t["size"]
        self.text = np.frombuffer(e.data, np.uint8, t["size"], t["off"])
        self.starts = self._starts()
        gap = np.diff(np.append(self.starts, self.thi))
        self.sizes = np.minimum(self.lengths, gap)
        self.secs = [(n, e.sec(n)["addr"], e.sec(n)["addr"] + e.sec(n)["size"])
                     for n in SECS if n in e.sections and e.sec(n)["addr"]]
        self.lo = min(lo for _, lo, _ in self.secs)
        self.hi = max(hi for _, _, hi in self.secs)

    def _starts(self):
        """Function starts and their true lengths (FDE pc_range), from .eh_frame_hdr."""
        e = self.e
        s = e.sec(".eh_frame_hdr")
        d, o = e.data, s["off"]
        assert d[o + 3] == 0x3B, "unexpected .eh_frame_hdr table encoding"
        cnt_off = o + 8 if d[o + 1] == 0x1B else o + 4
        n = struct.unpack_from("<I", d, cnt_off)[0]
        tbl = np.frombuffer(d, np.int32, 2 * n, cnt_off + 4).reshape(n, 2).astype(np.int64)
        st = tbl[:, 0] + s["addr"]
        fde = tbl[:, 1] + s["addr"]                       # FDE vaddrs
        ef = e.sec(".eh_frame")
        fo = fde - ef["addr"] + ef["off"]
        rng = np.array([struct.unpack_from("<I", d, int(x) + 12)[0] for x in fo], np.int64)
        keep = (st >= self.tlo) & (st < self.thi)
        st, rng = st[keep], rng[keep]
        order = np.argsort(st, kind="stable")
        st, rng = st[order], rng[order]
        u = np.concatenate([[True], st[1:] != st[:-1]])
        self.lengths = rng[u]
        return st[u]

    def section(self, va):
        for n, lo, hi in self.secs:
            if lo <= va < hi:
                return n
        return None

    def owner(self, va):
        i = bisect.bisect_right(self.starts, va) - 1
        return int(self.starts[i]) if i >= 0 else None

    def disp_positions(self):
        """Offsets (into .text) of every 4-byte relative operand, found without decoding:
        after an E8/E9 byte, or after a ModRM byte with mod=00 rm=101 (rip-relative).
        Over-masking is harmless: both builds mask the same bytes where code is equal."""
        t = self.text
        call = np.nonzero((t == 0xE8) | (t == 0xE9))[0] + 1
        rip = np.nonzero((t & 0xC7) == 0x05)[0] + 1
        pos = np.unique(np.concatenate([call, rip]))
        return pos[pos + 4 <= len(t)], call[call + 4 <= len(t)]

    def masked(self, pos):
        m = self.text.copy()
        for k in range(4):
            m[pos + k] = 0
        return m

    def disp_at(self, pos):
        t = self.text.astype(np.int64)
        v = t[pos] | (t[pos + 1] << 8) | (t[pos + 2] << 16) | (t[pos + 3] << 24)
        return np.where(v >= 1 << 31, v - (1 << 32), v)


def hashes(img, masked):
    out = []
    base = img.tlo
    mb = masked.tobytes()
    for s, n in zip(img.starts, img.sizes):
        o = int(s) - base
        out.append(hashlib.blake2b(mb[o:o + int(n)], digest_size=12).digest())
    return out


def match_functions(old, new, ho, hn):
    """old start -> (new start, 'same'|'changed')"""
    co, cn = collections.Counter(ho), collections.Counter(hn)
    new_at = {h: int(s) for h, s in zip(hn, new.starts) if cn[h] == 1}
    anchors = [(int(s), new_at[h]) for h, s in zip(ho, old.starts)
               if co[h] == 1 and h in new_at]
    # keep the anchors that run in the same order in both builds (longest chain)
    anchors.sort()
    ys = [b for _, b in anchors]
    prev, idx = [-1] * len(ys), []
    for i, y in enumerate(ys):
        j = _bis(ys, idx, y)
        if j == len(idx):
            idx.append(i)
        else:
            idx[j] = i
        prev[i] = idx[j - 1] if j else -1
    chain, k = [], idx[-1] if idx else -1
    while k >= 0:
        chain.append(anchors[k])
        k = prev[k]
    chain.reverse()
    fmap = {a: (b, "same") for a, b in chain}
    oi = {int(s): i for i, s in enumerate(old.starts)}
    ni = {int(s): i for i, s in enumerate(new.starts)}
    for (a1, b1), (a2, b2) in zip(chain, chain[1:]):
        i1, i2, j1, j2 = oi[a1], oi[a2], ni[b1], ni[b2]
        if i2 - i1 == j2 - j1 and i2 - i1 > 1:
            for d in range(1, i2 - i1):
                a, b = int(old.starts[i1 + d]), int(new.starts[j1 + d])
                fmap[a] = (b, "same" if ho[i1 + d] == hn[j1 + d] else "changed")
    return fmap, len(chain)


def _bis(ys, idx, y):
    lo, hi = 0, len(idx)
    while lo < hi:
        m = (lo + hi) // 2
        if ys[idx[m]] < y:
            lo = m + 1
        else:
            hi = m
    return lo


def data_pairs(old, new, fmap, rip_o, rip_n):
    """(old target, new target) of every relative operand in identical function pairs."""
    so, sn = set(rip_o.tolist()), set(rip_n.tolist())
    olds, news = [], []
    for a, (b, how) in fmap.items():
        if how != "same":
            continue
        i = bisect.bisect_left(old.starts, a)
        n = int(old.sizes[i])
        oa, ob = a - old.tlo, b - new.tlo
        lo = bisect.bisect_left(rip_o, oa)
        hi = bisect.bisect_left(rip_o, oa + n)
        for p in rip_o[lo:hi]:
            q = int(p) - oa + ob
            if q in sn:
                olds.append(int(p))
                news.append(q)
    olds, news = np.array(olds), np.array(news)
    to = old.tlo + olds + 4 + old.disp_at(olds)
    tn = new.tlo + news + 4 + new.disp_at(news)
    keep = (to >= old.lo) & (to < old.hi) & (tn >= new.lo) & (tn < new.hi)
    return to[keep], tn[keep]


def map_data(va, so, sn):
    """New address of an old data address (so/sn: old and new operand targets, sorted by
    old) and how sure: an operand aimed exactly at it (or a few bytes before: a field of a
    global), or operands on both sides that agree."""
    for k in (0, 1, 2, 4, 8, 16):
        lo, hi = np.searchsorted(so, va - k), np.searchsorted(so, va - k, side="right")
        if hi > lo:
            deltas = collections.Counter((sn[lo:hi] - so[lo:hi]).tolist())
            d, n = deltas.most_common(1)[0]
            return va + d, "exact%s %d/%d" % ("" if not k else " -%d" % k, n, hi - lo)
    i = int(np.searchsorted(so, va))
    if 0 < i < len(so):
        d1, d2 = sn[i - 1] - so[i - 1], sn[i] - so[i]
        if d1 == d2:
            return va + int(d1), "bracketed %#x..%#x" % (so[i - 1], so[i])
    return None, "unresolved"


def operand_table(img, pos):
    """(sorted target, owner function) of every relative operand, and per-operand target."""
    tgt = img.tlo + pos + 4 + img.disp_at(pos)
    owner = img.starts[np.maximum(np.searchsorted(img.starts, img.tlo + pos, side="right") - 1, 0)]
    keep = (tgt >= img.lo) & (tgt < img.hi)
    return pos[keep], tgt[keep], owner[keep]


def _targets(img, op, f):
    pos, tgt = op[0], op[1]
    i = bisect.bisect_left(img.starts, f)
    lo = np.searchsorted(pos, f - img.tlo)
    hi = np.searchsorted(pos, f - img.tlo + int(img.sizes[i]))
    return tgt[lo:hi]


_CS = None


def mnemonics(img, f):
    global _CS
    if _CS is None:
        import capstone
        _CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    i = bisect.bisect_left(img.starts, f)
    code = img.e.read_va(f, int(img.sizes[i]))
    return [ins.mnemonic for ins in _CS.disasm(code, f)]


MASKED = {}


def twins(img, cands):
    """The first of the candidates when all are byte-identical (masked): the complete and
    base-object copies of one constructor, interchangeable."""
    if len(cands) < 2:
        return None
    m = MASKED[img.path]
    seen = set()
    for g in cands:
        i = bisect.bisect_left(img.starts, g)
        o = g - img.tlo
        seen.add(bytes(m[o:o + int(img.sizes[i])]))
    return min(cands) if len(seen) == 1 else None


def norm_ops(img, f, mapper):
    """f's instructions as text, every rip-relative or branch target put through mapper
    (old build: into the new one; new build: as is), so equal code reads equal."""
    global _CS
    if _CS is None:
        import capstone
        _CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    i = bisect.bisect_left(img.starts, f)
    code = img.e.read_va(f, int(img.sizes[i]))
    out = []
    for ins in _CS.disasm(code, f):
        op = ins.op_str
        m = re.search(r"\[rip ([+-]) (0x[0-9a-f]+)\]", op)
        if m:
            d = int(m.group(2), 16) * (1 if m.group(1) == "+" else -1)
            t = mapper(ins.address + ins.size + d)
            op = op[:m.start()] + "[%s]" % (hex(t) if t else "?") + op[m.end():]
        elif ins.mnemonic in ("call", "jmp") and op.startswith("0x"):
            t = int(op, 16)
            if not (f <= t < f + int(img.sizes[i])):
                t2 = mapper(t)
                op = hex(t2) if t2 else "?"
            else:
                op = "+%#x" % (t - f)
        elif ins.mnemonic.startswith("j") and op.startswith("0x"):
            op = "+%#x" % (int(op, 16) - f)
        out.append(ins.mnemonic + " " + op)
    return out


def best_by_operands(f, old, new, cands, tmap):
    """The candidate whose instructions, operands included, equal f's; byte-for-byte
    equal twins are interchangeable, the lowest is taken."""
    import difflib
    mo = norm_ops(old, f, tmap)
    sc = sorted(((difflib.SequenceMatcher(None, mo, norm_ops(new, g, lambda t: t),
                                          autojunk=False).ratio(), -g) for g in cands),
                reverse=True)
    if not sc:
        return None, "no candidate"
    r1, g1 = sc[0]
    r2 = sc[1][0] if len(sc) > 1 else 0.0
    why = "operands %.2f (next %.2f of %d)" % (r1, r2, len(sc))
    if r1 >= 0.9 and (r1 - r2 >= 0.02 or r1 == 1.0):
        return -g1, why + (", identical twin taken" if r1 == r2 else "")
    if r1 >= 0.5 and r1 - r2 >= 0.1:
        # a changed base class (CCommand in 1.19.4) lowers every score; the margin decides
        return -g1, why + ", LOW CONFIDENCE"
    return None, "weak " + why


def best_by_code(f, old, new, cands, floor=0.85, margin=0.08):
    """The candidate whose instruction sequence is most like f's, if clearly so."""
    import difflib
    mo = mnemonics(old, f)
    sc = sorted(((difflib.SequenceMatcher(None, mo, mnemonics(new, g), autojunk=False)
                  .ratio(), g) for g in cands), reverse=True)
    if not sc:
        return None, "no candidate"
    r1, g1 = sc[0]
    r2 = sc[1][0] if len(sc) > 1 else 0.0
    why = "code %.2f (next %.2f of %d)" % (r1, r2, len(sc))
    if r1 >= floor and r1 - r2 >= margin:
        return int(g1), why
    return None, "weak " + why


def neighbours(f, old, new, known, span=8, cap=40):
    """New functions between the images of f's nearest known neighbours."""
    i = bisect.bisect_left(old.starts, f)
    lo = hi = None
    for d in range(1, span + 1):
        if lo is None and i - d >= 0 and int(old.starts[i - d]) in known:
            lo = (d, known[int(old.starts[i - d])])
        if hi is None and i + d < len(old.starts) and int(old.starts[i + d]) in known:
            hi = (d, known[int(old.starts[i + d])])
    if lo is None and hi is None:
        return []
    if lo and hi:
        a = bisect.bisect_right(new.starts, lo[1])
        b = bisect.bisect_left(new.starts, hi[1])
    elif lo:
        a = bisect.bisect_right(new.starts, lo[1]) + max(0, lo[0] - 1 - span)
        b = a + 2 * span
    else:
        b = bisect.bisect_left(new.starts, hi[1])
        a = max(0, b - 2 * span)
    return [int(x) for x in new.starts[a:b][:cap]]


def fingerprint_match(f, old, new, opo, opn, opn_pos, tmap, opo_t):
    """The new function whose operands hit the new images of most of f's operand targets:
    candidates come from the rare targets (a vtable, a string), the score counts all
    (a common callee too). -> (new start, why) or (None, why)."""
    back = {}
    for t in set(_targets(old, opo, f).tolist()):
        m = tmap(int(t))
        if m is not None:
            back[m] = int(t)
    mapped = set(back)
    if not mapped:
        return None, "no mapped operand"
    st, own = opn
    cands = set()
    for t in mapped:
        a, b = np.searchsorted(st, t), np.searchsorted(st, t, side="right")
        if 0 < b - a <= 400:
            cands.update(own[a:b].tolist())
    if not cands:
        return None, "no candidate"
    i = bisect.bisect_left(old.starts, f)
    size = int(old.sizes[i])
    scored = []
    for g in cands:
        hit = len(mapped & set(_targets(new, opn_pos, int(g)).tolist()))
        j = bisect.bisect_left(new.starts, g)
        scored.append((hit, -abs(int(new.sizes[j]) - size), int(g)))
    scored.sort(reverse=True)
    (n, ds, g), n2 = scored[0], (scored[1][0] if len(scored) > 1 else 0)
    why = "fingerprint %d/%d (next %d, size %+d)" % (n, len(mapped), n2, -ds)
    if n >= max(2, (len(mapped) + 1) // 2) and n > n2:
        return g, why
    if n >= 1 and n == n2:
        # a tie: a constructor, its Clone and its destructor share the vtable
        tied = [c for h, _, c in scored if h == n]
        g2, why2 = best_by_operands(f, old, new, tied, tmap)
        if g2 is not None:
            return g2, why + ", tie broken by " + why2
        # a family of look-alikes (one per mission type): the k-th function referencing
        # the rarest shared target in the old build is the k-th one in the new
        for t in sorted(mapped, key=lambda t: np.searchsorted(st, t, side="right")
                        - np.searchsorted(st, t)):
            a_, b_ = np.searchsorted(st, t), np.searchsorted(st, t, side="right")
            nn = sorted(set(own[a_:b_].tolist()))
            ot, oo = opo_t
            a2, b2 = np.searchsorted(ot, back[t]), np.searchsorted(ot, back[t], side="right")
            on = sorted(set(oo[a2:b2].tolist()))
            if len(nn) == len(on) > 1 and f in on and nn[on.index(f)] in tied:
                return int(nn[on.index(f)]), why + ", tie broken by rank %d/%d" % (
                    on.index(f) + 1, len(on))
        g2, why2 = best_by_code(f, old, new, tied, floor=0.7, margin=0.05)
        if g2 is not None:
            return g2, why + ", tie broken by " + why2
        twin = twins(new, tied)
        if twin is not None:
            return twin, why + ", tie between byte-identical twins (C1/C2 copies)"
    return None, "weak " + why


def rtti_vtables(img):
    sys.path.insert(0, os.path.join(ROOT, "bridge", "calling"))
    import rtti
    e = img.e
    rel, sym = rtti.all_relocs(e)
    ro = e.sec(".rodata")
    ti = {}
    for slot, (sname, typ, add) in sym.items():
        if sname in rtti.TI_VTABLE_SYMS:
            p = rel.get(slot + 8)
            if p and ro["addr"] <= p < ro["addr"] + ro["size"]:
                nm = e.cstr(p, 400)
                if nm:
                    ti[slot] = nm
    vt = collections.defaultdict(list)
    for slot, add in rel.items():
        if add in ti:
            vt[ti[add]].append(slot + 8)
    tinfo = {nm: slot for slot, nm in ti.items()}
    return vt, tinfo, rel


def _pair_gaps(pairs, n_old, n_new):
    """Index pairs (i, j) plus every equal-length gap between them, paired by position."""
    out = list(pairs)
    edges = [(-1, -1)] + sorted(pairs) + [(n_old, n_new)]
    for (i1, j1), (i2, j2) in zip(edges, edges[1:]):
        if i2 - i1 == j2 - j1:
            out.extend((i1 + d, j1 + d) for d in range(1, i2 - i1))
    return out


def vtable_pairs(old, new, vt_o, vt_n, rel_o, rel_n, fmap):
    """(old function, new function) from vtables matched by class name. The slots are
    aligned on the identical function pairs they hold (a virtual added or removed shifts
    the slots after it), and the slots between two aligned ones pair by position."""
    import difflib

    def slots(img, rel, v):
        out, i = [], 0
        while True:
            x = rel.get(v + 8 * i)
            if x is None or not (img.tlo <= x < img.thi):
                return out
            out.append(x)
            i += 1
    a, b, skipped = [], [], 0
    for nm, vs in vt_o.items():
        ws = vt_n.get(nm)
        if not ws or len(ws) != len(vs):
            continue
        for x, y in zip(sorted(vs), sorted(ws)):
            so, sn = slots(old, rel_o, x), slots(new, rel_n, y)
            ko = [fmap[f][0] if fmap.get(f, (0, ""))[1] == "same" else ("?", i)
                  for i, f in enumerate(so)]
            sm = difflib.SequenceMatcher(None, ko, sn, autojunk=False)
            anch = [(bl.a + k, bl.b + k) for bl in sm.get_matching_blocks()
                    for k in range(bl.size)]
            if len(so) != len(sn):
                skipped += 1
            for i, j in _pair_gaps(anch, len(so), len(sn)):
                a.append(so[i])
                b.append(sn[j])
    return np.array(a, np.int64), np.array(b, np.int64), skipped


VT_LINE = re.compile(r'"(\w+)":(\s*)\(r"([^"]+)",(\s*)(0x[0-9A-Fa-f]+)\)')


def slot_map(old, new, fmap, vt_o, vt_n, rel_o, rel_n, rx):
    """old slot offset -> Counter(new slot offset) over the aligned vtables of a family."""
    import difflib

    def slots(img, rel, v):
        out, i = [], 0
        while True:
            x = rel.get(v + 8 * i)
            if x is None or not (img.tlo <= x < img.thi):
                return out
            out.append(x)
            i += 1
    votes = collections.defaultdict(collections.Counter)
    for nm, vs in vt_o.items():
        if not re.fullmatch(rx, nm) or nm not in vt_n:
            continue
        so, sn = slots(old, rel_o, vs[0]), slots(new, rel_n, vt_n[nm][0])
        ko = [fmap[f][0] if fmap.get(f, (0, ""))[1] == "same" else ("?", i)
              for i, f in enumerate(so)]
        sm = difflib.SequenceMatcher(None, ko, sn, autojunk=False)
        anch = [(bl.a + k, bl.b + k) for bl in sm.get_matching_blocks() for k in range(bl.size)]
        for i, j in _pair_gaps(anch, len(so), len(sn)):
            votes[8 * i][8 * j] += 1
    return votes


def port_slots(old, new, fmap, vt_o, vt_n, rel_o, rel_n, apply):
    """offsets.VT_SLOTS: every virtual slot we call, re-derived for the new build."""
    p = os.path.join(ROOT, "bridge", "calling", "offsets.py")
    s = open(p, encoding="utf-8").read()
    changes, doubts = {}, []
    for m in VT_LINE.finditer(s):
        name, rx, off = m.group(1), m.group(3), int(m.group(5), 16)
        v = slot_map(old, new, fmap, vt_o, vt_n, rel_o, rel_n, rx)[off]
        if not v:
            doubts.append("%s: no aligned vtable" % name)
            continue
        (to, n), tot = v.most_common(1)[0], sum(v.values())
        if n * 4 < tot * 3:
            doubts.append("%s: +%#x -> +%#x only %d/%d" % (name, off, to, n, tot))
            continue
        print("  slot %-22s +%#x -> +%#x (%d/%d vtables)" % (name, off, to, n, tot))
        if to != off:
            changes[m.start(5)] = (m.end(5), "%#x" % to)
    for d in doubts:
        print("  SLOT UNSURE " + d)
    if apply and changes:
        for st in sorted(changes, reverse=True):
            en, txt = changes[st]
            s = s[:st] + txt.upper().replace("0X", "0x") + s[en:]
        open(p, "w", encoding="utf-8").write(s)
    return changes, doubts


def symbols(img):
    """name -> address of the function symbols in .dynsym (and .symtab if present)."""
    e, out = img.e, {}
    for tab, strs in ((".dynsym", ".dynstr"), (".symtab", ".strtab")):
        if tab not in e.sections or strs not in e.sections:
            continue
        t, st = e.sec(tab), e.sec(strs)
        for i in range(t["size"] // 24):
            nm, info, _, shndx, val, size = struct.unpack_from("<IBBHQQ", e.data,
                                                              t["off"] + 24 * i)
            if (info & 0xF) == 2 and img.tlo <= val < img.thi:
                o = st["off"] + nm
                out[e.data[o:e.data.index(b"\0", o)].decode("utf-8", "replace")] = val
    return out


# ------------------------------------------------------------------ the source
LIT = re.compile(r"\b0[xX][0-9A-Fa-f]{6,9}\b")


def source_files():
    for d in SRC_DIRS:
        p = os.path.join(ROOT, d)
        if os.path.isfile(p):
            yield p
            continue
        for dp, _, fs in os.walk(p):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if f.endswith((".py", ".json")):
                    yield os.path.join(dp, f)


def pkl_files():
    d = os.path.join(ROOT, "bridge", "calling")
    return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".pkl")]


def ints_in(obj, out):
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        out.add(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            ints_in(k, out)
            ints_in(v, out)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            ints_in(v, out)


def remap(obj, m):
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return m.get(obj, obj)
    if isinstance(obj, dict):
        return type(obj)((remap(k, m), remap(v, m)) for k, v in obj.items()) \
            if not isinstance(obj, collections.defaultdict) else obj
    if isinstance(obj, list):
        return [remap(v, m) for v in obj]
    if isinstance(obj, tuple):
        return tuple(remap(v, m) for v in obj)
    if isinstance(obj, (set, frozenset)):
        return type(obj)(remap(v, m) for v in obj)
    return obj


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    old, new = Image(args[0]), Image(args[1])
    ver = version_of(args[1]) or new.bid[:8]
    print("old %s (%d functions), new %s %s (%d functions)" % (
        old.bid[:12], len(old.starts), ver, new.bid[:12], len(new.starts)))

    rip_o, call_o = old.disp_positions()
    rip_n, call_n = new.disp_positions()
    MASKED[old.path], MASKED[new.path] = old.masked(rip_o), new.masked(rip_n)
    ho, hn = hashes(old, MASKED[old.path]), hashes(new, MASKED[new.path])
    fmap, nanch = match_functions(old, new, ho, hn)
    same = sum(1 for _, h in fmap.values() if h == "same")
    print("functions: %d anchors, %d matched (%d identical, %d changed), %d unmatched" % (
        nanch, len(fmap), same, len(fmap) - same, len(old.starts) - len(fmap)))
    to, tn = data_pairs(old, new, fmap, rip_o, rip_n)
    order = np.argsort(to, kind="stable")
    order = (to[order], tn[order])
    print("data: %d operand pairs" % len(to))
    vt_o, ti_o, rel_o = rtti_vtables(old)
    vt_n, ti_n, rel_n = rtti_vtables(new)
    va_, vb_, vskip = vtable_pairs(old, new, vt_o, vt_n, rel_o, rel_n, fmap)
    print("vtable slots: %d pairs (%d vtables changed length, aligned)" % (len(va_), vskip))
    print("virtual slots (offsets.VT_SLOTS):")
    port_slots(old, new, fmap, vt_o, vt_n, rel_o, rel_n, apply)
    sym_o, sym_n = symbols(old), symbols(new)
    by_sym = {v: sym_n[k] for k, v in sym_o.items() if k in sym_n}
    print("symbols: %d named functions in both" % len(by_sym))
    allo = np.concatenate([order[0], va_])
    alln = np.concatenate([order[1], vb_])
    o2 = np.argsort(allo, kind="stable")
    order = (allo[o2], alln[o2])
    vt_rev = {}
    for nm, vs in vt_o.items():
        if nm in vt_n and len(vs) == len(vt_n[nm]):
            for a, b in zip(sorted(vs), sorted(vt_n[nm])):
                vt_rev[a] = (b, nm)
    for a, (b, nm) in list(vt_rev.items()):
        vt_rev.setdefault(a - 0x10, (b - 0x10, nm + " start"))
        vt_rev.setdefault(a - 0x8, (b - 0x8, nm + " typeinfo slot"))
    for nm, slot in ti_o.items():
        if nm in ti_n:
            vt_rev.setdefault(slot, (ti_n[nm], "typeinfo " + nm))

    new_starts = set(int(x) for x in new.starts)
    known = {a: b for a, (b, h) in fmap.items() if h == "same"}
    known.update(by_sym)
    r_ = old.e.sec(".rodata")
    skip = {r_["addr"], r_["addr"] + r_["size"]}

    def xref_delta(f):
        so, sn = order
        lo, hi = np.searchsorted(so, f), np.searchsorted(so, f, side="right")
        if hi > lo:
            c = collections.Counter((sn[lo:hi] - so[lo:hi]).tolist())
            return c.most_common(1)[0][0]
        return None

    po, to_all, own_o = operand_table(old, rip_o)
    pn, tn_all, own_n = operand_table(new, rip_n)
    o3 = np.argsort(tn_all, kind="stable")
    opn = (tn_all[o3], own_n[o3])
    opo = (po, to_all, own_o)
    opn_pos = (pn, tn_all)
    o4 = np.argsort(to_all, kind="stable")
    opo_t = (to_all[o4], own_o[o4])

    def tmap(t):
        """An old operand target in the new build, only when known for sure."""
        if t in vt_rev:
            return vt_rev[t][0]
        so, sn = order
        lo, hi = np.searchsorted(so, t), np.searchsorted(so, t, side="right")
        if hi > lo:
            c = collections.Counter((sn[lo:hi] - so[lo:hi]).tolist())
            d, n = c.most_common(1)[0]
            if n * 2 > hi - lo:
                return t + d
        fm = fmap.get(t)
        if fm and fm[1] == "same":
            return fm[0]
        return None

    def resolve(va):
        if va in (old.tlo, old.thi) or va in skip:
            return None, "section bound"
        sec = old.section(va)
        if sec is None:
            return None, "outside the image"
        if sec == ".text":
            if va in by_sym:
                return by_sym[va], "symbol"
            f = old.owner(va)
            fm = fmap.get(f)
            if fm and fm[1] == "same":
                return fm[0] + (va - f), "function" if va == f else \
                    "inside function %#x+%#x" % (f, va - f)
            # the calls, pointers and vtable slots aimed exactly at it from identical code
            so, sn = order
            lo, hi = np.searchsorted(so, va), np.searchsorted(so, va, side="right")
            if hi > lo:
                c = collections.Counter((sn[lo:hi] - so[lo:hi]).tolist())
                d, n = c.most_common(1)[0]
                if n * 2 > hi - lo and ((va + d) in new_starts or va != f):
                    agree = "" if not fm else (" (position agrees)" if fm[0] == va + d
                                               else " (position said %#x)" % fm[0])
                    return va + d, "xref %d/%d%s" % (n, hi - lo, agree)
            g, why = fingerprint_match(f, old, new, opo, opn, opn_pos, tmap, opo_t)
            if g is not None:
                agree = "" if not fm else (" (position agrees)" if fm[0] == g
                                           else " (position said %#x)" % fm[0])
                if va == f:
                    return g, why + agree
                if fm and fm[0] == g and int(old.sizes[bisect.bisect_left(old.starts, f)]) \
                        == int(new.sizes[bisect.bisect_left(new.starts, g)]):
                    return g + (va - f), "inside %#x+%#x, %s, same length" % (f, va - f, why)
                return None, "inside %#x+%#x (function -> %#x by %s), body CHANGED" % (
                    f, va - f, g, why)
            if va != f and fm:
                d = xref_delta(f)
                if d is not None:
                    return va + d, "inside function %#x matched by xref, body CHANGED" % f
                return None, "inside changed function %#x" % f
            if va == f:
                nb = neighbours(f, old, new, known)
                g3, why3 = best_by_operands(f, old, new, nb, tmap)
                if g3 is None:
                    g3, why3 = best_by_code(f, old, new, nb)
                if g3 is not None:
                    agree = "" if not fm else (" (position agrees)" if fm[0] == g3
                                               else " (position said %#x)" % fm[0])
                    return g3, "neighbours, " + why3 + agree
                why += "; " + why3
            if fm:
                return fm[0], "function by position only (%s), body CHANGED" % why
            return None, "function %#x unmatched: %s" % (f, why)
        # vtable or typeinfo by RTTI name, at any slot of it
        for base in (va, ):
            if base in vt_rev:
                return vt_rev[base][0], "rtti " + vt_rev[base][1]
        nb = max((a for a in vt_rev if a <= va < a + 0x800), default=None)
        r, how = map_data(va, *order)
        if r is None and nb is not None:
            return vt_rev[nb][0] + (va - nb), "rtti %s +%#x" % (vt_rev[nb][1], va - nb)
        return r, how

    # ---- inventory
    lits = collections.defaultdict(list)          # value -> [(file, line)]
    in_code = set()                               # values used outside comments/docstrings
    for f in source_files():
        doc = False
        for i, line in enumerate(open(f, encoding="utf-8", errors="replace"), 1):
            q = line.count('"""') + line.count("'''")
            for m in LIT.finditer(line):
                v = int(m.group(0), 16)
                if old.lo <= v < old.hi and v not in NOT_ADDR:
                    lits[v].append((os.path.relpath(f, ROOT), i))
                    if not doc and "#" not in line[:m.start()] and not q:
                        in_code.add(v)
            if q % 2:
                doc = not doc
    pkl_ints = {}
    for f in pkl_files():
        s = set()
        try:
            ints_in(pickle.load(open(f, "rb")), s)
        except Exception as ex:          # noqa: BLE001
            print("  cannot read %s: %s" % (f, ex))
            continue
        pkl_ints[f] = {v for v in s if old.lo <= v < old.hi and v not in NOT_ADDR}
    allv = set(lits) | set().union(*pkl_ints.values())
    print("addresses: %d literals in source, %d in pickles, %d distinct" % (
        len(lits), sum(len(v) for v in pkl_ints.values()), len(allv)))

    res = {}
    for v in sorted(allv):
        res[v] = resolve(v)
    pkl_all = set().union(*pkl_ints.values())
    bad = {v: r for v, r in res.items() if r[0] is None and (v in in_code or v in pkl_all)}
    notes = {v: r for v, r in res.items() if r[0] is None and v not in bad}
    print("unresolved in comments only (left as they were): %d" % len(notes))
    changed = {v: r for v, r in res.items() if r[0] is not None and "CHANGED" in r[1]}
    print("resolved %d, unresolved %d, in changed functions %d" % (
        len(res) - len(bad), len(bad), len(changed)))
    for v, (_, why) in sorted(bad.items()):
        print("  UNRESOLVED %#x %s  %s" % (v, why, lits.get(v, [("pkl", 0)])[:2]))
    for v, (n, why) in sorted(changed.items()):
        print("  CHANGED    %#x -> %#x %s  %s" % (v, n, why, lits.get(v, [("pkl", 0)])[:2]))

    out = os.path.join(ROOT, "cache-%s" % ver, "port.json")
    for a in sys.argv[1:]:
        if a.startswith("--out="):
            out = a[6:]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump({"old": old.bid, "new": new.bid, "version": ver,
               "map": {"%#x" % v: ["%#x" % r[0] if r[0] is not None else None, r[1],
                                   ["%s:%d" % x for x in lits.get(v, [])[:4]]]
                       for v, r in res.items()}}, open(out, "w"), indent=1)
    print("report:", os.path.relpath(out, ROOT))
    if not apply:
        return
    m = {v: r[0] for v, r in res.items() if r[0] is not None}
    files = sorted({f for locs in lits.values() for f, _ in locs})
    n_lit = 0
    for rel in files:
        p = os.path.join(ROOT, rel)
        s = open(p, encoding="utf-8").read()

        def sub(mo):
            nonlocal n_lit
            v = int(mo.group(0), 16)
            if v not in m or v in NOT_ADDR:
                return mo.group(0)
            n_lit += 1
            h = mo.group(0)[2:]
            nv = "%X" % m[v] if any(c in "ABCDEF" for c in h) or h.isdigit() else "%x" % m[v]
            return mo.group(0)[:2] + nv
        s2 = LIT.sub(sub, s)
        if s2 != s:
            open(p, "w", encoding="utf-8").write(s2)
    # the scanner tables are rebuilt on the new binary, not remapped; the old ones are kept
    # to compare the command layouts
    import shutil
    oldver = re.search(r'^VERSION = "([^"]*)"', open(os.path.join(
        ROOT, "bridge", "calling", "offsets.py")).read(), re.M).group(1)
    keep = os.path.join(ROOT, "cache-%s" % oldver, "pkl")
    os.makedirs(keep, exist_ok=True)
    for f in pkl_ints:
        shutil.copy2(f, keep)
    off = os.path.join(ROOT, "bridge", "calling", "offsets.py")
    s = open(off, encoding="utf-8").read()
    s = re.sub(r'^VERSION = "[^"]*"', 'VERSION = "%s"' % ver, s, flags=re.M)
    s = re.sub(r'^BUILD_ID = "[0-9a-f]+"', 'BUILD_ID = "%s"' % new.bid, s, flags=re.M)
    t, r = new.e.sec(".text"), new.e.sec(".rodata")
    s = re.sub(r"^TEXT_LO, TEXT_HI .*$", "TEXT_LO, TEXT_HI     = %#X, %#X" % (
        t["addr"], t["addr"] + t["size"]), s, flags=re.M).replace("0X", "0x")
    s = re.sub(r"^RODATA_LO, RODATA_HI .*$", "RODATA_LO, RODATA_HI = %#X, %#X" % (
        r["addr"], r["addr"] + r["size"]), s, flags=re.M).replace("0X", "0x")
    open(off, "w", encoding="utf-8").write(s)
    print("applied: %d literals in %d files, offsets.py -> %s; old tables in %s" % (
        n_lit, len(files), ver, os.path.relpath(keep, ROOT)))
    regenerate(keep)


REGEN = ("cmdauto", "cmdctor", "stats")


def regenerate(keep):
    """Rebuild the scanner tables on the new binary (in a fresh interpreter, so the
    rewritten literals are the ones imported) and report the command layouts that moved."""
    import subprocess
    code = ("import sys; sys.path[:0] = ['bridge/calling', 'bridge/research']\n"
            "import config, rtti, funcs, elf\n"
            "rtti.build(); funcs.function_starts(elf.ELF(config.binary_path()))\n"
            "import %s\n" % ", ".join(REGEN) +
            "".join("%s.build(force=True)\n" % m for m in REGEN))
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                       text=True)
    print("tables rebuilt" if r.returncode == 0 else "TABLE REBUILD FAILED:\n" + r.stderr[-2000:])
    fm = os.path.join(ROOT, "bridge", "calling", "fieldmaps.pkl")
    if os.path.exists(fm):
        os.remove(fm)          # serializer field maps: rebuilt from the running game
    for name, key in (("cmdfields.pkl", "fields"), ("cmdctors.pkl", "fields")):
        try:
            a = pickle.load(open(os.path.join(keep, name), "rb"))
            b = pickle.load(open(os.path.join(ROOT, "bridge", "calling", name), "rb"))
        except (OSError, EOFError):
            continue
        moved = [k for k in a if k in b and a[k].get(key) != b[k].get(key)]
        argc = ["%s %s->%s" % (k, a[k]["args"], b[k]["args"]) for k in a
                if k in b and "args" in a[k] and a[k]["args"] != b[k].get("args")]
        if argc:
            print("  CONSTRUCTORS WITH A NEW ARGUMENT COUNT (pass every argument, a missing "
                  "one is whatever the register held): " + "; ".join(argc))
        gone = [k for k in a if k not in b]
        print("%s: %d classes, %d with moved fields, %d gone%s" % (
            name, len(b), len(moved), len(gone), (": e.g. %s" % ", ".join(moved[:6]))
            if moved else ""))


if __name__ == "__main__":
    main()
