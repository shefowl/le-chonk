"""Native panel text uses real runtime paths, not hand-picked preview captions."""
import collections
import types
import unittest
from unittest.mock import patch

from harness import modui, roster, run, ui_text


class RuntimePanelTests(unittest.TestCase):
    def runner(self):
        r = run.Runner.__new__(run.Runner)
        r.gw = types.SimpleNamespace(run=lambda fn: {'text': '1941.06.18.03'})
        r.at_war = {'GER', 'FIN'}; r._holds = 1
        r._land_war = (0, True)
        r.last_said = {'army': '1941.06.18: ' + 'Очень длинное донесение ' * 40}
        r.setting = lambda key: 4
        r.marshals = {201: types.SimpleNamespace(theatre={
            'id': 201, 'name': 'Западный фронт', 'enemy': 'GER', 'executing': False},
            name='Михаил Тухачевский')}
        r.feed_texts = lambda: {}; r.agent_texts = lambda: {}; r.plan_texts = lambda full: {}
        return r

    def test_runtime_captions_are_russian_and_short_rows_mark_truncation(self):
        r = self.runner()
        with patch.object(run.chat, 'game_language', return_value='ru'):
            texts = r.panel_texts()
        self.assertIn('война', texts['HEAD'])
        self.assertNotIn('at war', texts['HEAD'])
        self.assertNotIn('every theatre', texts['TH_MODE'])
        self.assertIn('агент', texts['ST_POLITICS'])
        self.assertTrue(texts['ST_ARMY'].endswith('...'))
        self.assertLessEqual(len(texts['ST_ARMY'].encode()), modui.BUILD.MARKERS['ST_ARMY'])
        self.assertLessEqual(ui_text.pixel_width(texts['ST_ARMY'], 'ru'), 398)
        self.assertLessEqual(ui_text.pixel_width(texts['TH_1'], 'ru'), 776)

    def test_filtered_empty_reports_explain_empty_state(self):
        r = self.runner()
        r.feed = collections.deque([('1941.06.18', 'army', 'A report', False)])
        r.ui = types.SimpleNamespace(muted={'army'}, important=False, feed_page=0, reader_bounds=lambda *args:None)
        with patch.object(run.chat, 'game_language', return_value='ru'):
            text = run.Runner.feed_texts(r)
        self.assertIn('нет', text['FEED'].lower())
        self.assertIn('0', text['FD_PAGE'])

    def test_send_feedback_is_initialized_and_survives_regular_refresh(self):
        r = self.runner(); r.ui = types.SimpleNamespace(shown={})
        self.assertEqual(r.panel_texts().get('SENT'), '')
        r.ui.shown['SENT'] = 'Could not read the input. Try again.'
        self.assertEqual(r.panel_texts()['SENT'], r.ui.shown['SENT'])

    def test_full_directive_reader_is_wired_to_actual_agent_directives(self):
        r = self.runner()
        del r.agent_texts
        ui = modui.ModUI(None); ui.agent = 'planner'
        r.ui = ui
        dirs = ['Директива %d ' % n + 'длинный текст ' * 20 for n in range(4)]
        r.chat = types.SimpleNamespace(busy={}, shown=lambda key: dirs, log=lambda key: [])
        with patch.object(run.chat, 'game_language', return_value='ru'):
            text = r.agent_texts()
        self.assertIn('Директива 0', text['DR_TEXT'])
        self.assertTrue(text['DR_PAGE'])
        self.assertLessEqual(ui_text.pixel_width(text['DR_1'], 'ru'), 776)
        self.assertTrue(text['DR_1'].endswith('...'))


class FreshContextTests(unittest.TestCase):
    def test_background_rebrief_drops_old_private_snapshot_caches(self):
        r = run.Runner.__new__(run.Runner)
        r.gw = types.SimpleNamespace(run=lambda fn: {'day': 5, 'text': '5'}
                                   if fn is run.ops.date else ['GER'])
        r.hold = lambda: None
        r.ctx = lambda: {'day': 5, 'date': '5', 'enemies': ['GER']}
        r.planner = types.SimpleNamespace(plan_for=lambda name: '')
        r._war_part = lambda *a: ''
        r.journal = types.SimpleNamespace(write=lambda **kw: None)
        c = {'day': 1, 'date': '1', 'enemies': ['GER'], '_fronts': {'old': object()},
             '_regions': {1: {2}}, ('_facing', 1): {'JAP'}, 'before_orders': 'keep'}
        with patch.object(run, 'day_of', side_effect=lambda d: d['day']):
            text = r.freshness_guard(types.SimpleNamespace(name='staff'), c,
                                     lambda ctx: 'fresh', [])()
        self.assertEqual(text, 'fresh')
        self.assertNotIn('_fronts', c)
        self.assertNotIn('_regions', c)
        self.assertNotIn(('_facing', 1), c)
        self.assertEqual(c['before_orders'], 'keep')


if __name__ == '__main__': unittest.main()
