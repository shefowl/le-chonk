"""Scripted-effect lookup must not compile every effect name for every decision."""
import time
import unittest
from unittest.mock import patch
from harness import spirits

class EffectLookupTests(unittest.TestCase):
    def test_nested_identifier_matches_existing_word_boundary_semantics(self):
        effects={'effect':{'a'},'prefix.effect':{'b'},'.effect':{'c'},
                 'other':{'d'},'effect_no':{'e'}}
        with patch.object(spirits,'scripted_effects',return_value=effects):
            self.assertEqual(spirits._removed_deep('remove_ideas = direct\nprefix.effect = yes\nother = no'), {'direct','a','b','c'})

    def test_lookup_cost_is_not_effect_count_times_decision_count(self):
        effects={'country_effect_%d'%i:{'idea_%d'%i} for i in range(8000)}
        with patch.object(spirits,'scripted_effects',return_value=effects):
            start=time.monotonic()
            for _ in range(10):
                self.assertEqual(spirits._removed_deep('country_effect_7999 = yes'),{'idea_7999'})
            elapsed=time.monotonic()-start
        self.assertLess(elapsed,.3,'effect lookup recompiles thousands of regexes per decision')
