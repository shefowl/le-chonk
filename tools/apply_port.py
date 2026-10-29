"""Rewrite Clausewitz-MCP's hardcoded 1.19.2 addresses to 1.19.3 (Linux, build-id a4971c9e).

Every entry was resolved by tools/port_addrs.py (anchors) or tools/constellation.py
(groups), and the ones marked * were additionally confirmed by disassembly or, for
globals, by reading the running game (tools/verify_globals.py). Literals are replaced
case-preserving in calling/ and mcp/; research/ and FINDINGS.md stay as 1.19.2 history.

usage: apply_port.py <bridge_dir>
"""
import os
import re
import sys

MAP = {
    # ---- globals, all * read back from the running game
    0x43F2648: 0x44086A8,  # G_GAMESTATE -> 17CCurrentGameState, 440 countries
    0x43F2598: 0x44085F8,  # G_IDLER -> 12CInGameIdler
    0x43DDE68: 0x43F3EB8,  # G_COUNTRYTAG_DB, 440 tags
    0x4388AC8: 0x439EAC8,  # G_MODIFIER_DEFS, MODIFIER_NONE...
    0x4652018: 0x4668158,  # G_CONSOLE_MANAGER, 383 commands
    0x43591B8: 0x436F1B8,  # G_DEFAULT_ALLOC -> 22CPdxNewDeleteAllocator
    0x4652AF0: 0x4668C30,  # G_TOKEN_TABLE
    0x4652B28: 0x4668C68,  # G_TOKEN_COUNT (70129)
    # ---- country getters
    0xD77A90: 0xD77320,    # * GetCountry: loads CGameState, +0x340, +0x310
    0xD36470: 0xD38840,    # * GetStability: reads +0x10A8
    0xD378D0: 0xD39CA0,    # * GetWarSupport: reads +0x10B0
    0xD78310: 0xD77BA0,    # GetCountryName, same unit as GetCountry (-0x770)
    0xD78240: 0xD77AD0,    # CCountryTag -> name helper (cmdauto), same unit
    0xD1FE30: 0xD220D0,    # * GetTechnologyStatus: mov rax,[rdi+0xF38]; called by CCanResearchTrigger
    0x11CBF10: 0x11D9190,  # * CanResearch(ts, tmpl, 0): tail call of CCanResearchTrigger
    0x20B36E0: 0x20C6F20,  # GetManpower (anchors agree)
    0x2111840: 0x2125090,  # * GetModifier: clamps to [0, 0x186A0]
    # ---- engine
    0x2D82F10: 0x2D95150,  # * Resolve(handle*): type 0x1269 check, 24-byte buckets
    0x2D83E70: 0x2D960B0,  # handle serializer helper (group of 4, unique delta)
    0x2D84A70: 0x2D96CB0,  # "read side" (group of 4)
    0x2D8EE60: 0x2DA10A0,  # dynamic variable getter (group of 4)
    0x2DC1C40: 0x2DD3E80,  # * CCommandQueue::Post: first call after command ctors, 558 callers
    0x344B060: 0x345D230,  # * console Execute: ++[rsi+0x7C]; call Dispatch
    0x344B080: 0x345D250,  # * console Dispatch
    0x3471B20: 0x3483CF0,  # token id -> std::string* (group of 11, unique delta)
    0x3475520: 0x34876F0,  # serializer: named
    0x34755E0: 0x34877B0,  # serializer: object
    0x3475680: 0x3487850,  # serializer: object
    0x3475EC0: 0x3488090,  # serializer: fixed
    0x34771C0: 0x3489390,  # serializer: int
    0x34773E0: 0x34895B0,  # serializer: int
    0x3477840: 0x3489A10,  # serializer: bool
    0x9FD170: 0x9FE6B0,    # serializer: array
    0x3485810: 0x34979E0,  # * operator new == exported _Znwm
    0x3485830: 0x3497A00,  # operator delete variant
    0x3485850: 0x3497A20,  # * operator delete == exported _ZdlPv
    # ---- commands (anchors: RTTI name in both builds)
    0x2095400: 0x20A8C40, 0x20955A0: 0x20A8DE0,   # CPauseGame ctor / CanExecute
    0x21E4710: 0x21F8030, 0x21E4FC0: 0x21F88E0,   # COrderGroupCommand
    0x21E5910: 0x21F9230, 0x21E66F0: 0x21FA010,   # CArmyGroupCommand
    0x21FF3E0: 0x2212D00, 0x21FF820: 0x2213140,   # CSetArmyLeaderCommand
    0x21F3A10: 0x2207330, 0x21F4280: 0x2207BA0,   # COrderNewRootCommand (front line)
    0x21F9BF0: 0x220D510, 0x21FB390: 0x220ECB0,   # COrderNewFrontCommand (offensive line)
    0x21F22F0: 0x2205C10, 0x21F2A10: 0x2206330,   # COrderNewFallbackCommand
    0x21EBA90: 0x21FF3B0, 0x21ED460: 0x2200D80,   # COrderAssignCommand
    0x21EDB90: 0x22014B0, 0x21EE620: 0x2201F40,   # COrderUnassignCommand
    0x21E9530: 0x21FCE50, 0x21E9090: 0x21FC9B0,   # COrderDeleteCommand
    0x21FEE60: 0x2212780, 0x21FF200: 0x2212B20,   # COrderExecuteCommand
    0x2200160: 0x2213A80, 0x2200760: 0x2214080,   # COrderSetInvasionSourceCommand
    0x2202420: 0x2215D40, 0x22029F0: 0x2216310,   # COrderSetParadropSourceCommand
    0x2202EE0: 0x2216800, 0x22030F0: 0x2216A10,   # COrderSetParadropTargetCommand
    0x23E9950: 0x23FB1E0,  # CMoveCommand ctor
    0x23F51A0: 0x2406A30,  # promote predicate (comment)
    0xA435D0: 0xA44AF0, 0xA45AA0: 0xA46FC0,       # CDiplomaticActionCommand ctor / CanExecute
    0xA563A0: 0xA578C0,    # CGenerateWarGoalAction ctor
    0xA4FBB0: 0xA510D0,    # CDeclareWarAction ctor
    # ---- vtables
    0x41AED30: 0x41C4980,  # 17CDeclareWarAction
    0x41AF190: 0x41C4DE0,  # 22CGenerateWarGoalAction
    0x419E870: 0x41B44C0,  # 18CBuildingReference
    0x42B0698: 0x42C66A8,  # scope: +0x10 and +0x60 are CGameDate address points
    0x42CA2D0: 0x42E02E0,  # CMoveCommand
    0x42677A8: 0x427D788, 0x4268450: 0x427E430, 0x4268528: 0x427E508,
    0x4268B10: 0x427EAF0, 0x4268BE8: 0x427EBC8, 0x4268CC0: 0x427ECA0,
    0x42690F8: 0x427F0D8, 0x4269530: 0x427F510, 0x426A898: 0x4280878,
    0x426A970: 0x4280950, 0x426AA48: 0x4280A28, 0x426ADA8: 0x4280D88,
    0x42C9CB8: 0x42DFCC8,
    # Upstream had these two 0xE0 past the real vtables (0x426AB20 / 0x426ABF8 in
    # 1.19.2); mapped by class name instead of carrying the error over.
    0x426AC00: 0x4280B00,  # CSetContinuousFocusCommand
    0x426ACD8: 0x4280BD8,  # CDropContinuousFocusCommand
}

# Not ported: unused outside offsets.py, and no confident match.
DROP = {0xD0CCE0: "FN_GET_POLITICS", 0x43E39F0: "G_STABILITY_BASE"}


def fmt(old_text, new):
    upper = any(c in "ABCDEF" for c in old_text[2:])
    s = "%X" % new if upper else "%x" % new
    return "0x" + s


def main():
    bridge = sys.argv[1]
    changed = {}
    for sub in ("calling", "mcp"):
        for root, _, files in os.walk(os.path.join(bridge, sub)):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                src = open(path, encoding="utf-8").read()

                def sub_(m):
                    v = int(m.group(0), 16)
                    if v in MAP:
                        changed[path] = changed.get(path, 0) + 1
                        return fmt(m.group(0), MAP[v])
                    return m.group(0)
                # skip "0xA438C0+0x2F5" style crash-trace references (1.19.2 history)
                out = re.sub(r"\b0x[0-9A-Fa-f]{6,8}\b(?!\+0x)", sub_, src)
                if out != src:
                    open(path, "w", encoding="utf-8").write(out)
    for p, n in sorted(changed.items()):
        print("%3d  %s" % (n, os.path.relpath(p, bridge)))


if __name__ == "__main__":
    main()
