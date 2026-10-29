"""
Scans live memory for RTTI vtable pointers and builds a class -> objects index.

Every game object (CNationalFocusDatabase, CTechnologyDatabase, ...) starts with a
vtable pointer. RTTI gives us the vtable address of all 18,882 classes in the
binary, so scanning memory for those values tells us where each object lives.

A single pass scans ~3.8 GB, which takes a few seconds with numpy.
"""
from __future__ import annotations
import os, pickle, re, struct
import numpy as np


_rebuilding = False


class ObjectIndex:
    def __init__(self, proc, rtti_map, cache_path=None, session=None):
        self.p = proc
        # what identifies the loaded game, not just the process: loading a save re-creates
        # the managers (CCharacterManager among them), and an index from before the load
        # points at freed objects (seen 17.10: no generals at all after a reload)
        self.session = session
        self.rtti = rtti_map                 # {class_name: [vaddr,...]}
        self.cache_path = cache_path
        self._byvptr = None                  # {vptr_runtime: [addr,...]}
        self._vptr_class = {}                # {vptr_runtime: class_name}
        self._plain_keys = {}                # immutable RTTI names, never object addresses
        for cls, vps in rtti_map.items():
            self._plain_keys.setdefault(re.sub(r"^\d+", "", cls), cls)
            for v in vps:
                self._vptr_class[proc.va(v)] = cls

    # ------------------------------------------------------------------ scanning
    def build(self, force=False, max_per_class=200000, progress=None):
        if self._byvptr is not None and not force:
            return self._byvptr
        if self.cache_path and os.path.exists(self.cache_path) and not force:
            with open(self.cache_path, "rb") as f:
                data = pickle.load(f)
            if (data.get("base") == self.p.base and data.get("pid") == self.p.pid
                    and data.get("session") == self.session):
                self._byvptr = data["map"]
                return self._byvptr

        targets = np.array(sorted(self._vptr_class), dtype=np.uint64)
        lo, hi = int(targets[0]), int(targets[-1])
        out = {}
        regions = self.p.regions(writable=True)
        CH = 32 << 20
        for ri, m in enumerate(regions):
            a = m["start"]
            if progress:
                progress(ri, len(regions))
            while a < m["end"]:
                n = min(CH, m["end"] - a)
                n -= n % 8
                if n <= 0:
                    break
                buf = self.p.try_read(a, n)
                if buf and len(buf) >= 8:
                    arr = np.frombuffer(buf[: len(buf) - len(buf) % 8], dtype=np.uint64)
                    cand = np.nonzero((arr >= lo) & (arr <= hi))[0]
                    if cand.size:
                        vals = arr[cand]
                        idx = np.searchsorted(targets, vals)
                        idx = np.clip(idx, 0, targets.size - 1)
                        good = targets[idx] == vals
                        for off, v in zip(cand[good], vals[good]):
                            lst = out.setdefault(int(v), [])
                            if len(lst) < max_per_class:
                                lst.append(a + int(off) * 8)
                a += n
        self._byvptr = out
        if self.cache_path:
            with open(self.cache_path, "wb") as f:
                pickle.dump({"base": self.p.base, "pid": self.p.pid,
                             "session": self.session, "map": out}, f)
        return out

    # ------------------------------------------------------------------ queries
    def scan(self, class_names, progress=None):
        """FRESH addresses of the given classes, straight from memory, not cached.

        The disk cache is keyed by pid, so within one session it goes stale: fronts,
        war goals and battles are destroyed and re-created while the game runs, and a
        stale pointer read as an array header once asked for tens of gigabytes. Use this
        for anything the game creates and frees during play.
        """
        want = {}
        for cn in class_names:
            key = cn if re.match(r"^\d", cn) else next(
                (c for c in self.rtti if re.fullmatch(r"\d+" + re.escape(cn), c)), None)
            for v in self.rtti.get(key, []) if key else []:
                want[self.p.va(v)] = re.sub(r"^\d+", "", key)
        if not want:
            return {cn: [] for cn in class_names}
        targets = np.array(sorted(want), dtype=np.uint64)
        lo, hi = int(targets[0]), int(targets[-1])
        found = {name: [] for name in set(want.values())}
        CH = 32 << 20
        for m in self.p.regions(writable=True):
            a = m["start"]
            while a < m["end"]:
                n = min(CH, m["end"] - a)
                n -= n % 8
                if n <= 0:
                    break
                buf = self.p.try_read(a, n)
                if buf and len(buf) >= 8:
                    arr = np.frombuffer(buf[: len(buf) - len(buf) % 8], dtype=np.uint64)
                    cand = np.nonzero((arr >= lo) & (arr <= hi))[0]
                    if cand.size:
                        vals = arr[cand]
                        idx = np.clip(np.searchsorted(targets, vals), 0, targets.size - 1)
                        good = targets[idx] == vals
                        for off, v in zip(cand[good], vals[good]):
                            found[want[int(v)]].append(a + int(off) * 8)
                a += n
        return found

    def instances(self, class_name: str):
        """Either the mangled name ('22CNationalFocusDatabase') or the plain one."""
        m = self.build()
        key = class_name
        if not re.match(r"^\d", key):
            key = self._plain_keys.get(key)
            if key is None:
                return []
        res = []
        for v in self.rtti.get(key, []):
            res.extend(m.get(self.p.va(v), []))
        res = sorted(set(res))
        plain = re.sub(r"^\d+", "", key)
        if res and not _rebuilding and any(self.class_of(a) != plain for a in (res[0], res[-1])):
            return self._rebuilt(class_name)          # stale after a save was loaded
        return res

    def _rebuilt(self, class_name):
        global _rebuilding
        _rebuilding = True
        try:
            self.build(force=True)
            return self.instances(class_name)
        finally:
            _rebuilding = False

    def singleton(self, class_name: str):
        """The one live object of a class. A cached address is checked first: loading a
        save frees and re-creates managers (CCharacterManager) without changing the pid
        or the country objects, and the stale address then held garbage."""
        inst = self.instances(class_name)
        if not inst or self.class_of(inst[0]) != re.sub(r"^\d+", "", class_name):
            self.build(force=True)
            inst = self.instances(class_name)
        return inst[0] if inst else None

    def class_of(self, addr: int):
        """Resolve an object's class name from its vptr."""
        v = self.p.try_read(addr, 8)
        if not v or len(v) < 8:
            return None
        vp = struct.unpack("<Q", v)[0]
        c = self._vptr_class.get(vp)
        return re.sub(r"^\d+", "", c) if c else None

    def counts(self, pattern=""):
        m = self.build()
        out = {}
        for vp, addrs in m.items():
            c = self._vptr_class.get(vp)
            if not c:
                continue
            name = re.sub(r"^\d+", "", c)
            if pattern and pattern.lower() not in name.lower():
                continue
            out[name] = out.get(name, 0) + len(addrs)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
