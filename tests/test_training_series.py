"""Offline regressions for finite model-chosen training batches and explicit repeat."""
import copy
import struct
import types
import unittest
from unittest.mock import patch

from harness import army, army_needs, agents
from harness.bridge import module

ind = module('industry')


class Memory:
    base = 0x10000000

    def __init__(self):
        self.data = {}
        self.next = 0x1000

    def read(self, addr, n):
        return bytes(self.data.get(addr + i, 0) for i in range(n))

    def write(self, addr, value):
        self.data.update((addr + i, b) for i, b in enumerate(value))

    def u64(self, addr):
        return struct.unpack('<Q', self.read(addr, 8))[0]

    def u32(self, addr):
        return struct.unpack('<I', self.read(addr, 4))[0]

    def i32(self, addr):
        return struct.unpack('<i', self.read(addr, 4))[0]

    def malloc(self, size):
        addr = self.next
        self.next += size + 0x100
        return addr


class Game:
    """Tiny command consumer: real verified command bytes, simulated conveyor effects."""
    def __init__(self):
        self.p = Memory()
        self._inj = object()
        self.rows = []
        self.posted = []
        self.called = []
        self.freed = []
        self.no_effect = False
        self.cmd = types.SimpleNamespace(post=self.post)
        self.malloc = self.p.malloc
        self.free = self.freed.append
        self.p.write(0x9008, struct.pack('<II', 52, 42))
        self.division_templates = lambda: [{'name': 'Motorized', 'ptr': '0x9000'}]

    def call(self, fn, args):
        self.called.append(fn)
        return 1

    def conveyor(self, cid, count, series):
        ptr = 0x20000 + cid * 0x100
        self.p.write(ptr + 8, struct.pack('<II', 64, cid))
        self.p.write(ptr + 0x40, struct.pack('<i', series))
        lines = [{'id': cid * 100 + j, 'started': 1, 'completed': 0, 'active': True}
                 for j in range(count)]
        return {'id': cid, 'ptr': ptr, 'template': 'Motorized', 'template_ptr': 0x9000,
                'divisions': count, 'series': series, 'repeat': series == 0,
                'lines': lines, 'deployment_province': 123}

    def post(self, cmd):
        vt = self.p.u64(cmd) - self.p.base
        self.posted.append(vt)
        if self.no_effect:
            return
        if vt == 0x4462410:
            series, count, province = struct.unpack('<iii', self.p.read(cmd + 0x2C, 12))
            self.rows.append(self.conveyor(max([r['id'] for r in self.rows] + [0]) + 1,
                                          count, series))
            self.rows[-1]['deployment_province'] = province
        elif vt == 0x44628C0:
            cid = self.p.u32(cmd + 0x28)
            series = self.p.i32(cmd + 0x2C)
            for r in self.rows:
                if r['id'] == cid:
                    r['series'], r['repeat'] = series, series == 0
                    self.p.write(r['ptr'] + 0x40, struct.pack('<i', series))

    def finish_round(self):
        out = 0
        for r in self.rows:
            for line in r['lines']:
                if line['active']:
                    line['completed'] += 1
                    out += 1
                    line['active'] = r['series'] == 0 or line['started'] < r['series']
                    line['started'] += int(line['active'])
        return out


