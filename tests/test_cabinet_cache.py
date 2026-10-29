"""Native availability cache invalidation, exercised without a game connection."""
import types
import unittest
from unittest.mock import patch

from harness.bridge import module

cabinet = module("cabinet")
combat = module("combat")


class CabinetCacheTests(unittest.TestCase):
    def setUp(self):
        self.country = types.SimpleNamespace(tag_id=1, political_power=114)
        self.hour = 24
        self.enemies = []
        self.event_available = False
        self.evaluations = 0
        self.g = types.SimpleNamespace(_c=lambda tag: self.country,
                                       date_raw=lambda: self.hour)
        self.wars = patch.object(combat, "enemy_tags", side_effect=lambda g, tag=None: self.enemies)
        self.wars.start()
        self.addCleanup(self.wars.stop)

        @cabinet._daily
        def availability(g, tag=None):
            self.evaluations += 1
            return {"hireable": ["advisor"] if self.country.political_power >= 150 else [],
                    "laws": ["war_economy"] if self.enemies else [],
                    "events": ["decision"] if self.event_available else []}
        self.availability = availability

    def test_pp_threshold_opens_availability_in_same_hour_and_week(self):
        self.assertEqual(self.availability(self.g)["hireable"], [])
        self.country.political_power = 150
        self.assertEqual(self.availability(self.g)["hireable"], ["advisor"])

    def test_war_change_opens_availability_in_same_hour_and_week(self):
        self.assertEqual(self.availability(self.g)["laws"], [])
        self.enemies = ["GER"]
        self.assertEqual(self.availability(self.g)["laws"], ["war_economy"])
        self.enemies = []
        self.assertEqual(self.availability(self.g)["laws"], [])

    def test_other_game_changes_expire_after_one_game_hour(self):
        self.assertEqual(self.availability(self.g)["events"], [])
        self.event_available = True
        self.hour += 1
        self.assertEqual(self.availability(self.g)["events"], ["decision"])

    def test_unchanged_epoch_does_not_repeat_expensive_checks(self):
        self.availability(self.g)
        self.availability(self.g)
        self.assertEqual(self.evaluations, 1)

    def test_cached_result_is_deep_copied_on_every_return(self):
        first = self.availability(self.g)
        first["hireable"].append("caller mutation")
        second = self.availability(self.g)
        self.assertEqual(second["hireable"], [])
        second["laws"].append("another caller mutation")
        self.assertEqual(self.availability(self.g)["laws"], [])
        self.assertEqual(self.evaluations, 1)

    def test_unreadable_epoch_does_not_reuse_unverifiable_availability(self):
        self.assertEqual(self.availability(self.g)["events"], [])
        self.event_available = True
        with patch.object(combat, "enemy_tags", side_effect=OSError("unreadable diplomacy")):
            self.assertEqual(self.availability(self.g)["events"], ["decision"])
        self.assertEqual(self.availability(self.g)["events"], ["decision"])


if __name__ == "__main__":
    unittest.main()
