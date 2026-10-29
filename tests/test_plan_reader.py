"""The native PLAN view must expose the complete saved plan, not just its idea."""
import unittest
import re
import types
from unittest.mock import patch
from harness import run, modui

class PlanReaderTests(unittest.TestCase):
    def test_readiness_label_records_snapshot_date_instead_of_claiming_current(self):
        r=run.Runner.__new__(run.Runner)
        r.planner=types.SimpleNamespace(plan={'wars':[{'enemy':'GER'}]})
        r.ui=types.SimpleNamespace(plan_page=0,reader_bounds=lambda *args:None)
        r.mv=object();r.ctx=lambda:{'date':'1941.06.18.03'}
        with patch('harness.chat.game_language',return_value='en'), \
             patch('harness.warplan.readiness_line',return_value='READY'), \
             patch('harness.warplan.render',return_value='War with GER'):
            r.plan_texts()
        self.assertIn('1941.06.18.03',' '.join(r._plan_cards))
        self.assertNotIn('CURRENT READINESS',' '.join(r._plan_cards))

    def test_text_only_war_plan_and_all_ministry_sections_are_present(self):
        self.assertTrue(callable(getattr(run,'plan_cards',None)))
        plan={k:'UNIQUE_'+k for k in ('economy','research','production','politics','army','air','navy','war_plan')}
        cards=run.plan_cards(plan,'en')
        for value in plan.values():self.assertIn(value,' '.join(cards))
        self.assertTrue(all(len(c.encode())<=520 and len(c.splitlines())<=3 for c in cards))

    def test_long_russian_section_survives_actual_marker_encoding(self):
        self.assertTrue(callable(getattr(run,'plan_cards',None)))
        text=' '.join('подготовка%d'%i for i in range(300))
        cards=run.plan_cards({'army':text},'ru')
        payload=[]
        for card in cards:
            encoded=re.sub(r'(?:\x11W)+$', '', modui._fit_lines(card,520).decode('utf-8')).rstrip()
            self.assertEqual(encoded,card)
            payload.extend(card.split('\n')[1:])
        self.assertEqual(' '.join(payload).split(),text.split())

    def test_native_page_buttons_do_not_toggle_roster(self):
        self.assertIn('plan_page',modui.BUILD.WHAT)
        for action in ('lc_plan_prev','lc_plan_next'):
            self.assertEqual(modui.BUILD.what_of(action),modui.BUILD.WHAT['plan_page'])
