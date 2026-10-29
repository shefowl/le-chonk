"""Air commands report all requested IDs and actual, pending and refused effects."""
import struct
import types
import unittest
from unittest.mock import patch

from harness import air, ops
from tests.test_air_reserves import Memory


class AirOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.p = Memory()
        self.p.u8 = lambda addr: self.p.read(addr, 1)[0]
        self.mine = {1: 0x1000, 2: 0x2000, 3: 0x3000}
        self.current = {1: 10, 2: 10, 3: 10}
        self.transfers = {}
        self.regions = {1: 0, 2: 0, 3: 0}
        self.no_effect = set()
        self.pending = set()
        self.commands = {}
        self.posted = []
        self.army = 0x4000
        self.p.write(self.army + 0xC, struct.pack('<i', 42))
        self.g = types.SimpleNamespace(p=self.p, _inj=object(), malloc=self.p.malloc,
                                      call=self.call, cmd=types.SimpleNamespace(post=self.post))
        patches = [patch.object(air, '_wings', side_effect=lambda g: dict(self.mine)),
                   patch.object(air, 'wings', side_effect=self.rows),
                   patch.object(air, '_region', side_effect=lambda g, w: self.regions[w // 0x1000]),
                   patch.object(air, 'home_base', side_effect=lambda g, w: self.current[w // 0x1000]),
                   patch.object(air, 'transfer_base', side_effect=lambda g, w: self.transfers.get(w // 0x1000)),
                   patch.object(air, 'base_state', side_effect=lambda g, b: b),
                   patch.object(air, 'bases', return_value=[{'state': 20, 'ptr': 20}]),
                   patch.object(air, '_post_region', side_effect=self.region),
                   patch.object(air, '_post_mission', side_effect=self.mission),
                   patch('harness.navy.wings_afloat', return_value={3}),
                   patch.object(ops, 'army', return_value=self.army),
                   patch('time.sleep')]
        # Only allowlist registration is external; preserve other module lookups.
        from harness.bridge import module
        patches.append(patch.object(module('api'), 'allow_call'))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def rows(self, g):
        return [{'id': i, 'kind': 'fighter', 'planes': 100, 'army': air.attached_army(g, w),
                 'region': self.regions[i], 'missions': air.missions_of(self.p.u32(w + air.W_MISSION)),
                 'can_fly': ['air_superiority'], 'base': self.current[i],
                 'transfer_to': self.transfers.get(i), 'effective_base': self.transfers.get(i, self.current[i])}
                for i, w in self.mine.items()]

    def region(self, g, w, rid):
        if w // 0x1000 not in self.no_effect:
            self.regions[w // 0x1000] = rid
        return {'ok': True}

    def mission(self, g, w, mask, enable=True, clear=True):
        if w // 0x1000 not in self.no_effect:
            self.p.write(w + air.W_MISSION, struct.pack('<I', mask))
        return {'ok': True}

    def call(self, ctor, args):
        self.commands[args[0]] = (ctor, args[1:])

    def post(self, cmd):
        ctor, args = self.commands[cmd]
        wing = args[0] // 0x1000
        self.posted.append(wing)
        if wing in self.no_effect:
            return
        if ctor == air.TRANSFER_CTOR:
            if wing in self.pending:
                self.transfers[wing] = args[1]
            else:
                self.current[wing] = args[1]
        elif ctor == air.ATTACH_CTOR:
            self.p.write(args[0] + air.W_ARMY, struct.pack('<Q', args[1]))

    def assert_unconfirmed(self, out):
        self.assertIs(out.get('ok'), False)
        self.assertIs(out.get('confirmed'), False)

    def test_assignment_partial_no_effect_is_unconfirmed(self):
        self.no_effect.add(2)
        out = air.assign(self.g, 5, 'air_superiority', [1, 2])
        self.assert_unconfirmed(out)
        self.assertEqual(out['wings_flying_it'], [1])
        self.assertIn(2, out['refused'])

    def test_assignment_preserves_missing_and_carrier_ids(self):
        out = air.assign(self.g, 5, 'air_superiority', [1, 3, 99])
        self.assert_unconfirmed(out)
        self.assertEqual(out['wings_flying_it'], [1])
        self.assertEqual(set(out['refused']), {3, 99})

    def test_assignment_insufficient_requested_count_is_partial(self):
        self.mine = {1: 0x1000}
        out = air.assign(self.g, 5, 'air_superiority', kind_='fighter', count=2)
        self.assert_unconfirmed(out)
        self.assertEqual(out['requested_wings'], 2)
        self.assertEqual(out['wings_flying_it'], [1])

    def test_assignment_complete_readback_is_confirmed(self):
        out = air.assign(self.g, 5, 'air_superiority', [1, 2])
        self.assertIs(out.get('ok'), True)
        self.assertIs(out.get('confirmed'), True)

    def test_direct_fly_preserves_invalid_ids(self):
        out = air.fly(self.g, [1, 3, 99], 5, 'air_superiority')
        self.assert_unconfirmed(out)
        self.assertEqual(set(out['refused']), {3, 99})

    def test_rebase_partial_arrival_and_invalid_ids_is_unconfirmed(self):
        self.no_effect.add(2)
        out = air.rebase(self.g, [1, 2, 3, 99], 20, {}, 'SOV')
        self.assert_unconfirmed(out)
        self.assertEqual(out['moved'], [1])
        self.assertEqual(set(out['refused']), {2, 3, 99})

    def test_rebase_pending_transfer_is_separate_from_arrival(self):
        self.pending.add(1)
        out = air.rebase(self.g, [1], 20, {}, 'SOV')
        self.assert_unconfirmed(out)
        self.assertIs(out.get('pending'), True)
        self.assertEqual(out['moved'], [])
        self.assertEqual(out['transferring'], [1])
        self.assertEqual(out.get('refused'), {})
        self.assertIs(out.get('transfer_confirmed'), True)

    def test_rebase_complete_arrival_is_confirmed(self):
        out = air.rebase(self.g, [1], 20, {}, 'SOV')
        self.assertIs(out.get('ok'), True)
        self.assertIs(out.get('confirmed'), True)

    def test_attach_partial_no_effect_and_invalid_ids_is_unconfirmed(self):
        self.no_effect.add(2)
        out = air.attach(self.g, [1, 2, 3, 99], self.army)
        self.assert_unconfirmed(out)
        self.assertEqual(out['wings'], {1: 42, 2: None, 3: None})
        self.assertEqual(set(out['refused']), {2, 3, 99})

    def test_attach_wrapper_does_not_drop_missing_ids(self):
        out = air.attach_to_army(self.g, 42, [1, 3, 99])
        self.assert_unconfirmed(out)
        self.assertEqual(out['wings_attached'], [1])
        self.assertEqual(set(out['refused']), {3, 99})

    def test_attach_complete_readback_is_confirmed(self):
        out = air.attach_to_army(self.g, 42, [1, 2])
        self.assertIs(out.get('ok'), True)
        self.assertIs(out.get('confirmed'), True)

    def test_concentrate_without_a_base_preserves_failed_assignment(self):
        regions = types.SimpleNamespace(label=str, states={5: [20]})
        self.no_effect.add(1)
        with patch.object(air, 'sky', return_value={}), patch.object(air, 'bases', return_value=[]):
            out = air.concentrate(self.g, regions, 5, 'fighter', 1, 'air_superiority', {}, 'SOV')
        self.assert_unconfirmed(out)
        self.assertEqual(out['flying_it'], [])
        self.assertIn(1, out['refused'])

    def test_concentrate_pending_rebase_does_not_claim_a_mission(self):
        regions = types.SimpleNamespace(label=str, states={5: [20]})
        self.pending.add(1)
        with patch.object(air, 'sky', return_value={}), \
                patch.object(air, '_post_region', return_value={'ok': True}), \
                patch.object(air, 'bases', return_value=[{'state': 20, 'ptr': 20, 'capacity': 200, 'planes': 0}]):
            out = air.concentrate(self.g, regions, 5, 'fighter', 1, 'air_superiority', {}, 'SOV')
        self.assert_unconfirmed(out)
        self.assertEqual(out['flying_it'], [])
        self.assertEqual(out['transferring'], [1])
        self.assertIs(out['pending'], True)
        self.assertEqual(out['wings'][0].get('transfer_to'), 20)

    def test_concentrate_cannot_report_more_wings_than_it_found(self):
        self.mine = {1: 0x1000}
        regions = types.SimpleNamespace(label=str, states={5: [20]})
        with patch.object(air, 'sky', return_value={}):
            out = air.concentrate(self.g, regions, 5, 'fighter', 2, 'air_superiority', {}, 'SOV')
        self.assert_unconfirmed(out)
        self.assertEqual(out['flying_it'], [1])
        self.assertEqual(out['requested_wings'], 2)

    def test_invalid_counts_are_rejected_before_any_game_access(self):
        regions = types.SimpleNamespace(label=str, states={5: [20]})
        for count in (0, -1, 1.5, '2', True):
            calls = [lambda: air.assign(self.g, 5, 'air_superiority', kind_='fighter', count=count),
                     lambda: air.attach_to_army(self.g, 42, kind_='fighter', count=count),
                     lambda: air.concentrate(self.g, regions, 5, 'fighter', count,
                                            'air_superiority', {}, 'SOV')]
            for call in calls:
                with self.subTest(count=count, tool=call), \
                        patch.object(air, '_wings', side_effect=AssertionError('game read before validation')) as read, \
                        patch.object(ops, 'army', side_effect=AssertionError('army read before validation')) as army, \
                        patch.object(air, 'sky', side_effect=AssertionError('sky read before validation')) as sky:
                    with self.assertRaisesRegex(ValueError, 'positive integer'):
                        call()
                    read.assert_not_called()
                    army.assert_not_called()
                    sky.assert_not_called()

    def test_concentrate_requires_a_count(self):
        with self.assertRaisesRegex(ValueError, 'positive integer'), \
                patch.object(air, 'sky', side_effect=AssertionError('game read before validation')):
            air.concentrate(self.g, None, 5, 'fighter', None, 'air_superiority', {}, 'SOV')


if __name__ == '__main__':
    unittest.main()
