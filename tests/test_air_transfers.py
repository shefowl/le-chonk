"""Offline regressions for pending rebases and leaving pilot exercises for missions."""
import contextlib
import struct
import types
import unittest
from unittest.mock import patch

from harness import air
from test_air_reserves import Memory


class AirFixture:
    def __init__(self, mask=0x800):
        self.p = Memory()
        self.p.u8 = lambda a: self.p.read(a, 1)[0]
        self.w, self.base, self.target, self.region = 0x2000, 0x5000, 0x6000, 0x9000
        self.p.write(self.w + air.W_MISSION, struct.pack('<I', mask))
        self.p.write(self.w + air.W_HANDLE, struct.pack('<II', 69, 483))
        for ptr, state in [(self.base, 206), (self.target, 219)]:
            self.p.write(ptr + air.AB_STATE, struct.pack('<Q', ptr + 0x1000))
            self.p.write(ptr + 0x1000 + air.ST_ID, struct.pack('<i', state))
        self.p.write(self.region + air.SR_ID, struct.pack('<i', 133))
        self.events = []
        self.freed = []
        self.g = types.SimpleNamespace(p=self.p, _inj=object(), malloc=self.p.malloc,
            call=lambda *a: 1, free=self.freed.append,
            cmd=types.SimpleNamespace(post=self.rebase_post))

    def rebase_post(self, cmd):
        self.p.write(self.w + 0x40, struct.pack('<II', 65, 57))

    def mission(self, g, w, mask, enable=True, clear=True):
        self.events.append(('mission', mask, enable, clear))
        old = self.p.u32(w + air.W_MISSION)
        value = 0 if clear else old
        value = (value | mask) if enable else (value & ~mask)
        self.p.write(w + air.W_MISSION, struct.pack('<I', value))
        return {'ok': True}

    def enable(self, g, w, region):
        self.events.append(('region', region))
        if self.p.u32(w + air.W_MISSION) & 0x800:
            return {'ok': False, 'error': 'training blocks region command'}
        self.p.write(w + air.W_REGION, struct.pack('<Q', self.region))
        return {'ok': True}

    def reads(self):
        s = contextlib.ExitStack()
        for name, value in [('_wings', {483: self.w}), ('_bases', {56: self.base, 57: self.target}),
                            ('home_base', self.base), ('attached_army', None)]:
            s.enter_context(patch.object(air, name, return_value=value))
        s.enter_context(patch('harness.navy.wings_afloat', return_value=set()))
        s.enter_context(patch('time.sleep'))
        return s


class TransferTests(unittest.TestCase):
    def test_pending_transfer_changes_effective_base_but_preserves_current_base(self):
        f = AirFixture()
        f.rebase_post(None)
        with f.reads():
            row = air.wings(f.g)[0]
        self.assertEqual(row['base'], 206)
        self.assertEqual(row['transfer_to'], 219)
        self.assertEqual(row['effective_base'], 219)

    def test_rebase_reports_accepted_transfer_instead_of_failure_before_arrival(self):
        f = AirFixture()
        with f.reads(), patch.object(air, 'bases', return_value=[{'state': 219, 'ptr': f.target}]):
            out = air.rebase(f.g, [483], 219, {}, 'SOV')
        self.assertEqual(out['moved'], [])
        self.assertEqual(out['transferring'], [483])
        self.assertNotIn('not_moved', out)

    def test_rebase_post_without_effect_is_not_accepted(self):
        f = AirFixture()
        f.g.cmd.post = lambda cmd: None
        with f.reads(), patch.object(air, 'bases', return_value=[{'state': 219, 'ptr': f.target}]):
            out = air.rebase(f.g, [483], 219, {}, 'SOV')
        self.assertEqual(out['transferring'], [])
        self.assertIn(483, out['not_moved'])