class FiniteBatchTests(unittest.TestCase):
    def setUp(self):
        self.g = Game()
        self.real_module = module
        for name, can in [('CreateConveyorExtended', 0x2205360),
                          ('SetConveyorSeries', 0x22071E0)]:
            vt = {'CreateConveyorExtended': 0x4462410, 'SetConveyorSeries': 0x44628C0}[name]
            self.g.p.write(self.g.p.base + vt + 80,
                           struct.pack('<Q', self.g.p.base + can))
        self.patchers = [patch.object(ind, 'conveyors', side_effect=lambda *a, **k:
                                     copy.deepcopy(self.g.rows)),
                         patch('time.sleep'), patch.object(module('api'), 'allow_call')]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def train(self, count=3, repeat=False):
        return army.train(self.g, 'Motorized', count, repeat=repeat,
                          deployment_province=123)

    def test_finite_three_produces_exactly_three_then_stops(self):
        result = self.train()
        self.assertEqual(result['series'], 1)
        self.assertEqual(result['parallel'], 3)
        self.assertEqual(self.g.finish_round(), 3)
        self.assertEqual(self.g.finish_round(), 0)
        self.assertFalse(result['repeat'])

    def test_explicit_repeat_keeps_parallel_lines(self):
        result = self.train(repeat=True)
        self.assertTrue(result['repeat'])
        self.assertEqual(result['series'], 0)
        self.assertEqual(self.g.finish_round(), 3)
        self.assertEqual(self.g.finish_round(), 3)
        self.assertEqual(self.g.posted, [0x4462410, 0x44628C0])

    def test_new_finite_batch_does_not_change_old_infinite_conveyor(self):
        self.g.rows = [self.g.conveyor(7, 38, 0)]
        result = self.train()
        self.assertEqual(result['conveyor_id'], 8)
        self.assertEqual(self.g.rows[0]['series'], 0)
        self.assertEqual(self.g.rows[0]['divisions'], 38)
        self.assertEqual(self.g.finish_round(), 41)
        self.assertEqual(self.g.finish_round(), 38)

    def test_post_without_new_conveyor_is_not_success(self):
        self.g.rows = [self.g.conveyor(7, 38, 0)]
        self.g.no_effect = True
        with self.assertRaisesRegex(army.OpError, 'not confirmed'):
            self.train()
        self.assertEqual(self.g.rows[0]['series'], 0)

    def test_set_series_refuses_post_without_effect(self):
        self.g.rows = [self.g.conveyor(7, 38, 0)]
        self.g.no_effect = True
        result = army.set_training_series(self.g, 7, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['series_after'], 0)

    def test_set_series_one_stops_existing_infinite_after_current_round(self):
        self.g.rows = [self.g.conveyor(7, 38, 0)]
        result = army.set_training_series(self.g, 7, 1)
        self.assertTrue(result['ok'])
        self.assertEqual(self.g.finish_round(), 38)
        self.assertEqual(self.g.finish_round(), 0)

    def test_unknown_canexecute_address_is_never_called_or_posted(self):
        self.g.p.write(self.g.p.base + 0x4462410 + 80, struct.pack('<Q', 0xDEADBEEF))
        with self.assertRaisesRegex(army.OpError, 'CanExecute'):
            self.train()
        self.assertEqual(self.g.called, [])
        self.assertEqual(self.g.posted, [])
        self.assertEqual(len(self.g.freed), 1)

    def test_canexecute_false_frees_command_without_post(self):
        self.g.call = lambda *args: 0
        with self.assertRaisesRegex(army.OpError, 'CanExecute false'):
            self.train()
        self.assertEqual(self.g.posted, [])
        self.assertEqual(len(self.g.freed), 1)

    def test_uncertain_post_does_not_free_or_retry_transferred_command(self):
        def uncertain(cmd):
            self.g.posted.append(cmd)
            raise RuntimeError('interrupted post')
        self.g.cmd.post = uncertain
        with self.assertRaisesRegex(RuntimeError, 'interrupted post'):
            self.train()
        self.assertEqual(len(self.g.posted), 1)
        self.assertEqual(self.g.freed, [])

    def test_corrupted_command_fields_are_never_called_or_posted(self):
        original = self.g.p.write

        def corrupt(addr, data):
            if len(data) == 0x40:
                data = data[:0x2C] + struct.pack('<i', 0) + data[0x30:]
            original(addr, data)

        self.g.p.write = corrupt
        with self.assertRaisesRegex(army.OpError, 'read-back'):
            self.train()
        self.assertEqual(self.g.called, [])
        self.assertEqual(self.g.posted, [])

    def test_missing_province_fails_before_creating_infinite_queue(self):
        with self.assertRaisesRegex(army.OpError, 'province'):
            army.train(self.g, 'Motorized', 3)
        self.assertEqual(self.g.posted, [])

    def test_invalid_count_or_repeat_fails_without_commands(self):
        for count, repeat in [(0, False), (25, False), (1.2, False), (True, False),
                              (3, 'false')]:
            with self.assertRaises(army.OpError):
                self.train(count, repeat)
        self.assertEqual(self.g.posted, [])


