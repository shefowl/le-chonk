"""Human offline defaults and native text scrolling, without game writes."""
import importlib.util
import tempfile
import unittest
import re
from pathlib import Path
from harness import modui

ROOT = Path(__file__).resolve().parents[1]

class DefaultsTests(unittest.TestCase):
    def test_markers_survive_native_6144_byte_localisation_copy(self):
        for key in ('PL_TEXT', 'CHAT', 'FEED', 'DR_TEXT'):
            value = modui.BUILD.marker(key).encode()
            native = value[:6144]
            self.assertEqual(list(modui.marker_buffers(native)), [(key, 0, native)])

    def test_running_previous_ui_truncated_buffer_is_discovered_by_full_content(self):
        # Captured engine copy: 6144 bytes, with the identity tail truncated away.
        text = b'The planner has not submitted a plan yet. Connect headquarters to receive the strategic briefing.'
        native = text + b' ' + b'\x11W' * ((6144-len(text)-1)//2)
        self.assertEqual(len(native), 6144)
        self.assertEqual(list(modui.marker_buffers(native)), [('PL_TEXT', 0, native)])
        self.assertEqual(list(modui.marker_buffers(b'X' + native[1:])), [])

    def test_fallbacks_are_exact_byte_buffers_without_machine_labels(self):
        for lang in ('english', 'russian'):
            values = []
            for key, size in modui.BUILD.MARKERS.items():
                value = modui.BUILD.marker(key, lang)
                self.assertEqual(len(value.encode()), size, (key, lang))
                self.assertNotIn('LCM_', value)
                self.assertNotIn('...', value)
                values.append(value)
            self.assertEqual(len(set(values)), len(values))

    def test_whitespace_signature_alone_cannot_authorise_buffer_discovery(self):
        value = modui.BUILD.marker('CHAT', 'english').encode()
        found = list(modui.marker_buffers(b'prefix' + value + b'\0'))
        self.assertEqual(found, [('CHAT', 6, value)])
        corrupt = b'X' + value[1:]
        self.assertEqual(list(modui.marker_buffers(corrupt)), [])

class ScrollDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name)
        modui.BUILD.build(output=cls.path)
        spec=importlib.util.spec_from_file_location('native_preview', ROOT/'tools/preview_gui.py')
        cls.preview=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.preview)
        nodes=cls.preview.get(cls.preview.parse((cls.path/'interface/le_chonk.gui').read_text()),'guiTypes')
        cls.window=next(v for _,_,v in nodes if cls.preview.get(v,'name')=='lc_panel_window')
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def test_readers_use_vanilla_text_scroll_and_receive_mouse_events(self):
        for name in ('lc_pl_text', 'lc_chat_text', 'lc_feed_text', 'lc_directives_text'):
            node = next((v for _, _, v in self.window if isinstance(v, list) and self.preview.get(v, 'name') == name), [])
            self.assertEqual(self.preview.get(node, 'alwaysTransparent'), 'yes', name)
            self.assertEqual(self.preview.get(node, 'fixedsize'), 'yes', name)

    def test_numeric_values_are_centred(self):
        values=[v for _,_,v in self.window if isinstance(v,list) and str(self.preview.get(v,'name','')).endswith('_value')]
        self.assertTrue(values)
        for node in values:self.assertEqual(self.preview.get(node,'format'),'centre')

    def test_open_button_is_icon_only_and_round_sized(self):
        nodes=self.preview.get(self.preview.parse((self.path/'interface/le_chonk.gui').read_text()),'guiTypes')
        top=next(v for _,_,v in nodes if self.preview.get(v,'name')=='lc_topbar_window')
        button=next(v for k,_,v in top if k=='buttonType')
        self.assertEqual(self.preview.get(button,'quadTextureSprite'),'GFX_lc_open')
        self.assertEqual(self.preview.get(button,'buttonText'),'')

    def test_part_controls_are_hidden_for_one_part_and_next_stops_at_end(self):
        scripted=self.preview.get(self.preview.parse((self.path/'common/scripted_guis/le_chonk_scripted_gui.txt').read_text()),'scripted_gui')
        triggers=self.preview.get(self.preview.get(scripted,'lc_panel'),'triggers')
        visible=self.preview.get(triggers,'lc_plan_next_visible')
        self.assertFalse(self.preview.condition(visible,{'lc_tab_plan'},{'lc_plan_last':0}))
        self.assertTrue(self.preview.condition(visible,{'lc_tab_plan'},{'lc_plan_last':2}))
        enabled=self.preview.get(triggers,'lc_plan_next_click_enabled')
        self.assertFalse(self.preview.condition(enabled,set(),{'lc_plan_page':2,'lc_plan_last':2}))
        self.assertTrue(self.preview.condition(enabled,set(),{'lc_plan_page':1,'lc_plan_last':2}))

