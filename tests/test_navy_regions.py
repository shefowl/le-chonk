"""Fleet-selected regions and filtered task-force coverage are different facts."""
import struct
import types
import unittest
from unittest.mock import patch

from harness import navy, admiralty
from tests.test_air_reserves import Memory


class NavyRegionsTests(unittest.TestCase):
    def setUp(self):
        self.p = Memory(); self.g = types.SimpleNamespace(p=self.p)
        self.fleet, self.taskforce = 0x1000, 0x2000
        self.sea = types.SimpleNamespace(name={174: 'Norwegian Coast'})
        self.set_regions([])

    def set_regions(self, ids):
        self.p.write(self.fleet + 0x40, struct.pack('<QiiQ', 0x3000, len(ids), len(ids), 0))
        for n, rid in enumerate(ids):
            ptr = 0x4000 + n * 0x100
            self.p.write(0x3000 + 8 * n, struct.pack('<Q', ptr))
            self.p.write(ptr + 0x48, struct.pack('<i', rid))

    def invoke(self, effect):
        def post(*args):
            if effect: self.set_regions([174])
            return None
        with patch.object(navy, '_find_fleet', return_value=self.fleet), \
                patch.object(navy, '_int_array', return_value=0), \
                patch.object(navy, '_post', side_effect=post):
            return navy.set_fleet_regions(self.g, 'Severnyy Flot', [174], self.sea)

    def test_accepted_post_without_effect_is_not_confirmed(self):
        r = self.invoke(False)
        self.assertFalse(r['ok'])
        self.assertFalse(r['confirmed'])
        self.assertEqual(r['regions'], [])
        self.assertEqual(r['requested_regions'], [174])

    def test_fleet_region_readback_is_independent_of_empty_taskforce_coverage(self):
        r = self.invoke(True)
        self.assertTrue(r['ok'])
        self.assertTrue(r['confirmed'])
        self.assertEqual(r['regions'], [174])
        self.assertEqual(navy.mission_regions(self.g, self.taskforce), [])

    def test_mission_guard_uses_configured_fleet_regions(self):
        a = admiralty.Admiralty.__new__(admiralty.Admiralty)
        a.this_turn = set()
        tf = {'name': 'Northern Patrol', 'fleet': 'Severnyy Flot', 'ptr': self.taskforce,
              'fleet_ptr': self.fleet, 'ships': {'destroyer': 6}}
        a._tf = lambda g, name: tf
        self.set_regions([174])
        ctx = {'gw': types.SimpleNamespace(run=lambda fn, *args: fn(self.g, *args)),
               'mv': object(), 'sea': self.sea}
        with patch('harness.fleets.ZONES', {}), \
                patch.object(navy, 'set_task_force_mission', return_value={'mission': 'patrol'}) as send:
            r = a.handle(ctx, 'task_force_mission', {'task_force': 'Northern Patrol', 'mission': 'patrol'})
        send.assert_called_once_with(self.g, self.taskforce, 'patrol')
        self.assertEqual(r['mission'], 'patrol')

    def test_agent_retains_partial_region_readback_for_model(self):
        a = admiralty.Admiralty.__new__(admiralty.Admiralty); a.this_turn = set()
        ctx = {'gw': types.SimpleNamespace(run=lambda fn, *args: fn(self.g, *args)),
               'mv': object(), 'sea': self.sea}
        result = {'ok': False, 'confirmed': False, 'regions': [46],
                  'requested_regions': [174], 'error': 'selection not confirmed'}
        with patch('harness.fleets.ZONES', {}), patch.object(navy, 'set_fleet_regions', return_value=result):
            actual = a.handle(ctx, 'fleet_regions', {'fleet': 'Severnyy Flot', 'regions': [174]})
        self.assertEqual(actual, result)


if __name__ == '__main__': unittest.main()
