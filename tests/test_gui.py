import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load_build():
    spec = importlib.util.spec_from_file_location('gui_build', ROOT / 'mod/build.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

class GuiTextTests(unittest.TestCase):
    def test_pages_honour_explicit_lines(self):
        from harness.run import pages_of
        pages = pages_of(['one\ntwo\nthree', 'four'], 80, 3)
        self.assertEqual(pages, [['one\ntwo\nthree'], ['four']])

    def test_long_report_is_split_without_losing_words(self):
        from harness.run import pages_of
        text = ' '.join('word%d' % i for i in range(80))
        pages = pages_of([text], 35, 4)
        self.assertGreater(len(pages), 1)
        self.assertEqual(' '.join(' '.join(p).replace('\n', ' ') for p in pages), text)
        self.assertTrue(all(sum(len(r.splitlines()) for r in p) <= 4 for p in pages))

    def test_preview_build_does_not_change_installed_mod(self):
        b = load_build()
        original = ROOT / 'mod/le_chonk/interface/le_chonk.gui'
        before = original.read_bytes()
        with tempfile.TemporaryDirectory() as d:
            b.build(output=d)
            self.assertTrue((Path(d) / 'interface/le_chonk.gui').is_file())
            self.assertEqual(original.read_bytes(), before)

    def test_settings_have_short_localisation_fallbacks(self):
        b = load_build()
        with tempfile.TemporaryDirectory() as d:
            b.build(output=d)
            s = (Path(d) / 'localisation/english/le_chonk_l_english.yml').read_text('utf-8-sig')
            for key, *_ in b.ROSTER.SETTINGS:
                line = next(l for l in s.splitlines() if l.startswith(' LC_SV_' + key.upper() + ':'))
                self.assertNotIn('LCM_', line)
                self.assertIn('[?lc_s_' + key + '|0]', line)

class GuiRoutingAndWrappingTests(unittest.TestCase):
    def test_direct_contact_is_routed_as_agent_window_change(self):
        b=load_build()
        self.assertEqual(b.what_of('lc_open_agent_industry'), b.WHAT['window'])

    def test_colour_codes_are_not_split_at_line_boundary(self):
        from harness.run import pages_of
        pages=pages_of(['x'*65+'\x11R'+'z'*120+'\x11!'],66,18)
        text='\n'.join('\n'.join(p) for p in pages)
        import re
        self.assertIsNone(re.search('\x11(?:$|\n)',text))
        self.assertEqual(re.sub('\x11.','',text).replace('\n',''),'x'*65+'z'*120)

    def test_colour_is_restored_on_each_page(self):
        from harness.run import pages_of
        pages=pages_of(['\x11Rabcdefghijk\x11!'],5,1)
        self.assertEqual(pages,[['\x11Rabcde\x11!'],['\x11Rfghij\x11!'],['\x11Rk\x11!']])

    def test_wide_glyphs_fit_actual_report_page(self):
        from harness.run import pages_of
        from harness.ui_text import pixel_width
        text=('мышцы WWW ')*200
        pages=pages_of([text],572,18,measure=pixel_width)
        for page in pages:
            lines='\n'.join(page).splitlines()
            self.assertLessEqual(len(lines),18)
            self.assertTrue(all(pixel_width(line)<=572 for line in lines))
        self.assertEqual(' '.join(' '.join(p).replace('\n',' ') for p in pages).split(),text.split())

class FontLookupTests(unittest.TestCase):
    def test_font_discovery_is_not_repeated_for_each_character(self):
        from harness import ui_text
        import os
        if hasattr(ui_text, '_metrics'):ui_text._metrics.cache_clear()
        with patch.dict(os.environ, {}, clear=True), \
                patch('harness.bridge.game_dir', return_value='/tmp/no-game-fonts') as directory:
            for letter in 'abcde' * 20:
                ui_text.pixel_width(letter, 'test-font-cache')
            self.assertEqual(directory.call_count, 1)
        if hasattr(ui_text, '_metrics'):ui_text._metrics.cache_clear()

if __name__ == '__main__': unittest.main()