class ReaderContentTests(unittest.TestCase):
    def test_same_saved_plan_relabels_after_language_change(self):
        from harness import run
        from types import SimpleNamespace
        from unittest.mock import patch
        runner=run.Runner.__new__(run.Runner)
        runner.planner=SimpleNamespace(plan={'idea':'Example strategy','army':'Example army'})
        runner.ui=SimpleNamespace(plan_page=0,reader_bounds=lambda *args:None)
        with patch('harness.chat.game_language',return_value='en'):
            self.assertIn('Strategy',runner.plan_texts()['PL_TEXT'])
        with patch('harness.chat.game_language',return_value='ru'):
            self.assertIn('Замысел',runner.plan_texts()['PL_TEXT'])

    def test_large_plan_has_every_word_after_actual_native_encoding(self):
        from harness import run
        from types import SimpleNamespace
        from unittest.mock import patch
        import re
        words=['подготовка%d' % n for n in range(4000)]
        runner=run.Runner.__new__(run.Runner)
        runner.planner=SimpleNamespace(plan={'idea':'UNIQUE_IDEA','army':' '.join(words)})
        runner.ui=SimpleNamespace(plan_page=0,reader_bounds=lambda *args:None,set_page=lambda *args:None)
        with patch('harness.chat.game_language',return_value='ru'), patch.object(modui,'_lang',return_value='ru'):
            first=runner.plan_texts()
            self.assertIn('UNIQUE_IDEA',first['PL_TEXT'])
            self.assertGreater(len(runner._plan_reader),1)
            shown=[]
            for page in range(len(runner._plan_reader)):
                runner.ui.plan_page=page
                text=runner.plan_texts()['PL_TEXT']
                self.assertEqual(re.sub(r'(?:\x11W)+$', '', modui._fit_lines(text,modui.BUILD.MARKERS['PL_TEXT']).decode()).rstrip(),text)
                shown.extend(re.findall(r'подготовка\d+',text))
            self.assertEqual(shown,words)

class ReaderBoundsTests(unittest.TestCase):
    def test_bounds_are_synced_once_and_never_write_while_disconnected(self):
        from unittest.mock import patch
        ui=modui.ModUI(None);ui.tag='SOV'
        with patch.object(ui,'_con',return_value=(True,'')) as command:
            ui.reader_bounds('plan',3)
            command.assert_not_called()
            ui.present=True
            ui.reader_bounds('plan',3)
            ui.reader_bounds('plan',3)
            command.assert_called_once_with('set_var SOV.lc_plan_last 2')
            ui.reader_bounds('plan',1)
            self.assertEqual(command.call_args[0][0],'set_var SOV.lc_plan_last 0')

    def test_cached_bounds_are_periodically_restored_after_same_pid_save_reload(self):
        from unittest.mock import patch
        ui=modui.ModUI(None);ui.tag='SOV';ui.present=True
        with patch.object(ui,'_con',return_value=(True,'')) as command, patch.object(modui.time,'monotonic') as clock:
            clock.return_value=10
            ui.reader_bounds('plan',3)
            clock.return_value=15
            ui.reader_bounds('plan',3)
            self.assertEqual(command.call_count,1)
            clock.return_value=41
            ui.reader_bounds('plan',3)
            self.assertEqual(command.call_count,2)
