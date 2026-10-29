"""Offline front allocation regressions: synthetic reads and command boundary only."""
import contextlib
import types
import unittest
from unittest.mock import patch

from harness import ops


class FrontFixture:
    def __init__(self, positions=None, lines=None, assigned=None):
        self.lines = lines or {1000: [1], 2000: [2, 3, 4], 3000: [5, 6]}
        self.units = positions or {100 + i: 0 for i in range(24)}
        self.orders = list(self.lines)
        self.assigned = {o: set((assigned or {}).get(o, ())) for o in self.orders}
        self.calls = []
        self.fields = {}
        for i, o in enumerate(self.orders):
            self.fields.update({o + ops.OI_ID: i + 101, o + ops.OI_FRONT: 10 + i,
                                o + ops.OI_SECTION: i, o + ops.OI_FROM: 0,
                                o + ops.OI_TO: ops.FULL, o + ops.OI_EXEC: 1})
        self.g = types.SimpleNamespace(p=types.SimpleNamespace(i32=lambda a: self.fields[a]),
                                       player_tag=lambda: "SOV")
        self.mv = types.SimpleNamespace(
            land={0, 1, 2, 3, 4, 5, 6, 50, 51},
            adj={0: {1}, 1: {0, 2}, 2: {1, 3}, 3: {2, 4}, 4: {3, 5},
                 5: {4, 6}, 6: {5}, 50: {51}, 51: {50}},
            state_of={i: 89 for i in range(7)}, land_provinces=lambda st: set(range(1, 7)),
            label=lambda st: str(st))
        self.ao = types.SimpleNamespace(assign_to_order=self.assign,
                                        delete_order=self.forbid, draw_front_line=self.forbid)

    def forbid(self, *a, **kw):
        raise AssertionError("assignment repair must not redraw or delete orders")

    def assign(self, game, army, oid, units):
        self.calls.append((oid, set(units)))
        for assigned in self.assigned.values():
            assigned.difference_update(units)
        order = next(o for o in self.orders if self.fields[o + ops.OI_ID] == oid)
        self.assigned[order].update(units)
        return True, ""

    def reads(self):
        stack = contextlib.ExitStack()
        values = {"_armyops": lambda: self.ao, "_passable": lambda *a: lambda pr: pr in self.mv.land,
                  "_front_index": lambda g: {}, "_fronts": lambda g, og: self.orders,
                  "_sector_line": lambda g, fronts, o: self.lines[o],
                  "_divs": lambda g, o: list(self.assigned[o]),
                  "division_bases": lambda g, og: list(self.units),
                  "province_of": lambda g, d: self.units[d]}
        for name, fn in values.items():
            stack.enter_context(patch.object(ops, name, fn))
        stack.enter_context(patch.object(ops.time, "sleep"))
        return stack


