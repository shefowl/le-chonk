"""Offline native-panel regressions: full directives, navigation and send feedback."""
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import modui
from harness.ui_text import pixel_width

ROOT = Path(__file__).resolve().parents[1]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class NativePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.path = Path(cls.directory.name)
        cls.build = module('mod/build.py', 'polish_build')
        cls.build.build(output=cls.path)
        cls.preview = module('tools/preview_gui.py', 'polish_preview')
        data = cls.preview.parse((cls.path / 'common/scripted_guis/le_chonk_scripted_gui.txt').read_text())
        cls.panel = cls.preview.get(cls.preview.get(data, 'scripted_gui'), 'lc_panel')
        ui = cls.preview.get(cls.preview.parse((cls.path / 'interface/le_chonk.gui').read_text()), 'guiTypes')
        cls.window = next(v for k, op, v in ui if cls.preview.get(v, 'name') == 'lc_panel_window')

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_agent_detail_keeps_staff_section_selected(self):
        trigger = self.preview.get(self.preview.get(self.panel, 'triggers'), 'lc_tab_staff_active_visible')
        self.assertTrue(self.preview.condition(trigger, {'lc_tab_agent', 'lc_online'}, {}))

    def test_send_feedback_has_visible_widget(self):
        widgets = [v for k, op, v in self.window if k == 'instantTextBoxType']
        feedback = next((v for v in widgets if self.preview.get(v, 'text') == 'LC_SENT'), None)
        self.assertIsNotNone(feedback, 'SEND errors have no visible surface')
        trigger = self.preview.get(self.preview.get(self.panel, 'triggers'), self.preview.get(feedback, 'name') + '_visible')
        self.assertTrue(self.preview.condition(trigger, {'lc_tab_agent', 'lc_online'}, {}))

    def test_full_directives_replace_chat_without_overlaying_it(self):
        trigger = self.preview.get(self.preview.get(self.panel, 'triggers'), 'lc_chat_text_visible')
        self.assertFalse(self.preview.condition(trigger, {'lc_tab_agent', 'lc_directives_open'}, {}))
        def find_text(items, text):
            for k, op, v in items:
                if k == 'instantTextBoxType' and self.preview.get(v, 'text') == text:
                    return v
                if k == 'containerWindowType' and isinstance(v, list):
                    res = find_text(v, text)
                    if res is not None:
                        return res
            return None
        reader = find_text(self.window, 'LC_DR_TEXT')
        self.assertIsNotNone(reader)
        trigger = self.preview.get(self.preview.get(self.panel, 'triggers'), self.preview.get(reader, 'name') + '_visible')
        self.assertTrue(self.preview.condition(trigger, {'lc_tab_agent', 'lc_directives_open'}, {}))

    def test_back_returns_to_the_screen_that_opened_contact(self):
        effects = self.preview.get(self.panel, 'effects')

        def apply(actions, flags, variables):
            for name, op, value in actions:
                if name == 'clr_country_flag': flags.discard(value)
                elif name == 'set_country_flag': flags.add(value)
                elif name == 'set_variable':
                    for key, _, val in value:
                        variables[key] = variables.get(val, float(val) if val.isdigit() else 0)
                elif name == 'if':
                    limit = self.preview.get(value, 'limit')
                    if self.preview.condition(limit, flags, variables):
                        apply([part for part in value if part[0] != 'limit'], flags, variables)

        for source, control in [('agents', 'lc_open_agent_industry_click'), ('staff', 'lc_sf_open_click')]:
            flags = set() if source == 'agents' else {'lc_tab_staff'}
            variables = {'lc_card': 4}
            apply(self.preview.get(effects, control), flags, variables)
            self.assertIn('lc_tab_agent', flags)
            apply(self.preview.get(effects, 'lc_ag_back_click'), flags, variables)
            self.assertTrue(self.preview.condition(self.preview.parse('value = { ' + self.build.tab_cond(source) + ' }')[0][2], flags, variables))

    def test_previous_page_is_disabled_on_first_page(self):
        triggers = self.preview.get(self.panel, 'triggers')
        for control, variable in [('lc_plan_prev', 'lc_plan_page'), ('lc_directive_prev', 'lc_directive_page'),
                                  ('lc_feed_newer', 'lc_feed_page'), ('lc_chat_newer', 'lc_chat_page')]:
            trigger = self.preview.get(triggers, control + '_click_enabled')
            self.assertIsNotNone(trigger)
            self.assertFalse(self.preview.condition(trigger, set(), {variable: 0}))
            self.assertTrue(self.preview.condition(trigger, set(), {variable: 1}))


