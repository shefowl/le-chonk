"""Queue changes and model-selected inland fort provinces have truthful outcomes."""
import types
import unittest
from unittest.mock import Mock, patch

from harness import economy, ops
from harness.bridge import module


class Queue:
    def __init__(self, effect=True, accepted=True):
        self.rows = [dict(index=i, building=b, handle={'type': 59, 'id': i + 1})
                     for i, b in enumerate(['infrastructure', 'arms_factory',
                                            'industrial_complex', 'arms_factory'])]
        self.effect, self.accepted, self.moves = effect, accepted, []

    def construction_queue(self):
        return [dict(r, index=i) for i, r in enumerate(self.rows)]

    def move_construction(self, index, position):
        self.moves.append((index, position))
        if self.effect:
            self.rows.insert(position, self.rows.pop(index))
        return {'ok': self.accepted}


class ConstructionOutcomeTests(unittest.TestCase):
    def test_priority_noop_is_not_claimed_as_moved(self):
        q = Queue(effect=False)
        r = economy.prioritize_construction(q, 'arms_factory')
        self.assertFalse(r['ok'])
        self.assertFalse(r['confirmed'])
        self.assertEqual(r['moved'], 0)
        self.assertEqual(len(q.moves), 1)

    def test_priority_rejection_stops_and_keeps_actual_queue(self):
        q = Queue(effect=False, accepted=False)
        r = economy.prioritize_construction(q, 'arms_factory')
        self.assertFalse(r['ok'])
        self.assertIn('infrastructure', r['queue_front'])
        self.assertEqual(len(q.moves), 1)

    def test_priority_preserves_relative_order_and_verifies(self):
        q = Queue()
        r = economy.prioritize_construction(q, 'arms_factory')
        self.assertTrue(r['ok'])
        self.assertTrue(r['confirmed'])
        self.assertEqual([x['handle']['id'] for x in q.rows], [2, 4, 1, 3])
        self.assertEqual(r['moved'], 2)

    def test_already_prioritized_is_successful_without_post(self):
        q = Queue()
        q.rows = [q.rows[1], q.rows[3], q.rows[0], q.rows[2]]
        r = economy.prioritize_construction(q, 'arms_factory')
        self.assertTrue(r['ok'])
        self.assertEqual(r['moved'], 0)
        self.assertEqual(q.moves, [])

    def fort_fixture(self):
        g = types.SimpleNamespace(player_tag=lambda: 'SOV',
            add_construction=Mock(return_value={'ok': True}))
        mv = types.SimpleNamespace(land={1, 2, 3, 9}, state_of={1: 206, 2: 206, 3: 206, 9: 9},
            land_provinces=lambda st: {1, 2, 3}, adj={1: {2}, 2: {1, 3}, 3: {2}},
            terrain={1: 'plains', 2: 'forest', 3: 'urban'}, vp={}, forts={}, coast=set(),
            city_at={3: ('Minsk', 10)}, label=lambda st: 'Minsk (#206)')
        return g, mv, {206: ('SOV', 'SOV')}

    def test_explicit_inland_fort_province_reaches_existing_command(self):
        g, mv, h = self.fort_fixture()
        with patch.object(ops, '_ctl', return_value=lambda pr: 'SOV'):
            r = economy.build(g, 206, 'bunker', 2, mv, h, provinces=[2, 3])
        self.assertEqual(r['provinces'], [2, 3])
        self.assertTrue(r['ok'])
        self.assertEqual(g.add_construction.call_count, 2)
        g.add_construction.assert_any_call(206, 'bunker', level=2, province=3)

    def test_fort_validates_entire_list_before_any_post(self):
        g, mv, h = self.fort_fixture()
        with patch.object(ops, '_ctl', return_value=lambda pr: 'SOV'):
            for provinces in ([1, 9], [1, 1], []):
                with self.subTest(provinces=provinces), self.assertRaises(Exception):
                    economy.build(g, 206, 'bunker', 1, mv, h, provinces=provinces)
        g.add_construction.assert_not_called()

    def test_partial_build_preserves_queued_count_but_reports_failure(self):
        g, mv, h = self.fort_fixture()
        g.add_construction.side_effect = [{'ok': True}, {'ok': False}]
        with patch.object(ops, '_ctl', return_value=lambda pr: 'SOV'):
            r = economy.build(g, 206, 'bunker', 1, mv, h, provinces=[2, 3])
        self.assertFalse(r['ok'])
        self.assertEqual(r['provinces'], [2])
        self.assertEqual(r['refused_provinces'], [3])

    def test_partial_state_build_is_not_full_success(self):
        g = types.SimpleNamespace(add_construction=Mock(side_effect=[{'ok': True}, {'ok': False}]),
                                  date=lambda: {'raw': 24})
        r = economy.build(g, 206, 'arms_factory', 3)
        self.assertEqual(r['queued'], 1)
        self.assertEqual(r['requested'], 3)
        self.assertFalse(r['ok'])

    def test_invalid_build_count_never_posts(self):
        g = types.SimpleNamespace(add_construction=Mock(return_value={'ok': True}))
        for count in (0, -1, 11, 1.5, True):
            with self.subTest(count=count), self.assertRaises(Exception):
                economy.build(g, 206, 'arms_factory', count)
        g.add_construction.assert_not_called()


class NativePriorityTests(unittest.TestCase):
    def fixture(self, effect):
        G = module('game')
        rows = [0x2000, 0x3000, 0x4000]
        g = types.SimpleNamespace(p=types.SimpleNamespace(u64=lambda ptr: 0x5000),
            _c=lambda tag: types.SimpleNamespace(ptr=0x1000),
            handle_of=lambda ptr: {'type': 59, 'id': ptr}, _settle=Mock())
        def send(name, fields, tag):
            if effect:
                rows.insert(fields['arg1'], rows.pop(2))
            return {'ok': True}
        g.send_command = Mock(side_effect=send)
        return G, g, rows

    def test_native_priority_noop_reports_unconfirmed(self):
        G, g, rows = self.fixture(False)
        with patch.object(G, '_arr', side_effect=lambda *a: list(rows)):
            r = G.Game.move_construction(g, 2, 0)
        self.assertFalse(r['ok'])
        self.assertFalse(r['confirmed'])
        self.assertEqual(r['position_after'], 2)

    def test_native_priority_reads_same_handle_at_new_position(self):
        G, g, rows = self.fixture(True)
        with patch.object(G, '_arr', side_effect=lambda *a: list(rows)):
            r = G.Game.move_construction(g, 2, 0)
        self.assertTrue(r['ok'])
        self.assertEqual(r['position_after'], 0)
        g._settle.assert_called_once_with(0.5)


if __name__ == '__main__': unittest.main()