class FrontAssignmentTests(unittest.TestCase):
    def test_near_old_one_province_piece_still_fills_length_quotas(self):
        f = FrontFixture(assigned={1000: range(100, 124)})
        with f.reads():
            result = ops._assign_nearest(f.g, f.mv, {}, 1830)
        self.assertEqual(result, {101: 4, 102: 12, 103: 8})
        self.assertEqual([len(f.assigned[o]) for o in f.orders], [4, 12, 8])

    def test_enough_divisions_cover_even_a_short_piece_before_thickening_long_pieces(self):
        f = FrontFixture(positions={100: 0, 101: 0, 102: 0},
                         lines={1000: [1], 2000: list(range(2, 102)),
                                3000: list(range(102, 202))})
        f.mv.land = set(range(202))
        f.mv.adj = {i: {j for j in (i - 1, i + 1) if 0 <= j < 202} for i in range(202)}
        with f.reads():
            result = ops._assign_nearest(f.g, f.mv, {}, 1830)
        self.assertEqual(result, {101: 1, 102: 1, 103: 1})

    def test_existing_fixed_coverage_counts_towards_the_one_per_piece_minimum(self):
        f = FrontFixture(positions={100: 0, 101: 0, 102: 0},
                         lines={1000: [1], 2000: list(range(2, 102)),
                                3000: list(range(102, 202))},
                         assigned={2000: {100}})
        f.mv.land = set(range(202))
        f.mv.adj = {i: {j for j in (i - 1, i + 1) if 0 <= j < 202} for i in range(202)}
        with f.reads():
            result = ops._assign_nearest(f.g, f.mv, {}, 1830, only={101, 102})
        self.assertEqual(result, {101: 1, 102: 1, 103: 1})
        self.assertEqual(f.assigned[2000], {100})
        self.assertTrue(all(100 not in ds for _, ds in f.calls))

    def test_enclave_cannot_receive_unreachable_mainland_divisions(self):
        f = FrontFixture(positions={100: 0, 101: 0, 102: 50, 103: 999},
                         lines={1000: [1], 2000: [2], 3000: [51]})
        with f.reads():
            result = ops._assign_nearest(f.g, f.mv, {}, 1830)
        self.assertEqual(result, {101: 1, 102: 1, 103: 1})
        self.assertEqual(f.assigned[3000], {102})
        self.assertNotIn(103, set().union(*f.assigned.values()))

    def test_only_subset_fills_quota_deficits_without_reassigning_fixed_units(self):
        f = FrontFixture(positions={100 + i: 0 for i in range(6)},
                         lines={1000: [1], 2000: [2, 3], 3000: [4, 5, 6]},
                         assigned={1000: {100}, 2000: {101, 102}})
        with f.reads():
            result = ops._assign_nearest(f.g, f.mv, {}, 1830, only={103, 104, 105})
        self.assertEqual(result, {101: 1, 102: 2, 103: 3})
        self.assertEqual(f.assigned[1000], {100})
        self.assertEqual(f.assigned[2000], {101, 102})
        self.assertEqual(f.assigned[3000], {103, 104, 105})
        self.assertTrue(all(ds <= {103, 104, 105} for _, ds in f.calls))

    def test_balanced_exact_assignments_are_not_posted_again(self):
        f = FrontFixture(assigned={1000: range(100, 104), 2000: range(104, 116),
                                  3000: range(116, 124)})
        with f.reads():
            result = ops._assign_nearest(f.g, f.mv, {}, 1830)
        self.assertEqual(result, {101: 4, 102: 12, 103: 8})
        self.assertEqual(f.calls, [])

    def test_fixed_overfull_piece_does_not_take_more_from_only_subset(self):
        f = FrontFixture(positions={100 + i: 0 for i in range(6)},
                         lines={1000: [1], 2000: [2]}, assigned={1000: {100, 101, 102, 103}})
        with f.reads():
            result = ops._assign_nearest(f.g, f.mv, {}, 1830, only={104, 105})
        self.assertEqual(result, {101: 4, 102: 2})
        self.assertEqual(f.assigned[1000], {100, 101, 102, 103})
        self.assertEqual(f.assigned[2000], {104, 105})

    def test_nearest_excess_units_fill_the_other_piece(self):
        f = FrontFixture(positions={100: 0, 101: 1, 102: 5, 103: 6},
                         lines={1000: [1], 2000: [6]}, assigned={1000: {100, 101, 102, 103}})
        with f.reads():
            ops._assign_nearest(f.g, f.mv, {}, 1830)
        self.assertEqual(f.assigned[1000], {100, 101})
        self.assertEqual(f.assigned[2000], {102, 103})

    def test_lengths_are_counted_in_each_reachable_component(self):
        # Each piece contains an island and mainland part. Their global lengths are
        # equal, but mainland units must split 3:1 and island units 1:3.
        f = FrontFixture(positions={100 + i: (0 if i < 4 else 50) for i in range(8)},
                         lines={1000: [1, 2, 3, 50], 2000: [4, 51, 52, 53]})
        f.mv.land.update({52, 53})
        f.mv.adj.update({51: {50, 52}, 52: {51, 53}, 53: {52}})
        with f.reads():
            result = ops._assign_nearest(f.g, f.mv, {}, 1830)
        self.assertEqual(result, {101: 4, 102: 4})
        self.assertEqual(len(f.assigned[1000] & {100, 101, 102, 103}), 3)
        self.assertEqual(len(f.assigned[2000] & {100, 101, 102, 103}), 1)
        self.assertEqual(len(f.assigned[1000] & {104, 105, 106, 107}), 1)
        self.assertEqual(len(f.assigned[2000] & {104, 105, 106, 107}), 3)

    def test_unreachable_existing_assignment_is_refused_before_any_post(self):
        f = FrontFixture(positions={100: 999, 101: 0}, assigned={1000: {100, 101}})
        with f.reads(), self.assertRaisesRegex(ops.OpError, "can reach none"):
            ops._assign_nearest(f.g, f.mv, {}, 1830)
        self.assertEqual(f.calls, [])

    def test_post_success_without_readback_assignment_is_an_error(self):
        f = FrontFixture(assigned={1000: range(100, 124)})
        f.ao.assign_to_order = lambda *a: (True, "")
        with f.reads(), self.assertRaisesRegex(ops.OpError, "assign"):
            ops._assign_nearest(f.g, f.mv, {}, 1830)

    def test_unchanged_cut_repairs_assignments_and_preserves_orders_and_attacks(self):
        f = FrontFixture(assigned={1000: range(100, 124)})
        before = dict(f.fields)
        pieces = [{"line": line, "idx": list(range(len(line))), "fid": 10 + i,
                   "sec": i, "front": 0x10000 + i} for i, line in enumerate(f.lines.values())]
        with f.reads(), patch.object(ops, "theatre_armies", return_value=[1830]), \
                patch.object(ops, "_hid", return_value=1830), \
                patch.object(ops, "theatre_line", return_value=pieces), \
                patch.object(ops, "forward_of_defence", return_value=set()), \
                patch.object(ops, "_ctl", return_value=lambda pr: "SOV"), \
                patch.object(ops.B, "module", return_value=types.SimpleNamespace(enemy_tags=lambda g: [])), \
                patch.object(ops.B, "arr", return_value=[0x9000]), \
                patch.object(ops, "_arrows_of", return_value=([89, 80], [])), \
                patch.object(ops, "_redraw", side_effect=f.forbid):
            result = ops._cut_front.__wrapped__(f.g, f.mv, {}, 201, {89}, [(1830, 1)],
                                                drop_attacks=False)
        self.assertTrue(result[1830]["unchanged"])
        self.assertEqual([len(f.assigned[o]) for o in f.orders], [4, 12, 8])
        self.assertEqual(f.fields, before)


if __name__ == "__main__":
    unittest.main()
