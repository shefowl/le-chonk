"""Native GUI feedback; no game or model calls."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from harness import modui,chat

class NativePaddingTests(unittest.TestCase):
    def test_parser_normalized_localisation_retains_a_unique_zero_width_signature(self):
        for lang in ('english','russian'):
            values=[]
            for key,width in modui.BUILD.MARKERS.items():
                native=modui.BUILD.marker(key,lang)
                source=modui.BUILD.marker_source(key,lang)
                loaded=source.replace('§','\x11').replace('\t',' ')
                self.assertEqual(loaded,native)
                self.assertEqual(len(native.encode()),width)
                self.assertNotIn('  ',native)
                self.assertEqual(list(modui.marker_buffers(native.encode())),[(key,0,native.encode())])
                values.append(native)
            self.assertEqual(len(values),len(set(values)))

    def test_runtime_padding_does_not_create_blank_lines_or_truncation_ellipsis(self):
        with patch.object(modui,'_lang',return_value='en'):
            for fn in (modui._fit,modui._fit_lines):
                raw=fn('Ready.',12000)
                self.assertEqual(len(raw),12000)
                visible=re.sub(r'\x11.','',raw.decode()).strip()
                self.assertEqual(visible,'Ready.')
                self.assertFalse(b'  ' in raw, 'padding consists of visible-width spaces')

class ChatAdmissionTests(unittest.TestCase):
    def test_second_request_cannot_overwrite_busy_state_or_duplicate_user_text(self):
        with tempfile.TemporaryDirectory() as directory:
            store=chat.Chat(str(Path(directory)/'chat.json'))
            self.assertTrue(store.begin('army','answering','First','1936.01.01'))
            self.assertFalse(store.begin('army','writing a report','Second','1936.01.01'))
            self.assertEqual(store.busy['army'][0],'answering')
            self.assertEqual([row[1] for row in store.log('army')],['First'])

class ConnectionFeedbackTests(unittest.TestCase):
    def test_disconnected_request_does_not_claim_delivery_or_start_a_model(self):
        ui=modui.ModUI(None);ui.agent='army'
        with patch.object(ui,'_flag',side_effect=lambda name:name=='lc_send'),patch.object(ui,'_effect'), \
             patch.object(ui,'_var',return_value=None),patch.object(ui,'read_input',return_value='Hello'), \
             patch.object(ui,'write') as write,patch.object(modui,'_lang',return_value='en'):
            ui.on_message=called=lambda *args: self.fail('offline request started a model')
            ui.requests()
        self.assertIn('offline',write.call_args[0][0]['SENT'].lower())

    def test_activity_tracks_selected_agent_and_disconnect_clears_it(self):
        ui=modui.ModUI(None);ui.present=True;ui.agent='army';ui.activity_fn=lambda:{'army':('answering',0)}
        with patch.object(ui,'_effect',return_value=(True,'')), patch.object(ui,'_con',return_value=(True,'')) as con, \
             patch.object(ui,'write'):
            ui.connect();ui.sync_activity()
            self.assertTrue(ui.online)
            self.assertIn('lc_busy_army 1', ' '.join(str(c) for c in con.call_args_list))
            ui.disconnect()
            self.assertFalse(ui.online)
            self.assertTrue(ui.closed)
            ui.connect()
            self.assertFalse(ui.online)

class DefinitionFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        cls.temp=tempfile.TemporaryDirectory();cls.path=Path(cls.temp.name)
        modui.BUILD.build(output=cls.path)
        spec=importlib.util.spec_from_file_location('feedback_preview',Path(__file__).resolve().parents[1]/'tools/preview_gui.py')
        cls.p=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.p)
        script=cls.p.get(cls.p.parse((cls.path/'common/scripted_guis/le_chonk_scripted_gui.txt').read_text()),'scripted_gui')
        cls.panel=cls.p.get(script,'lc_panel');cls.triggers=cls.p.get(cls.panel,'triggers')
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def check(self, name, flags, variables):
        trigger=self.p.get(self.triggers,name)
        self.assertIsNotNone(trigger,name)
        return self.p.condition(trigger,flags,variables)
    def test_offline_or_busy_cannot_send_or_ask_for_report(self):
        for control in ('lc_send','lc_report'):
            self.assertFalse(self.check(control+'_click_enabled',set(),{'lc_agent':1}))
            self.assertTrue(self.check(control+'_click_enabled',{'lc_online'},{'lc_agent':1,'lc_busy_army':1}))
            self.assertFalse(self.check(control+'_click_enabled',{'lc_online'},{'lc_agent':1,'lc_busy_supreme':1}))
    def test_offline_pending_flag_never_claims_agent_is_thinking(self):
        flags={'lc_tab_agent','lc_report'}
        self.assertFalse(self.check('lc_view_reporting_visible',flags,{'lc_agent':1}))
        self.assertFalse(self.check('lc_request_spinner_visible',flags,{'lc_agent':1}))
        self.assertTrue(self.check('lc_offline_warning_visible',flags,{'lc_agent':1}))
        self.assertFalse(self.check('lc_ag_st_visible',flags,{'lc_agent':1}))
        self.assertTrue(self.check('lc_offline_agent_visible',flags,{'lc_agent':1}))
    def test_busy_spinner_remains_after_click_flag_is_consumed(self):
        flags={'lc_tab_agent','lc_online'};variables={'lc_agent':1,'lc_busy_supreme':2}
        self.assertTrue(self.check('lc_request_spinner_visible',flags,variables))
        self.assertTrue(self.check('lc_view_reporting_visible',flags,variables))
        self.assertFalse(self.check('lc_view_title_chat_visible',flags,variables))
    def test_refresh_hides_stale_agent_body_until_paint_is_acknowledged(self):
        flags={'lc_tab_agent','lc_online','lc_view_pending'}
        self.assertFalse(self.check('lc_chat_text_visible',flags,{'lc_agent':1}))
        self.assertTrue(self.check('lc_refresh_spinner_visible',flags,{'lc_agent':1}))

    def test_pending_request_slot_cannot_be_overwritten_from_another_agent(self):
        flags={'lc_online','lc_tab_agent','lc_report'}
        variables={'lc_agent':5,'lc_request_agent':1}
        self.assertFalse(self.check('lc_report_click_enabled',flags,variables))
        self.assertFalse(self.check('lc_request_spinner_visible',flags,variables))

    def test_settings_have_real_bounds_and_require_connection(self):
        name='lc_s_speed_peace_inc_click_enabled'
        self.assertFalse(self.check(name,set(),{'lc_s_speed_peace':3}))
        self.assertFalse(self.check(name,{'lc_online'},{'lc_s_speed_peace':5}))
        self.assertTrue(self.check(name,{'lc_online'},{'lc_s_speed_peace':4}))

class CompletionTests(unittest.TestCase):
    def test_call_result_is_returned_without_reopening_admission_before_ui_feedback(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            store=chat.Chat(str(Path(directory)/'chat.json'))
            store.begin('army','writing a report')
            agent=SimpleNamespace(name='Army',system='Army role')
            with patch.object(store,'_call',side_effect=TimeoutError):
                error=store.report('army',agent,'',[],'1936.01.01','en')
            self.assertEqual(error,'TimeoutError')
            self.assertIn('army',store.busy)
            self.assertFalse(store.begin('army','answering','too early'))
            store.end('army')
            self.assertTrue(store.begin('army','answering','retry'))

    def test_preparation_failure_clears_busy_and_is_reported(self):
        from harness import run
        from types import SimpleNamespace
        import threading
        with tempfile.TemporaryDirectory() as directory:
            r=run.Runner.__new__(run.Runner);r.last_date='1936.01.01'
            r.chat=chat.Chat(str(Path(directory)/'chat.json'));r.chat.begin('army','writing a report')
            seen=[]
            r.ui=SimpleNamespace(lock=threading.RLock(),finish_request=lambda *args:seen.append(args))
            def bad():raise ValueError('could not build briefing')
            r._reply_worker('army','writing a report',bad)
            self.assertNotIn('army',r.chat.busy)
            self.assertEqual(seen,[('army','writing a report','ValueError')])

class ReconciliationTests(unittest.TestCase):
    def test_dirty_rollback_reconnects_and_restores_busy_variables(self):
        import time
        ui=modui.ModUI(None);ui.present=True;ui.online=True;ui.tag='SOV';ui.dirty=10
        ui.activity_sync_at=time.monotonic();ui.activity_cache={'army':1}
        ui.activity_fn=lambda:{'army':('answering',0)};ui.shown={'CHAT':'old'}
        with patch.object(ui,'_var',return_value=5),patch.object(ui,'_effect',return_value=(True,'')) as fx, \
             patch.object(ui,'_con',return_value=(True,'')) as con,patch.object(ui,'pull'):
            ui.poll()
        self.assertIn(('lc_ui_connect',),[call.args for call in fx.call_args_list])
        self.assertTrue(ui.online)
        self.assertEqual(ui.activity_cache.get('army'),1)
        self.assertEqual(ui.shown,{})
        self.assertTrue(any('lc_busy_army 1' in call.args[0] for call in con.call_args_list))

    def test_periodic_reconciliation_clears_stale_saved_busy_value(self):
        ui=modui.ModUI(None);ui.present=True;ui.online=True;ui.tag='SOV'
        ui.activity_sync_at=1;ui.activity_cache={'army':0};ui.activity_fn=lambda:{}
        with patch.object(modui.time,'monotonic',return_value=32), \
             patch.object(ui,'_effect',return_value=(True,'')) as fx,patch.object(ui,'_con',return_value=(True,'')) as con:
            ui.sync_activity()
        fx.assert_called_once_with('lc_ui_keepalive')
        self.assertTrue(any('lc_busy_army 0' in call.args[0] for call in con.call_args_list))

    def test_ui_disconnect_failure_never_escapes_shutdown(self):
        ui=modui.ModUI(None,log=lambda text:None);ui.online=True
        with patch.object(ui,'_effect',side_effect=RuntimeError('game closed')):
            ui.disconnect()
        self.assertFalse(ui.online)
        self.assertTrue(ui.closed)

class EmptyReplyTests(unittest.TestCase):
    def test_empty_report_is_a_failure_instead_of_success_feedback(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            store=chat.Chat(str(Path(directory)/'chat.json'));store.begin('army','writing a report')
            with patch.object(store,'_call',return_value={'report':''}):
                error=store.report('army',SimpleNamespace(name='Army',system='role'),'',[],'1936.01.01','en')
            self.assertEqual(error,'EmptyReplyError')

class PaintAcknowledgementTests(unittest.TestCase):
    def test_failed_ack_is_retried_even_if_variable_write_succeeded(self):
        ui=modui.ModUI(None);ui.online=True;ui.needs_paint_ack=True;ui.tag='SOV';ui.dirty=3
        with patch.object(ui,'_write',side_effect=lambda texts:ui.shown.update(texts)), \
             patch.object(ui,'_con',return_value=(True,'')),patch.object(ui,'_effect',side_effect=[(False,'timeout'),(True,'')]):
            ui.write({'HEAD':'Connected'})
            self.assertTrue(ui.needs_paint_ack)
            ui.write({'HEAD':'Connected'})
            self.assertFalse(ui.needs_paint_ack)
