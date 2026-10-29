"""Confirm ported .data/.bss globals against a running game, READ-ONLY.

Reads go through /proc/<pid>/mem: no ptrace stop, no injected call, the game keeps
running. Each candidate is checked structurally (vptr class via RTTI, array sizes,
strings it should contain), the way the bridge itself dereferences it.

usage: verify_globals.py <bridge_dir> [pid]
"""
import os
import re
import struct
import subprocess
import sys

CANDIDATES = {
    "G_GAMESTATE":       [0x44086A8],
    "G_IDLER":           [0x44085F8],
    "G_COUNTRYTAG_DB":   [0x43F3EB8],
    "G_MODIFIER_DEFS":   [0x439EAC8],
    "G_CONSOLE_MANAGER": [0x4668158, 0x46680D0, 0x4668148, 0x4668088],
    "G_DEFAULT_ALLOC":   [0x436F1B8],
    "G_TOKEN_TABLE":     [0x4668C30, 0x4668C68],
    "G_TOKEN_COUNT":     [0x4668C68, 0x4668D18],
}


class Mem:
    def __init__(self, pid):
        self.f = open("/proc/%d/mem" % pid, "rb", buffering=0)
        exe = os.path.realpath("/proc/%d/exe" % pid)
        for line in open("/proc/%d/maps" % pid):
            parts = line.rstrip("\n").split(None, 5)  # the path may contain spaces
            if len(parts) == 6 and os.path.realpath(parts[5]) == exe and int(parts[2], 16) == 0:
                self.base = int(parts[0].split("-")[0], 16)
                break

    def read(self, a, n):
        try:
            return os.pread(self.f.fileno(), n, a)
        except OSError:
            return None

    def u64(self, a):
        b = self.read(a, 8)
        return struct.unpack("<Q", b)[0] if b and len(b) == 8 else None

    def i32(self, a):
        b = self.read(a, 4)
        return struct.unpack("<i", b)[0] if b and len(b) == 4 else None

    def stdstring(self, a):
        p, n = self.u64(a), self.u64(a + 8)
        if p is None or n is None or n > 4096:
            return None
        b = self.read(p, n)
        return b.decode("utf-8", "replace") if b is not None else None


def main():
    bridge = sys.argv[1]
    sys.path[:0] = [os.path.join(bridge, "calling"), os.path.join(bridge, "research")]
    import rtti
    vt_names = {}
    for name, vps in rtti.build()["vtables"].items():
        for vp in vps:
            vt_names[vp] = name
    pid = int(sys.argv[2]) if len(sys.argv) > 2 else int(
        subprocess.check_output(["pgrep", "-x", "hoi4"]).split()[0])
    m = Mem(pid)
    B = m.base
    print("pid %d image base %#x" % (pid, B))

    def cls(ptr):
        vp = m.u64(ptr) if ptr else None
        return vt_names.get(vp - B) if vp else None

    for a in CANDIDATES["G_GAMESTATE"]:
        gs = m.u64(B + a)
        n = m.i32(gs + 0x310 + 0xC)
        alloc = m.u64(gs + 0x310 + 0x10)
        tag = m.i32(gs + 0x518)
        t2i = m.u64(gs + 0x340)
        arr = m.u64(gs + 0x310)
        c = m.u64(arr + 8 * m.i32(t2i + 4 * tag)) if t2i and arr else None
        print("G_GAMESTATE %#x: %s countries=%s player_tag_idx=%s player=%r %s alloc=+%#x" % (
            a, cls(gs), n, tag, m.stdstring(c + 0x10) if c else None, cls(c), (alloc or B) - B))
    for a in CANDIDATES["G_IDLER"]:
        idl = m.u64(B + a)
        print("G_IDLER %#x: %s country=%r paused=%s/%s" % (
            a, cls(idl), m.stdstring(idl + 0x620), m.read(idl + 0x6C1, 1), m.read(idl + 0x6C3, 1)))
    for a in CANDIDATES["G_COUNTRYTAG_DB"]:
        tdb = m.u64(B + a)
        data, n = m.u64(tdb), m.i32(tdb + 0xC)
        tags = [m.stdstring(data + i * 0x20) for i in range(6)] if data else None
        print("G_COUNTRYTAG_DB %#x: size=%s first=%s" % (a, n, tags))
    for a in CANDIDATES["G_MODIFIER_DEFS"]:
        tbl = m.u64(B + a)
        rows = [(m.stdstring(tbl + i * 0x78), m.i32(tbl + i * 0x78 + 0x6C)) for i in (0, 1, 2, 100)] if tbl else None
        print("G_MODIFIER_DEFS %#x: %s" % (a, rows))
    for a in CANDIDATES["G_CONSOLE_MANAGER"]:
        mgr = m.u64(B + a)
        data, n = (m.u64(mgr), m.i32(mgr + 0xC)) if mgr else (None, None)
        names = [m.stdstring(data + i * 0x1C8 + 0x08) for i in range(4)] if data and n and 0 < n < 2000 else None
        print("G_CONSOLE_MANAGER %#x: count=%s names=%s" % (a, n, names))
    for a in CANDIDATES["G_DEFAULT_ALLOC"]:
        print("G_DEFAULT_ALLOC %#x: object %s" % (a, cls(B + a)))
    for t in CANDIDATES["G_TOKEN_TABLE"]:
        for c in CANDIDATES["G_TOKEN_COUNT"]:
            if c - t != 0x38:
                continue
            n, base = m.i32(B + c), m.u64(B + t)
            toks = [m.stdstring(base + i * 0x20) for i in (1, 2, 3, 100)] if base else None
            print("G_TOKEN_TABLE %#x / COUNT %#x: count=%s tokens=%s" % (t, c, n, toks))


if __name__ == "__main__":
    main()
