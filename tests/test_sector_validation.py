"""Malformed sector cuts must not erase orders or consume a turn's allowance."""
import types
import unittest
from unittest.mock import patch

from harness import agents, ops


class SectorValidationTests(unittest.TestCase):
    def test_duplicate_army_rejected_before_game_access(self):
        g = types.SimpleNamespace(p=object())
        with patch.object(ops, 'theatre_armies', side_effect=AssertionError('game accessed')):
            with self.assertRaisesRegex(ops.OpError, 'duplicate|once'):
                ops.cut_front(g, object(), {}, 203, {229, 230, 584}, [(393, 3), (393, 4)])

    def test_invalid_weights_do_not_reach_native_mutations(self):
        for plan in ([], [(393, 0)], [(393, float('nan'))], [(393, float('inf'))],
                     [(393.9, 1)], [(True, 1)]):
            with self.subTest(plan=plan), patch.object(ops, 'theatre_armies',
                    side_effect=AssertionError('game accessed')):
                with self.assertRaises(ops.OpError):
                    ops.cut_front(types.SimpleNamespace(p=object()), object(), {}, 203, {229}, plan)

    def test_duplicate_model_rows_leave_cut_available(self):
        m = agents.Marshal.__new__(agents.Marshal)
        m.theatre = {'id': 203, 'enemy': 'PER'}
        m.this_turn = set()
        ctx = {'gw': types.SimpleNamespace(run=lambda *a: (_ for _ in ()).throw(
                   AssertionError('game accessed'))), 'mv': object()}
        with self.assertRaisesRegex(ops.OpError, 'duplicate|once'):
            m.handle(ctx, 'set_sectors', {'armies': [
                {'army': 393, 'states': [230]}, {'army': 393, 'states': [229]}]})
        self.assertNotIn('set_sectors', m.this_turn)


if __name__ == '__main__': unittest.main()
