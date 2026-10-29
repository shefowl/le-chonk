"""Tool routing preserves validated inputs and partial effects for the model."""
import types
import unittest
from unittest.mock import Mock, patch

from harness import agents, air, economy, ops


class ToolBoundaryTests(unittest.TestCase):
    def test_concentrate_model_input_is_not_coerced_before_validation(self):
        a = agents.AirStaff.__new__(agents.AirStaff); a.this_turn = set()
        a._not_mine = lambda ctx: []
        ctx = {'gw': types.SimpleNamespace(run=lambda fn, *args: fn(None, *args)),
               'mv': object(), 'holders': {}, 'own': 'SOV', 'enemies': set()}
        with patch.object(agents, 'briefing_air_regions',
                          return_value=types.SimpleNamespace(name={1: 'test'})), \
                patch.object(air, '_wings', side_effect=AssertionError('read game')):
            for bad in (1.5, True, '2', 0, -1):
                with self.subTest(count=bad), self.assertRaisesRegex(ops.OpError, 'positive integer'):
                    a.handle(ctx, 'concentrate_air', {'region': 1, 'kind': 'fighter',
                        'wings': bad, 'mission': 'air_superiority'})

    def test_industry_routes_model_selected_fort_provinces(self):
        a = agents.Industry.__new__(agents.Industry); a.memory = []; a.this_turn = set()
        ctx = {'gw': types.SimpleNamespace(run=lambda fn, *args: fn(None, *args)),
               'mv': object(), 'holders': {206: ('SOV', 'SOV')}}
        with patch.object(economy, 'build', return_value={'ok': True, 'provinces': [9]}) as build:
            a.handle(ctx, 'build', {'state_id': 206, 'building': 'bunker', 'provinces': [9]})
        self.assertEqual(build.call_args.args[-1], [9])

    def test_location_reader_lists_choices_without_posting(self):
        g = types.SimpleNamespace(player_tag=lambda: 'SOV', add_construction=Mock())
        mv = types.SimpleNamespace(land_provinces=lambda st: {2, 3}, terrain={2: 'forest'},
                                  coast=set(), city_at={3: ('Minsk', 10)}, forts={2: 1})
        with patch.object(ops, '_ctl', return_value=lambda pr: 'SOV'):
            result = economy.construction_locations(g, mv, {206: ('SOV', 'SOV')}, 206)
        self.assertEqual([p['province'] for p in result['provinces']], [2, 3])
        self.assertEqual(result['provinces'][0]['land_fort_level'], 1)
        g.add_construction.assert_not_called()


if __name__ == '__main__': unittest.main()
