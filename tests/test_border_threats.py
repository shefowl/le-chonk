"""Offline coalition and visibility regressions; no game attachment or commands."""
import collections
import contextlib
import types
import unittest
from unittest.mock import patch

from harness import front, front_threats, ops, zone


class BorderFixture:
    """Hungary's border, visible rear and hidden rear opposite one Soviet province."""
    units = {"SOV": [1], "GER": [6], "HUN": [2, 3, 4], "PER": []}

    def player_tag(self):
        return "SOV"

    def faction_members(self, tag):
        if tag in {"GER", "HUN"}:
            return {"faction": "Axis", "members": ["GER", "HUN"]}
        return {"faction": None, "members": []}

    def country(self, tag):
        return types.SimpleNamespace(ptr=tag)

    def __init__(self):
        self.p = None
        self.provinces = {89: {1}, 10: {2, 3, 4}, 20: {6}, 30: {7}}
        self.holders = {89: ("SOV", "SOV"), 10: ("HUN", "HUN"),
                        20: ("GER", "GER"), 30: ("PER", "PER")}
        self.mv = types.SimpleNamespace(
            land={1, 2, 3, 4, 6, 7},
            adj={1: {2, 6, 7}, 2: {1, 3}, 3: {2, 4}, 4: {3}, 6: {1}, 7: {1}},
            state_of={1: 89, 2: 10, 3: 10, 4: 10, 6: 20, 7: 30},
            land_provinces=lambda st: self.provinces[st], label=lambda st: str(st))

    def bridge(self, name):
        if name == "game":
            return types.SimpleNamespace(_arr=lambda p, tag, off: self.units[tag])
        if name == "combat":
            return types.SimpleNamespace(enemy_tags=lambda g, tag=None: [],
                                         subject_tags=lambda g, tag=None: [])
        raise AssertionError(name)

    def power_at(self, game, tags):
        return collections.Counter(q for t in tags for q in self.units[t])

    def readings(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(zone, "B", self.bridge))
        stack.enter_context(patch.object(ops, "province_of", lambda game, division: division))
        stack.enter_context(patch.object(front, "power_at", self.power_at))
        return stack


class BorderVisibilityTests(unittest.TestCase):
    def test_expected_faction_members_are_seen_only_within_visibility(self):
        game = BorderFixture()
        with game.readings():
            seen = zone.seen_by_state(game, game.mv, game.holders, {89}, "GER")
        # HUN p2 is on the border, p3 one province behind, p4 hidden three steps away.
        self.assertEqual(seen, {10: 2, 20: 1})

    def test_briefing_identifies_expected_coalition_and_its_visible_troops(self):
        game = BorderFixture()
        with game.readings():
            lines = zone.picture(game, game.mv, game.holders, {89}, expected="GER")
        hungary = [line for line in lines if "HUN" in line]
        self.assertEqual(len(hungary), 1)
        self.assertIn("same faction as GER", hungary[0])
        self.assertIn("at peace with us", hungary[0])
        self.assertIn("1 divisions seen", hungary[0])
        self.assertIn("1 more", hungary[0])
        self.assertFalse(any(line.startswith("NEUTRAL HUN") for line in lines))
        self.assertTrue(any("NEUTRAL PER" in line for line in lines))
        self.assertTrue(any("ZONE" in line and ": 3, worth 3" in line for line in lines))

    def test_strength_picture_does_not_reveal_the_hidden_coalition_rear(self):
        game = BorderFixture()
        with game.readings():
            worth = zone.seen_by_state(game, game.mv, game.holders, {89}, "GER", worth=True)
        self.assertEqual(worth, {10: 2, 20: 1})


class ExpectedThreatTests(unittest.TestCase):
    def test_coalition_keeps_explicit_tags_and_adds_public_faction_members(self):
        game = BorderFixture()
        self.assertEqual(front_threats.coalition(game, "ger+fin"), {"GER", "HUN", "FIN"})

    def test_unreadable_diplomacy_keeps_explicit_border_threats(self):
        def unavailable(tag):
            raise OSError("no faction pointer")
        game = types.SimpleNamespace(faction_members=unavailable)
        self.assertEqual(front_threats.coalition(game, {"GER", "HUN"}), {"GER", "HUN"})

    def test_context_union_includes_plan_and_theatre_multi_country_specs(self):
        ctx = {"enemies": ["GER"], "coming_wars": {"JAP"}, "war_wanted": ["ROM"],
               "planner": types.SimpleNamespace(plan={"wars": [{"enemy": "GER+HUN"}]}),
               "marshals": {201: types.SimpleNamespace(theatre={"enemy": "FIN+SWE"})}}
        self.assertTrue(callable(getattr(front_threats, "expected", None)),
                        "the runner and alerts need one expected threat set")
        self.assertEqual(front_threats.expected(ctx), {"GER", "HUN", "JAP", "ROM", "FIN", "SWE"})

    def test_runner_snapshot_is_authoritative_including_an_empty_snapshot(self):
        self.assertTrue(callable(getattr(front_threats, "expected", None)))
        self.assertEqual(front_threats.expected({"expected_enemies": {"GER", "HUN"}}),
                         {"GER", "HUN"})
        self.assertEqual(front_threats.expected({"expected_enemies": [], "enemies": ["GER"]}),
                         set())


if __name__ == "__main__":
    unittest.main()
