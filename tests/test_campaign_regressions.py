"""Offline regressions: no process attachment or game commands."""
import types
import unittest
from unittest.mock import patch

from harness import economy, navy
from harness.bridge import module


class FuelHistoryTests(unittest.TestCase):
    def setUp(self):
        self.saved = economy._FUEL_LOG[:]
        self.addCleanup(lambda: economy._FUEL_LOG.__setitem__(slice(None), self.saved))

    @patch.object(economy, "fuel", return_value=(1600, 10000))
    def test_balance_uses_only_samples_after_last_rewind(self, _fuel):
        economy._FUEL_LOG[:] = [(100, 9000), (120, 1000), (105, 1000), (106, 1300)]
        self.assertEqual(economy.fuel_report(None), (1600, 10000, 300, None))

    @patch.object(economy, "fuel", return_value=(1600, 10000))
    def test_first_sample_after_rewind_has_no_measured_balance(self, _fuel):
        economy._FUEL_LOG[:] = [(100, 9000), (120, 1000), (105, 1600)]
        self.assertEqual(economy.fuel_report(None), (1600, 10000, None, None))

    @patch.object(economy, "fuel", return_value=(1600, 10000))
    def test_recording_rewind_starts_a_new_history(self, _fuel):
        economy._FUEL_LOG[:] = [(100, 9000), (120, 1000)]
        economy.track_fuel(None, 105)
        self.assertEqual(economy._FUEL_LOG, [(105, 1600)])

    @patch.object(economy, "fuel", return_value=(1600, 10000))
    def test_normal_history_keeps_30_day_window(self, _fuel):
        economy._FUEL_LOG[:] = [(60, 10000), (100, 2400), (104, 1600)]
        self.assertEqual(economy.fuel_report(None), (1600, 10000, -200, 8))


class NavalCommandTests(unittest.TestCase):
    @patch.object(navy, "_post", return_value=None)
    def test_mission_remaining_on_hold_is_an_error(self, _post):
        game = types.SimpleNamespace(p=types.SimpleNamespace(i32=lambda addr: 0))
        result = navy.set_task_force_mission(game, 0x1000, "strike_force")
        self.assertIn("error", result)
        self.assertIn("strike_force", result["error"])
        self.assertIn("hold", result["error"])

    @patch.object(navy, "_post", return_value=None)
    def test_confirmed_mission_succeeds(self, _post):
        game = types.SimpleNamespace(p=types.SimpleNamespace(i32=lambda addr: 2))
        self.assertEqual(navy.set_task_force_mission(game, 0x1000, "strike_force"),
                         {"mission": "strike_force"})

    @patch.object(navy.time, "sleep")
    def test_disallowed_command_is_not_posted(self, _sleep):
        # Exercise the real CanExecute helper with a synthetic command vtable.
        # Only process memory and injected calls are replaced.
        calls = []
        p = types.SimpleNamespace(base=0, write=lambda *args: None,
                                  u64=lambda addr: {0x100: 0x200, 0x250: 0x300}[addr])
        game = types.SimpleNamespace(
            p=p, _inj=object(), malloc=lambda size: 0x100,
            call=lambda fn, args: calls.append(fn) or 0,
            cmd=types.SimpleNamespace(post=lambda cmd: calls.append("post")))
        real_module = module
        api = types.SimpleNamespace(allow_call=lambda fn: None)
        # industry imports api directly; patch its allowlist mutation, not the check.
        real_module("industry")
        with patch.object(real_module("api"), "allow_call"), \
                patch.object(navy, "B", side_effect=lambda name:
                             api if name == "api" else real_module(name)):
            result = navy._post(game, 0x400, 0x48, [], lambda cmd: None)
        self.assertNotIn("post", calls)
        self.assertEqual(calls, [0x400, 0x300])
        self.assertIn("CanExecute", result)

    @patch.object(navy.time, "sleep")
    def test_allowed_command_is_posted_after_field_and_game_checks(self, _sleep):
        calls = []
        p = types.SimpleNamespace(base=0, write=lambda *args: None,
                                  u64=lambda addr: {0x100: 0x200, 0x250: 0x300}[addr])
        game = types.SimpleNamespace(
            p=p, _inj=object(), malloc=lambda size: 0x100,
            call=lambda fn, args: calls.append(fn) or 1,
            cmd=types.SimpleNamespace(post=lambda cmd: calls.append("post")))
        real_module = module
        real_module("industry")
        with patch.object(real_module("api"), "allow_call"):
            result = navy._post(game, 0x400, 0x48, [],
                                lambda cmd: calls.append("fields"))
        self.assertIsNone(result)
        self.assertEqual(calls, [0x400, "fields", 0x300, "post"])


if __name__ == "__main__":
    unittest.main()
