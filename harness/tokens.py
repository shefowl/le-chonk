"""The game's token table, read live: {token id: string}.

27.10: bridge/calling/tokens.pkl was dumped on 20.10 and the ids have moved since (the
table grew from 61k to 70k, our panel mod adds its own lc_* tokens): the facility's
sp_air_axial_jet_engine read as sp_naval_escort_carrier, admirals' traits came out
shifted. Ids depend on what the game loaded, so they are read from the running game,
once per game process (one bulk read of the table, then the strings).
"""
import struct

from .bridge import module as B

_CACHE = {}


def table(g):
    p = g.p
    fm = B("fieldmap")
    n = p.i32(p.base + fm.G_TOKEN_COUNT)
    key = (p.base, n)
    if key not in _CACHE:
        base = p.u64(p.base + fm.G_TOKEN_TABLE)
        raw = p.read(base, n * 0x20)
        out = {}
        for i in range(n):
            ptr, ln = struct.unpack_from("<Qq", raw, i * 0x20)
            if ptr and 0 < ln < 256:
                try:
                    out[i] = p.read(ptr, ln).decode("utf8", "replace")
                except OSError:
                    pass
        _CACHE.clear()
        _CACHE[key] = out
    return _CACHE[key]
