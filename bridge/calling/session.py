"""Session info: single or multiplayer, and which country is being played.

All PURE READS (process_vm_readv), the game is never stopped and there is no
desync risk in multiplayer. The one exception is `pause()`, which goes through the
player's own command channel: the same path as pressing space, subject to whatever
rules the multiplayer session enforces.
"""
import struct

G_IDLER      = 0x459E908
G_GAMESTATE  = 0x459E9F0
IDLER_COUNTRY_NAME = 0x620    # std::string, name of the played country
IDLER_LOCAL_PLAYER = 0x7D0    # std::string, who paused last (1.19.3: 0x7C0)
SESSION_NET  = 0x58           # CSession+0x58 -> network object (CProxyServer)
CLIENT_FIRST = 0x348          # first remote client name in the network object
CLIENT_STRIDE = 0x700
GS_MAJORS    = 0x328          # major powers (CCountry*)
GS_COMBATS   = 0x268          # active land battles (CLandCombat*)

IDLER_PAUSED = 0x6C1          # 1 = paused, 0 = running
IDLER_HARD   = 0x6C3          # 1 = a hard pause (set by the u8=1 command, by loading)

# CPauseGame (vtable 0x444B070). A player command, so it works in multiplayer too.
PAUSE_CTOR   = 0x21bae60      # (this, std::string* who, u8 pause, u8 flag)
PAUSE_CANEXE = 0x21BB4B0      # always true
PAUSE_SIZE   = 0x50


def _std_string(p, addr):
    try:
        ptr, ln = struct.unpack("<Qq", p.read(addr, 16))
        if 0 <= ln < 256:
            return p.read(ptr, ln).decode("utf8", "replace")
    except OSError:
        pass
    return None


def _idler_name(g):
    """The name at idler+0x7C0. NOTE: this field changes during play. It appears
    to hold whoever acted last. So it is NOT reliable as the local player name."""
    return _std_string(g.p, g.p.u64(g.p.base + G_IDLER) + IDLER_LOCAL_PLAYER)


def country_name(g):
    """The country name cached in the idler: may be stale."""
    return _std_string(g.p, g.p.u64(g.p.base + G_IDLER) + IDLER_COUNTRY_NAME)


def _my_country_name(g):
    try:
        return g.player().name
    except Exception:
        return country_name(g)


def _net_object(g):
    s = g.oi.instances("CSession")
    if not s:
        return None, None
    net = g.p.u64(s[0] + SESSION_NET)
    return net, (g.oi.class_of(net) if net else None)


def remote_players(g, max_clients=16):
    """Remote client names in the network object, std::strings 0x700 apart."""
    net, _ = _net_object(g)
    if not net:
        return []
    out = []
    for k in range(max_clients):
        n = _std_string(g.p, net + CLIENT_FIRST + k * CLIENT_STRIDE)
        if n and n.isprintable() and 1 < len(n) < 40:
            out.append(n)
    return out


def info(g):
    """The session summary to read when connecting."""
    p = g.p
    gs = p.u64(p.base + G_GAMESTATE)
    from game import _arr
    tagof = {}
    for t in g.tags():
        try:
            tagof[g.country(t).ptr] = t
        except Exception:
            pass
    try:
        majors = [tagof.get(x) for x in _arr(p, gs, GS_MAJORS)]
    except Exception:
        majors = []
    names = remote_players(g)
    extra = _idler_name(g)
    if extra and extra not in names:
        names.append(extra)
    net, netcls = _net_object(g)
    me = g.player_tag()
    try:
        import combat as _cb
        at_war = _cb.at_war(g)      # pure read; g.wars() would freeze the game
    except Exception:
        at_war = None
    try:
        combats = len(_arr(p, gs, GS_COMBATS))
    except Exception:
        combats = 0
    return {
        "my_tag": me,
        # Take the name from CCountry: idler+0x620 is a CACHE and goes stale when a
        # civil war moves the player to a different country.
        "my_country": _my_country_name(g),
        # CDummyServer is the single-player stand-in; its name slots can hold leftovers
        # (18.10, 1.19.3: ['Player', 'true'] after several save loads)
        "is_multiplayer": len(names) > 1 and netcls != "CDummyServer",
        "player_count": len(names),
        "players": sorted(names),
        "network_class": netcls,
        "majors": majors,
        "at_war": at_war,
        "active_land_combats": combats,
        "date": g.date()["text"],
        "note": ("Player names are read from the network object (CSession+0x58) at "
                 "0x700 intervals. Which of them is the local player CANNOT be told; "
                 "the country you are playing is definitive in my_tag/my_country."),
    }


def is_paused(g):
    """Is the game paused right now? PURE READ (idler+0x6C1)."""
    return bool(g.p.u8(g.p.u64(g.p.base + G_IDLER) + IDLER_PAUSED))


def pause(g, paused=True, who=None):
    """Pause or resume: the same command as the player's space key.

    NOTE: `CPauseGame` is a TOGGLE. The u8 argument does not mean "pause/resume";
    whatever is passed, the state flips. This field was once taken for a set-value,
    so `pause(False)` sometimes PAUSED the game and divisions never moved. The real
    state is now read first (idler+0x6C1) and the toggle is only sent when it
    differs, the same as pressing space once, when needed.
    """
    want = bool(paused)
    if is_paused(g) == want:
        return True, "already %s" % ("paused" if want else "running")
    if who is None and not want:
        who = _idler_name(g)      # a soft pause yields at once to the one who set it
    ok, err = _toggle(g, who, want)
    if not ok:
        return False, err
    import time as _t
    for _ in range(10):
        _t.sleep(0.05)
        if is_paused(g) == want:
            return True, ""
    return False, "toggle sent but the state did not change"


def _toggle(g, who=None, want=True):
    """Send the player's pause command once; `pause()` decides when it is needed and this
    picks the u8 that moves the game towards `want` (see the table below)."""
    p = g.p
    who = who or (_idler_name(g) or "AI")
    own = g._inj is None
    if own:
        g.attach()
    try:
        s = g.malloc(0x20)
        raw = who.encode()[:15]
        p.write(s, struct.pack("<QQ", s + 0x10, len(raw)) + raw + b"\0" * (16 - len(raw)))
        cmd = g.malloc(PAUSE_SIZE)
        p.write(cmd, b"\0" * PAUSE_SIZE)
        # +0x48 goes to the idler's pause virtual (1.19.4, 0x2191C60; 1.19.3 the same):
        #   1: no hard pause -> hard pause; a hard pause -> run (both flags cleared)
        #   0: a hard pause -> nothing; a soft pause or running -> flip, but only 30 s
        #      after the last flip (+0x810, +0x818) unless `who` is the one who paused
        # 27.10: always sending 1 left a soft pause (after a load) paused for good.
        flag = 1 if want or p.u8(p.u64(p.base + G_IDLER) + IDLER_HARD) else 0
        g.call(PAUSE_CTOR, [cmd, s, flag, 0])
        if p.u8(cmd + 0x48) != flag:
            return False, "pause flag was not written"
        if not (g.call(PAUSE_CANEXE, [cmd, 0]) & 0xFF):
            return False, "CanExecute returned false"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own:
            g.detach()