class TrainingFactsTests(unittest.TestCase):
    def test_template_requirements_include_tank_motorized_and_queue_demand(self):
        g = types.SimpleNamespace(
            division_templates=lambda: [{'name': 'Armor', 'ptr': '0x9000'}],
            divisions=lambda **k: {'divisions': [{'template': 'Armor'}]},
            equipment_stockpile=lambda **k: {'stockpile': [
                {'equipment': 'light_tank_chassis_1', 'count': 1204},
                {'equipment': 'light_tank_chassis_1', 'count': 534},
                {'equipment': 'motorized_equipment_1', 'count': 9481}]},
            production_lines=lambda: [{'equipment': 'light_tank_chassis_2',
                                      'output_per_day': 27.5}])
        cv = [{'id': 7, 'template': 'Armor', 'divisions': 3, 'series': 0, 'repeat': True,
               'ptr': 0x4000, 'lines': [{'id': 2, 'started': 5, 'completed': 4,
                                       'active': True}]}]
        with patch.object(ind, 'conveyors', return_value=cv), \
                patch.object(ind, 'training', return_value=[{'template': 'Armor'}] * 3), \
                patch.object(army, '_battalions', side_effect=[['light_armor', 'motorized'], []]), \
                patch.object(army_needs, 'unit_needs', return_value={
                    'light_armor': {'light_tank_chassis': 60},
                    'motorized': {'motorized_equipment': 35, 'infantry_equipment': 100}}):
            result = army.training_details(g)
        self.assertEqual(result['stockpile']['light_tank_chassis'], 1738)
        self.assertEqual(result['templates'][0]['equipment_per_division']['infantry_equipment'], 100)
        self.assertEqual(result['queue_full_equipment']['light_tank_chassis'], 180)
        self.assertEqual(result['conveyors'][0]['lines'][0]['completed'], 4)
        self.assertNotIn('ptr', result['conveyors'][0])
        self.assertIn('not a deficit', result['equipment_note'])


class CancelTrainingTests(unittest.TestCase):
    def test_conveyor_id_limits_cancellation_and_effect_is_counted(self):
        before = [{'template': 'Motorized', 'conveyor': 700, 'line': 71, 'training': .2},
                  {'template': 'Motorized', 'conveyor': 800, 'line': 81, 'training': .1}]
        with patch.object(ind, 'conveyors', return_value=[{'id': 7, 'ptr': 700},
                                                        {'id': 8, 'ptr': 800}]), \
                patch.object(ind, 'training', side_effect=[before, before[1:]]), \
                patch.object(ind, 'remove_line', return_value={'ok': True}) as remove, \
                patch('time.sleep'):
            r = army.cancel_training(None, 'Motorized', 1, conveyor_id=7)
        remove.assert_called_once_with(None, 71)
        self.assertTrue(r['ok'])
        self.assertEqual(r['cancelled'], 1)
        self.assertEqual(r['still_training'], 1)

    def test_post_without_disappearance_does_not_claim_cancellation(self):
        before = [{'template': 'Motorized', 'conveyor': 700, 'line': 71, 'training': .2}]
        with patch.object(ind, 'training', return_value=before), \
                patch.object(ind, 'remove_line', return_value={'ok': True}), \
                patch('time.sleep'):
            r = army.cancel_training(None, 'Motorized', 1)
        self.assertFalse(r['ok'])
        self.assertEqual(r['cancelled'], 0)
        self.assertIn('not confirmed', r['error'])


class ArmyToolRoutingTests(unittest.TestCase):
    def setUp(self):
        self.agent = agents.Army.__new__(agents.Army)
        self.g = object()
        self.ctx = {'mv': object(), 'gw': types.SimpleNamespace(
            run=lambda fn, *a, **kw: fn(self.g, *a, **kw))}

    def test_default_train_is_finite_and_uses_capital(self):
        with patch.object(army, 'home_province', return_value=6380), \
                patch.object(army, 'train', return_value={'ok': True}) as train:
            self.agent.handle(self.ctx, 'train', {'template': 'Armor', 'count': 4})
        train.assert_called_once_with(self.g, 'Armor', 4, False, 6380)

    def test_explicit_location_and_repeat_are_forwarded(self):
        with patch.object(army, 'home_province') as home, \
                patch.object(army, 'train', return_value={'ok': True}) as train:
            self.agent.handle(self.ctx, 'train', {'template': 'Armor', 'count': 4,
                                                'repeat': True, 'deployment_province': 123})
        home.assert_not_called()
        train.assert_called_once_with(self.g, 'Armor', 4, True, 123)

    def test_series_and_precise_cancellation_are_exposed(self):
        with patch.object(army, 'set_training_series') as series, \
                patch.object(army, 'cancel_training') as cancel:
            self.agent.handle(self.ctx, 'set_training_series', {'conveyor_id': 81, 'series': 1})
            self.agent.handle(self.ctx, 'cancel_training', {'template': 'Infantry', 'count': 8,
                                                          'conveyor_id': 81})
        series.assert_called_once_with(self.g, 81, 1)
        cancel.assert_called_once_with(self.g, 'Infantry', 8, 81)


if __name__ == '__main__':
    unittest.main()