class DirectiveReaderTests(unittest.TestCase):
    def test_every_long_directive_round_trips_across_reader_pages(self):
        ui = modui.ModUI(None)
        directives = [' '.join('директива%d_%d' % (n, i) for i in range(160)) for n in range(4)]
        with patch.object(ui, 'set_page', side_effect=lambda var, page: setattr(ui, 'directive_page', page)):
            pages = ui.directive_texts(directives, 'ru')
            count = int(pages['DR_PAGE'].split('/')[1].split()[0])
            self.assertGreater(count, 1)
            rendered = []
            for page in range(count):
                ui.directive_page = page
                data = ui.directive_texts(directives, 'ru')
                text = data['DR_TEXT']
                self.assertLessEqual(len(text.splitlines()), 240)
                self.assertTrue(all(pixel_width(line, 'ru') <= 756 for line in text.splitlines()))
                with patch.object(modui, '_lang', return_value='ru'):
                    self.assertEqual(re.sub(r'(?:\x11W)+$', '', modui._fit_lines(text, 9000).decode()).rstrip(), text)
                rendered.append(text)
        expected = ' '.join('%d. %s' % (i + 1, text) for i, text in enumerate(directives))
        self.assertEqual(' '.join(' '.join(rendered).split()), expected)

    def test_empty_input_and_unreadable_input_have_distinct_feedback(self):
        ui = modui.ModUI(None)
        ui.agent = 'industry'
        ui.online = True
        ui.on_message = lambda *args: True
        with patch.object(ui, '_flag', side_effect=lambda name: name == 'lc_send'), \
                patch.object(ui, '_effect'), patch.object(ui, 'write') as write, \
                patch.object(ui, 'read_input', return_value=''), patch.object(modui, '_lang', return_value='en'):
            ui.requests()
            empty = write.call_args[0][0]['SENT']
        with patch.object(ui, '_flag', side_effect=lambda name: name == 'lc_send'), \
                patch.object(ui, '_effect'), patch.object(ui, 'write') as write, \
                patch.object(ui, 'read_input', return_value=None), patch.object(modui, '_lang', return_value='en'):
            ui.requests()
            unreadable = write.call_args[0][0]['SENT']
        self.assertNotEqual(empty, unreadable)
        self.assertIn('empty', empty.lower())
        self.assertIn('read', unreadable.lower())


class PreviewGeometryTests(unittest.TestCase):
    def test_long_unbreakable_token_is_reported_as_overflow(self):
        from PIL import Image
        preview = module('tools/preview_gui.py', 'geometry_preview')
        font = preview.BitmapFont('hoi_16mbs')
        self.assertTrue(font.draw(Image.new('RGBA', (100, 100)), 'W' * 100, (0, 0), 20, 20))


class CompactTextTests(unittest.TestCase):
    def clip(self, *args, **kwargs):
        from harness import ui_text
        self.assertTrue(callable(getattr(ui_text, 'ellipsize_coloured', None)))
        return ui_text.ellipsize_coloured(*args, **kwargs)

    def test_long_russian_preview_fits_pixels_and_actual_marker_bytes(self):
        text = 'Приоритет действующих дивизий; производство и снабжение. ' * 20
        clipped = self.clip(text, 398, language='ru', max_bytes=100)
        self.assertTrue(clipped.endswith('...'))
        self.assertLessEqual(pixel_width(clipped, 'ru'), 398)
        self.assertLessEqual(len(clipped.encode()), 100)
        with patch.object(modui, '_lang', return_value='ru'):
            self.assertEqual(re.sub(r'(?:\x11W)+$', '', modui._fit(clipped, 100).decode()).rstrip(), clipped)

    def test_coloured_utf8_preview_closes_colour_before_ellipsis(self):
        text = '\x11R' + 'Противник наступает ' * 20 + '\x11!'
        clipped = self.clip(text, 200, language='ru', max_bytes=80)
        self.assertTrue(clipped.startswith('\x11R'))
        self.assertTrue(clipped.endswith('\x11!...'))
        self.assertIsNone(re.search('\x11(?:$|[^A-Za-z0-9!])', clipped))
        self.assertLessEqual(len(clipped.encode()), 80)

    def test_short_russian_text_keeps_its_final_character_after_encoding(self):
        clipped = self.clip('Готово', 398, language='ru', max_bytes=100)
        self.assertEqual(re.sub('[\x11§].', '', clipped), 'Готово')
        self.assertFalse(clipped.endswith('...'))
        with patch.object(modui, '_lang', return_value='ru'):
            self.assertEqual(re.sub(r'(?:\x11W)+$', '', modui._fit(clipped, 100).decode()).rstrip(), clipped)


if __name__ == '__main__':
    unittest.main()
