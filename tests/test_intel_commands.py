"""Offline native-command routing and effect confirmation for intelligence tools."""
import struct
import types
import unittest
from unittest.mock import patch

from harness import intel


class Memory:
    base = 0x10000000

    def __init__(self):
        self.data = {}

    def read(self, addr, size):
        return bytes(self.data.get(addr + i, 0) for i in range(size))

    def write(self, addr, data):
        self.data.update((addr + i, value) for i, value in enumerate(data))

    def u64(self, addr):
        return struct.unpack('<Q', self.read(addr, 8))[0]

    def i32(self, addr):
        return struct.unpack('<i', self.read(addr, 4))[0]


class Game:
    """Consumes documented ctor signatures, then applies optional posted effects."""
    def __init__(self):
        self.p = Memory()
        self._inj = object()
        self.allocations = {}
        self.next = 0x10000
        self.commands = {}
        self.posted = []
        self.ours = {}
        self.allowed = True
        self.no_effect = False
        self.settles = []
        self.cmd = types.SimpleNamespace(post=self.post)
        self.p.write(0x2000 + 0xF98, struct.pack('<Q', 0x3000))

    def malloc(self, size):
        ptr = self.next
        self.next += size + 0x100
        self.allocations[ptr] = size
        return ptr

    def player(self):
        return types.SimpleNamespace(ptr=0x2000)

    def _c(self, tag):
        return types.SimpleNamespace(tag_id=7 if tag is None else 8)

    def _settle(self, delay):
        self.settles.append(delay)

    def call(self, addr, args):
        if addr == 0x123456:
            return int(self.allowed)
        sizes = {0x22CEA20: (0x40, 2), 0x22CEF80: (0x40, 3),
                 0x22D3CD0: (0x40, 3), 0x22D5520: (0x58, 8)}
        size, count = sizes[addr]
        assert self.allocations[args[0]] >= size, 'undersized native command'
        assert len(args) == count, 'wrong native constructor signature'
        self.commands[args[0]] = (addr, args)
        self.p.write(args[0], struct.pack('<Q', self.p.base + addr))
        self.p.write(self.p.base + addr + 80, struct.pack('<Q', self.p.base + 0x123456))
        return 0

    def post(self, cmd):
        ctor, args = self.commands[cmd]
        self.posted.append(ctor)
        if self.no_effect:
            return
        if ctor == 0x22CEA20:
            self.p.write(0x3000 + 0xC0, struct.pack('<i', 256))
        elif ctor == 0x22CEF80:
            self.p.write(0x3000 + 0xD0, struct.pack('<Q', args[2]))
        elif ctor == 0x22D3CD0:
            self.ours['Agent'] = args[2]


class IntelligenceCommandTests(unittest.TestCase):
    def setUp(self):
        self.g = Game()
        api = types.SimpleNamespace(allow_call=lambda addr: None)
        ao = types.SimpleNamespace(_mkstring=lambda g: 0x5000,
                                   _readstring=lambda g, ptr: 'refused' if not g.allowed else '')
        modules = {'api': api, 'armyops': ao,
                   'offsets': types.SimpleNamespace(VT_CANEXECUTE=80)}
        patches = [patch.object(intel, 'B', side_effect=modules.__getitem__),
                   patch.object(intel, '_upgrades', return_value={'upgrade_test': 0x7000}),
                   patch.object(intel, '_names', side_effect=lambda g, off:
                                {'Agent': 0x8000} if off == 0x18 else dict(g.ours)),
                   patch.object(intel, 'operations_db', return_value={'operation_test': 3})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_create_uses_agency_ctor_and_confirms_creation_started(self):
        result = intel.create(self.g)
        self.assertTrue(result['created'])
        self.assertTrue(result.get('verified', False))
        self.assertEqual(self.g.posted, [0x22CEA20])
        self.assertEqual(self.g.settles, [0.5])

    def test_create_post_without_effect_is_not_creation(self):
        self.g.no_effect = True
        result = intel.create(self.g)
        self.assertIn('not confirmed', result.get('error', ''))
        self.assertFalse(result.get('created', False))

    def test_negative_canexecute_never_posts(self):
        self.g.allowed = False
        result = intel.create(self.g)
        self.assertEqual(result['error'], 'refused')
        self.assertEqual(self.g.posted, [])
        self.assertEqual(self.g.settles, [])

    def test_upgrade_post_without_effect_is_not_upgrading(self):
        self.g.no_effect = True
        result = intel.upgrade(self.g, 'upgrade_test')
        self.assertIn('not confirmed', result.get('error', ''))
        self.assertNotIn('upgrading', result)

    def test_upgrade_confirms_selected_upgrade_pointer(self):
        result = intel.upgrade(self.g, 'upgrade_test')
        self.assertEqual(result['upgrading'], 'upgrade_test')
        self.assertTrue(result.get('verified', False))

    def test_recruit_post_without_effect_is_not_recruitment(self):
        self.g.no_effect = True
        result = intel.recruit(self.g, 'Agent')
        self.assertIn('not confirmed', result.get('error', ''))
        self.assertNotIn('recruited', result)

    def test_recruit_confirms_operative_in_our_roster(self):
        result = intel.recruit(self.g, 'Agent')
        self.assertEqual(result['recruited'], 'Agent')
        self.assertTrue(result.get('verified', False))

    def test_operation_keeps_its_own_ctor_and_larger_allocation(self):
        self.g.ours['Agent'] = 0x8000
        self.g.p.write(0x8008, struct.pack('<II', 100, 42))
        result = intel.prepare_operation(self.g, 'operation_test', 'GER', ['Agent'])
        self.assertEqual(result['prepared'], 'operation_test')
        self.assertEqual(self.g.posted, [0x22D5520])


if __name__ == '__main__':
    unittest.main()
