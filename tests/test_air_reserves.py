"""Offline tests for explicit reserve deployment: no game process or injected calls."""
import struct
import types
import unittest
from unittest.mock import patch

from harness import air


class Memory:
    base = 0x10000000

    def __init__(self):
        self.data = {}
        self.strings = {}
        self.next = 0x10000

    def malloc(self, n):
        addr = self.next
        self.next += n + 0x100
        self.write(addr, bytes(n))
        return addr

    def write(self, addr, data):
        self.data.update((addr + i, b) for i, b in enumerate(data))

    def read(self, addr, n):
        return bytes(self.data.get(addr + i, 0) for i in range(n))

    def u64(self, addr):
        return struct.unpack('<Q', self.read(addr, 8))[0]

    def i32(self, addr):
        return struct.unpack('<i', self.read(addr, 4))[0]

    def u32(self, addr):
        return struct.unpack('<I', self.read(addr, 4))[0]

    def stdstring(self, addr):
        return self.strings.get(addr, '')


class ReserveTests(unittest.TestCase):
    def test_reads_stock_by_variant_without_producible_list_or_200_item_limit(self):
        p = Memory()
        p.write(0x1000 + 0xF40, struct.pack('<Q', 0x2000))
        p.write(0x2000 + 0x220, struct.pack('<QiiQ', 0x4000, 201, 201, 0))
        # Two designs on one airframe, including one beyond the old briefing's limit.
        for index, vp, did, name, count in [(0, 0xA000, 1476, 'I-15', 296),
                                          (200, 0xB000, 1477, 'I-16', 999)]:
            p.write(0x4000 + index * 16, struct.pack('<Qq', vp, count * 100000))
            p.write(vp + 8, struct.pack('<II', 70, did))
            p.write(vp + 0x14, struct.pack('<i', 4))
            p.strings[vp + 0x28] = name
            p.write(vp + 0x3F0, struct.pack('<Q', 0xC000))
        p.write(0xC000 + 0x530, struct.pack('<Q', 0xD000))
        g = types.SimpleNamespace(p=p, PS_STOCKPILE=0x220, _eq_by_id={4: 'small_plane_airframe_1'},
                                  _load_equipment=lambda: None,
                                  _c=lambda tag: types.SimpleNamespace(ptr=0x1000))
        rows = air.reserves(g)
        self.assertEqual([(r['design_id'], r['design'], r['planes']) for r in rows],
                         [(1476, 'I-15', 296), (1477, 'I-16', 999)])
        self.assertNotIn('ptr', rows[0])

    def test_base_capacity_counts_allied_wings_as_well_as_ours(self):
        p = Memory()
        p.write(0x3000 + air.AB_STATE, struct.pack('<Q', 0x4000))
        p.write(0x4000 + air.ST_ID, struct.pack('<i', 1))
        p.write(0x3000 + air.AB_CAPACITY, struct.pack('<i', 200))
        p.write(0x5000 + air.W_PLANES, struct.pack('<i', 100))
        p.write(0x6000 + air.W_PLANES, struct.pack('<i', 80))
        ao = types.SimpleNamespace(handle_objects=lambda g, t: {1: 0x5010, 2: 0x6010})
        with patch('harness.bridge.module', return_value=ao), \
                patch.object(air, '_bases', return_value={1: 0x3000}), \
                patch.object(air, 'home_base', return_value=0x3000):
            rows = air.bases(types.SimpleNamespace(p=p), {1: ('SOV', 'SOV')}, 'SOV')
        self.assertEqual(rows[0]['planes'], 180)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.p = Memory()
        self.calls = []
        self.freed = []
        self.g = types.SimpleNamespace(p=self.p, _inj=object(), malloc=self.p.malloc,
            free=self.freed.append,
            _c=lambda tag=None: types.SimpleNamespace(tag_id=27),
            call=lambda fn, args: self.calls.append(('call', fn)) or 1,
            cmd=types.SimpleNamespace(post=lambda cmd: self.calls.append(('post', cmd))))
        self.target = {'state': 1, 'capacity': 200, 'planes': 0, 'ptr': 0x3000}
        self.variant = {'design_id': 1477, 'design': 'I-16',
                        'equipment': 'small_plane_airframe_1', 'planes': 999,
                        'ptr': 0xA000, 'archetype': 0xD000}
        self.p.write(self.target['ptr'] + air.AB_HANDLE, struct.pack('<II', 65, 3))
        self.holders = {1: ('SOV', 'SOV')}
        self.patchers = [patch.object(air, 'bases', return_value=[self.target]),
                         patch.object(air, '_reserve_rows', return_value=[self.variant]),
                         patch.object(air, 'wings', return_value=[]),
                         patch('time.sleep')]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def deploy(self, **kw):
        return air.create_wing(self.g, 1, 1477, 100, self.holders, 'SOV', **kw)

    def test_dry_run_builds_serializable_stock_and_never_uses_gui_pointer(self):
        # Exercise actual builder and field verification; replace only the pure game check.
        with patch.object(air, '_deploy_allowed', return_value=True):
            result = self.deploy(dry_run=True)
        self.assertTrue(result['ok'])
        self.assertEqual(self.calls, [])
        cmd = 0x10000
        self.assertEqual(self.p.u64(cmd), self.p.base + air.DEPLOY_VTABLE)
        self.assertEqual(self.p.read(cmd + 0x78, 1), b'\x01')
        self.assertEqual(self.p.u64(cmd + 0x80), 0)
        self.assertEqual(self.p.read(cmd + 0x2C, 8), struct.pack('<II', 65, 3))
        stock = cmd + 0x38
        group = self.p.u64(stock + 8)
        variants = self.p.u64(stock + 0x20)
        self.assertEqual(struct.unpack('<Qiiq', self.p.read(group, 24)),
                         (0xD000, 0, 1, 10000000))
        self.assertEqual(struct.unpack('<Qq', self.p.read(variants, 16)),
                         (0xA000, 10000000))
        self.assertEqual(self.freed, [variants, group, cmd])
        self.assertNotIn(self.variant['ptr'], self.freed)
        self.assertNotIn(self.variant['archetype'], self.freed)

    def test_canexecute_false_does_not_post(self):
        with patch.object(air, '_deploy_allowed', return_value=False):
            result = self.deploy()
        self.assertFalse(result['ok'])
        self.assertIn('CanExecute', result['error'])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.freed), 3)

    def test_capacity_or_stock_refusal_happens_before_allocation_or_calls(self):
        self.target['planes'] = 150
        self.assertFalse(self.deploy()['ok'])
        self.target['planes'] = 0
        self.variant['planes'] = 99
        self.assertFalse(self.deploy()['ok'])
        self.assertEqual(self.p.next, 0x10000)
        self.assertEqual(self.calls, [])

    def test_effect_not_post_return_decides_success(self):
        with patch.object(air, '_deploy_allowed', return_value=True):
            result = self.deploy()
        self.assertFalse(result['ok'])
        self.assertIn('not confirmed', result['error'])
        self.assertEqual(self.calls[0][0], 'post')
        self.assertEqual(self.freed, [])

    def test_new_wing_is_returned_without_automatic_mission_or_base_choice(self):
        after = [{'id': 123, 'base': 1, 'planes': 100, 'missions': [], 'army': None}]
        with patch.object(air, '_deploy_allowed', return_value=True), \
                patch.object(air, 'wings', side_effect=[[], after]), \
                patch.object(air, '_reserve_rows', side_effect=[
                    [self.variant], [{**self.variant, 'planes': 899}]]):
            result = self.deploy()
        self.assertTrue(result['ok'])
        self.assertEqual(result['created_wings'], [123])
        self.assertEqual(result['planes'], 100)
        self.assertEqual([c[0] for c in self.calls], ['post'])

    def test_new_wing_without_selected_design_consumption_is_not_success(self):
        after = [{'id': 123, 'base': 1, 'planes': 100, 'missions': [], 'army': None}]
        with patch.object(air, '_deploy_allowed', return_value=True), \
                patch.object(air, 'wings', side_effect=[[], after]):
            result = self.deploy()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reserve_before'], 999)
        self.assertEqual(result['reserve_after'], 999)
        self.assertEqual(result['consumed'], 0)

    def test_other_design_consumption_does_not_confirm_selected_design(self):
        other = {**self.variant, 'design_id': 1476, 'design': 'I-15', 'planes': 296}
        after = [{'id': 123, 'base': 1, 'planes': 100, 'missions': [], 'army': None}]
        with patch.object(air, '_deploy_allowed', return_value=True), \
                patch.object(air, 'wings', side_effect=[[], after]), \
                patch.object(air, '_reserve_rows', side_effect=[
                    [self.variant, other], [self.variant, {**other, 'planes': 196}]]):
            result = self.deploy()
        self.assertFalse(result['ok'])
        self.assertEqual(result['consumed'], 0)

    def test_extra_reinforcement_consumption_still_confirms_requested_design(self):
        after = [{'id': 123, 'base': 1, 'planes': 100, 'missions': [], 'army': None}]
        with patch.object(air, '_deploy_allowed', return_value=True), \
                patch.object(air, 'wings', side_effect=[[], after]), \
                patch.object(air, '_reserve_rows', side_effect=[
                    [self.variant], [{**self.variant, 'planes': 890}]]):
            result = self.deploy()
        self.assertTrue(result['ok'])
        self.assertEqual(result['reserve_before'], 999)
        self.assertEqual(result['reserve_after'], 890)
        self.assertEqual(result['consumed'], 109)

    def test_exhausted_selected_variant_is_zero_after_deployment(self):
        self.variant['planes'] = 100
        after = [{'id': 123, 'base': 1, 'planes': 100, 'missions': [], 'army': None}]
        with patch.object(air, '_deploy_allowed', return_value=True), \
                patch.object(air, 'wings', side_effect=[[], after]), \
                patch.object(air, '_reserve_rows', side_effect=[[self.variant], []]):
            result = self.deploy()
        self.assertTrue(result['ok'])
        self.assertEqual(result['reserve_after'], 0)
        self.assertEqual(result['consumed'], 100)

    def test_invalid_plane_count_does_not_touch_game(self):
        for amount in (0, -1, 101, 1.5, True):
            result = air.create_wing(self.g, 1, 1477, amount, self.holders, 'SOV')
            self.assertFalse(result['ok'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.p.next, 0x10000)

    def test_build_readback_failure_frees_tracked_addresses_not_corrupted_pointers(self):
        original = self.p.write

        def corrupt(addr, data):
            if len(data) == air.DEPLOY_SIZE:
                data = data[:0x40] + struct.pack('<Q', self.variant['ptr']) + data[0x48:]
            original(addr, data)

        self.p.write = corrupt
        with self.assertRaisesRegex(ValueError, 'read-back'):
            self.deploy()
        self.assertEqual(len(self.freed), 3)
        self.assertNotIn(self.variant['ptr'], self.freed)
        self.assertNotIn(self.variant['archetype'], self.freed)
        self.assertEqual(self.calls, [])

    def test_corrupted_command_fields_are_never_checked_or_posted(self):
        original = self.p.write

        def corrupt(addr, data):
            if len(data) == air.DEPLOY_SIZE:
                data = data[:0x24] + b'\x00' + data[0x25:]
            original(addr, data)

        self.p.write = corrupt
        with patch.object(air, '_deploy_allowed') as check:
            with self.assertRaisesRegex(ValueError, 'read-back'):
                self.deploy()
        check.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_unidentified_canexecute_address_is_never_called(self):
        cmd = self.p.malloc(air.DEPLOY_SIZE)
        self.p.write(cmd, struct.pack('<Q', self.p.base + air.DEPLOY_VTABLE))
        self.p.write(self.p.base + air.DEPLOY_VTABLE + 80,
                     struct.pack('<Q', self.p.base + air.DEPLOY_CAN_EXECUTE + 1))
        with self.assertRaisesRegex(ValueError, 'unrecognized'):
            air._deploy_allowed(self.g, cmd)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