class TrainingMissionTests(unittest.TestCase):
    def test_leaves_training_before_assigning_region_and_new_mission(self):
        f = AirFixture()
        with f.reads(), patch.object(air, '_post_mission', side_effect=f.mission), \
                patch.object(air, '_post_region', side_effect=f.enable):
            out = air.fly(f.g, [483], 133, 'air_superiority')
        self.assertEqual(f.events, [('mission', 0x800, False, False), ('region', 133),
                                    ('mission', 1, True, True)])
        self.assertEqual(out['wings'][0]['region'], 133)
        self.assertEqual(out['wings'][0]['missions'], ['air_superiority'])

    def test_failed_region_restores_previous_training_and_reports_failure(self):
        f = AirFixture()
        with f.reads(), patch.object(air, '_post_mission', side_effect=f.mission), \
                patch.object(air, '_post_region', return_value={'ok': False, 'error': 'range'}):
            out = air.fly(f.g, [483], 133, 'air_superiority')
        self.assertEqual(f.p.u32(f.w + air.W_MISSION), 0x800)
        self.assertEqual(out['refused'][483], 'range; previous training restored')

    def test_post_success_without_region_effect_does_not_set_new_mission(self):
        f = AirFixture(mask=0)
        with f.reads(), patch.object(air, '_post_mission', side_effect=f.mission), \
                patch.object(air, '_post_region', return_value={'ok': True}):
            out = air.fly(f.g, [483], 133, 'air_superiority')
        self.assertEqual(f.events, [])
        self.assertIn('not confirmed', out['refused'][483])

    def test_unconfirmed_training_stop_does_not_try_region(self):
        f = AirFixture()
        with f.reads(), patch.object(air, '_post_mission', return_value={'ok': True}), \
                patch.object(air, '_post_region') as region:
            out = air.fly(f.g, [483], 133, 'air_superiority')
        region.assert_not_called()
        self.assertIn('stopping training not confirmed', out['refused'][483])


class NativeCommandTests(unittest.TestCase):
    def setUp(self):
        self.f = AirFixture()
        self.posted = []
        self.f.g.cmd.post = self.posted.append
        for vt, can in [(0x44701F0, 0x2419200), (0x44702B8, 0x2419c90)]:
            self.f.p.write(self.f.p.base + vt + 80, struct.pack('<Q', self.f.p.base + can))

    def test_disable_training_command_keeps_other_missions_and_owns_only_handle_array(self):
        f = self.f
        out = air._post_mission(f.g, f.w, 0x800, False, False)
        self.assertTrue(out['ok'])
        cmd = self.posted[0]
        self.assertEqual(f.p.u32(cmd + 0x40), 0x800)
        self.assertEqual(f.p.read(cmd + 0x44, 3), b'\0\0\0')
        self.assertEqual(f.p.read(f.p.u64(cmd + 0x28), 8), f.p.read(f.w + air.W_HANDLE, 8))
        self.assertEqual(f.freed, [])

    def test_region_command_has_empty_secondary_array_and_exact_target(self):
        f = self.f
        out = air._post_region(f.g, f.w, 133)
        self.assertTrue(out['ok'])
        cmd = self.posted[0]
        self.assertEqual(f.p.i32(cmd + 0x58), 133)
        self.assertEqual(f.p.u64(cmd + 0x40), 0)
        self.assertEqual(f.p.i32(cmd + 0x4C), 0)
        self.assertEqual(f.p.u8(cmd + 0x5C), 0)

    def test_dry_run_validates_commands_without_post_and_frees_both_arrays(self):
        f = self.f
        self.assertEqual(air._post_region(f.g, f.w, 133, dry_run=True),
                         {'ok': True, 'dry_run': True})
        self.assertEqual(air._post_mission(f.g, f.w, 0x800, False, False, dry_run=True),
                         {'ok': True, 'dry_run': True})
        self.assertEqual(self.posted, [])
        self.assertEqual(len(f.freed), 4)

    def test_unknown_canexecute_is_never_called_and_all_unposted_allocations_are_freed(self):
        f = self.f
        f.p.write(f.p.base + 0x44701F0 + 80, struct.pack('<Q', 0xDEADBEEF))
        with patch.object(f.g, 'call') as call:
            out = air._post_region(f.g, f.w, 133)
        self.assertFalse(out['ok'])
        call.assert_not_called()
        self.assertEqual(self.posted, [])
        self.assertEqual(len(f.freed), 2)

    def test_canexecute_false_does_not_post(self):
        f = self.f
        f.g.call = lambda *a: 0
        out = air._post_region(f.g, f.w, 133)
        self.assertFalse(out['ok'])
        self.assertEqual(self.posted, [])
        self.assertEqual(len(f.freed), 2)

    def test_command_corruption_fails_readback_before_any_call(self):
        f = self.f
        write = f.p.write
        def corrupt(addr, data):
            if len(data) == 0x60:
                data = data[:0x58] + struct.pack('<i', 999) + data[0x5C:]
            write(addr, data)
        f.p.write = corrupt
        with patch.object(f.g, 'call') as call:
            out = air._post_region(f.g, f.w, 133)
        self.assertFalse(out['ok'])
        call.assert_not_called()
        self.assertEqual(self.posted, [])


if __name__ == '__main__':
    unittest.main()
