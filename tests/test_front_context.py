"""Offline regression coverage for fresh, complete theatre briefings."""
import collections
import types
import unittest
from unittest.mock import patch

from harness import briefing, front, ops


class FrontContextTests(unittest.TestCase):
    def context(self):
        return {"date": "1941.06.18.03", "mv": object(),
                "gw": types.SimpleNamespace(run=lambda fn, *a: fn(None, *a))}

    def test_same_context_shares_a_snapshot(self):
        ctx = self.context()
        with patch.object(front, "Front", side_effect=lambda *a: object()):
            self.assertIs(front.of(ctx, "GER"), front.of(ctx, "GER"))

    def test_new_context_on_same_date_reads_a_new_snapshot(self):
        first, second = self.context(), self.context()
        with patch.object(front, "Front", side_effect=lambda *a: object()):
            self.assertIsNot(front.of(first, "GER"), front.of(second, "GER"))

    def test_responsibility_includes_border_without_drawn_sector(self):
        fr = types.SimpleNamespace(our_line={1, 2, 3},
                                   mv=types.SimpleNamespace(state_of={1: 230, 2: 229, 3: 584}))
        zone = front.responsibility_zone(fr, {230, 229})
        self.assertEqual(zone, {1, 2})


class TheatreBriefingTests(unittest.TestCase):
    def setUp(self):
        self.west = types.SimpleNamespace(theatre={"id": 201, "name": "West", "enemy": "GER"},
                                          region=lambda ctx: {94})
        self.east = types.SimpleNamespace(theatre={"id": 204, "name": "Far East", "enemy": "JAP"},
                                          region=lambda ctx: {408})
        self.ctx = {"date": "1941.06.18.03", "enemies": {"GER"},
                    "marshals": {201: self.west, 204: self.east},
                    "holders": {}, "mv": types.SimpleNamespace(state_of={1: 94, 2: 408}),
                    "gw": types.SimpleNamespace(run=lambda fn, *a: fn(None, *a))}

    def thin_lines(self):
        # Empty Sakhalin must not erase the other 73 divisions from the comparison.
        weigh = [(201, "West", 144, 35, 113, 182, 9, {94}),
                 (204, "Far East", 73, 26, 48, 0, 1, {408})]
        lines = []
        with patch.object(briefing, "_stakes", return_value={}), \
             patch.object(briefing._t(), "quiet", side_effect=lambda c, m: m is self.east):
            briefing._thin(self.ctx, weigh, lines)
        return "\n".join(lines)

    def test_thin_comparison_keeps_quiet_theatre_with_holes(self):
        text = self.thin_lines()
        self.assertIn("Far East", text)
        self.assertIn("73 assigned", text)
        self.assertIn("26 line provinces", text)
        self.assertIn("48 on line", text)
        self.assertIn("0 enemy", text)
        self.assertIn("1 empty", text)

    def test_arithmetic_excess_is_not_reported_as_available_divisions(self):
        text = self.thin_lines()
        self.assertIn("headcount minus line 47", text)
        self.assertIn("not verified available divisions", text)
        self.assertNotIn("move_to_theatre", text)

    def test_compact_overview_contains_other_theatres_local_counts_and_war_status(self):
        mv = types.SimpleNamespace(state_of={1: 94, 2: 408}, adj={1: {11}, 2: {12}})
        fr = types.SimpleNamespace(mv=mv, our_line={1, 2}, their_line={11, 12},
                                   n_div=collections.defaultdict(int, {("us", 1): 5}),
                                   divisions=lambda side, ps: sum(({1: 5, 2: 0} if side == "us"
                                                                   else {11: 40, 12: 0}).get(p, 0)
                                                                  for p in ps))
        self.ctx["mv"] = mv
        overview = briefing._theatre_overview
        def read(g, mv, h, tid, enemies):
            return {"divisions": 144 if tid == 201 else 73}
        with patch.object(front, "of", return_value=fr), \
             patch.object(ops, "command_view", side_effect=read), \
             patch.object(briefing._t(), "facing", side_effect=lambda c, m: {m.theatre["enemy"]}), \
             patch.object(briefing._t(), "line_of", side_effect=lambda c, m: m.theatre["enemy"]):
            text = "\n".join(overview(self.ctx, 201))
            all_text = "\n".join(overview(self.ctx, None))
        self.assertIn("204 Far East", text)
        self.assertIn("73 assigned", text)
        self.assertIn("1 line provinces", text)
        self.assertIn("0 on line", text)
        self.assertIn("1 empty", text)
        self.assertIn("PEACE", text)
        self.assertNotIn("201 West", text)
        self.assertIn("WAR WITH GER", all_text)

    def test_supreme_counts_full_responsibility_when_no_sector_exists(self):
        self.ctx["marshals"] = {201: self.west}
        self.ctx["mv"] = mv = types.SimpleNamespace(label=str)
        fr = types.SimpleNamespace(our_line={1, 2}, their_line={11},
                                   mv=types.SimpleNamespace(state_of={1: 94, 2: 94},
                                                            adj={1: {11}, 2: {11}}),
                                   n_div=collections.defaultdict(int, {("us", 1): 5}),
                                   divisions=lambda side, ps: 5 if side == "us" and 1 in ps
                                   else 20 if side == "them" and 11 in ps else 0)
        self.west.name = "Commander"
        self.west.theatre["directive"] = "Hold"
        views = {"divisions": 24, "armies": []}
        from harness import garrison
        lines = []
        with patch.object(ops, "theatre_listing", return_value={}), \
             patch.object(garrison, "lines", return_value=[]), \
             patch.object(ops, "command_view", return_value=views), \
             patch.object(ops, "unassigned_summary", return_value={"total": 0}), \
             patch.object(front, "of", return_value=fr), \
             patch.object(front, "group_zone", side_effect=AssertionError("drawn sectors used")), \
             patch.object(front, "summary", side_effect=lambda f, z: "coverage " + str(sorted(z))), \
             patch.object(briefing, "_stakes", return_value={}), \
             patch.object(briefing, "_main_effort_missing", return_value=[]), \
             patch.object(briefing._t(), "quiet", return_value=False), \
             patch.object(briefing._t(), "facing", return_value={"GER"}), \
             patch.object(briefing._t(), "at_war_in", return_value=set()), \
             patch.object(briefing._t(), "line_of", return_value="GER"):
            briefing._groups(self.ctx, {}, {"army_groups": [], "armies": []}, lines)
        text = "\n".join(lines)
        self.assertIn("coverage [1, 2]", text)
        self.assertIn("1 of its 2 line provinces EMPTY", text)


if __name__ == "__main__":
    unittest.main()
