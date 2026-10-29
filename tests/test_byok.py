"""Offline credential and contest profile boundaries; no network or game access."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from harness import credentials


class CredentialTests(unittest.TestCase):
    def test_atomic_private_roundtrip_and_blank_key_retention(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'
            credentials.save({'provider':'mistral', 'model':'mistral-large-4', 'api_key':'secret'}, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            credentials.save({'provider':'mistral', 'model':'mistral-large-4', 'api_key':''}, path)
            self.assertEqual(credentials.load(path)['api_key'], 'secret')
            credentials.save({'provider':'custom', 'base_url':'https://example.com/v1', 'model':'other', 'api_key':''}, path)
            self.assertEqual(credentials.load(path)['api_key'], '')

    def test_rejects_unsafe_endpoints_and_corrupt_config(self):
        for url in ('http://example.com/v1', 'https://key@host/v1', 'https://host/v1?key=x', 'file:///tmp/x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                credentials.validate({'provider':'custom','base_url':url,'model':'test'})
        self.assertEqual(credentials.validate({'provider':'custom','base_url':'http://127.0.0.1:8000/v1/','model':'test'})['base_url'], 'http://127.0.0.1:8000/v1')
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'bad.json';p.write_text('{')
            with self.assertRaises(ValueError): credentials.load(p)

    def test_contest_cannot_route_to_custom_or_override_model(self):
        for provider, model in [('openrouter',None),('custom',None),('mistral','mistral-large-latest')]:
            a=SimpleNamespace(models='lechonk',provider=provider,supreme_model=model,theater_model=None)
            with self.assertRaises(ValueError): credentials.resolve_profile(a, {})
        a=SimpleNamespace(models='lechonk',provider=None,supreme_model=None,theater_model=None)
        credentials.resolve_profile(a, {'provider':'custom','base_url':'https://example.com/v1','model':'other','api_key':'wrong'})
        self.assertEqual((a.provider,a.supreme_model,a.theater_model), ('mistral','mistral-large-4','mistral-large-4'))

    def test_configured_profile_uses_selected_provider_model(self):
        a=SimpleNamespace(models='configured',provider=None,supreme_model=None,theater_model=None)
        credentials.resolve_profile(a, {'provider':'custom','model':'my-model'})
        self.assertEqual((a.provider,a.supreme_model,a.theater_model), ('custom','my-model','my-model'))

    def test_llm_never_sends_other_provider_key_and_redacts(self):
        from harness.llm import LLM
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'MISTRAL_API_KEY':'studio-secret'}, clear=True), patch('harness.llm.load_env'), patch('harness.credentials.load',return_value={'provider':'custom','base_url':'https://other.test/v1','model':'x','api_key':'custom-secret'}):
            llm=LLM('mistral', log_path=str(Path(folder)/'calls.jsonl'))
            self.assertEqual(llm.base,'https://api.mistral.ai/v1')
            self.assertEqual(llm.key,'studio-secret')
            llm._log({'error':'studio-secret'})
            self.assertNotIn('studio-secret',Path(llm.log_path).read_text())

class SetupIntegrationTests(unittest.TestCase):
    def test_neutral_role_names_in_both_languages(self):
        from mod import build
        self.assertEqual(build.STAFF[1][2:], ('Supreme command','Ставка'))
        for key,_,en,ru in build.STAFF:
            self.assertNotIn('Le Chonk', en+ru)
        for copy in build.CARDS.values(): self.assertNotIn('Le Chonk',' '.join(copy))

    def test_native_setup_request_is_consumed_and_single_instance(self):
        from harness.modui import ModUI
        ui=ModUI(None, lambda m: None)
        ui._flag=lambda _:True
        effects=[];ui._effect=lambda name:effects.append(name)
        proc=SimpleNamespace(poll=lambda:None)
        with patch('subprocess.Popen',return_value=proc) as launch:
            ui._configuration_request();ui._configuration_request()
            launch.assert_called_once()
            self.assertTrue(launch.call_args.args[0][-1].endswith('/tools/configure_model.py'))
        self.assertEqual(effects,['lc_setup_consumed','lc_setup_consumed'])

class FinalRunTests(unittest.TestCase):
    def test_fresh_state_preserves_campaign_and_marker_cache(self):
        from tools.final_run import archive_state
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'logs').mkdir();(root/'config').mkdir()
            for name in ('campaign.json','chat.json','.lc_markers.json'):(root/'logs'/name).write_text(name)
            (root/'roster.json').write_text('old-roster')
            (root/'config/italy-roster.json').write_text('{"planner":"on"}')
            backup=archive_state(root)
            self.assertEqual((backup/'campaign.json').read_text(),'campaign.json')
            self.assertEqual((backup/'roster.json').read_text(),'old-roster')
            self.assertFalse((root/'logs/campaign.json').exists())
            self.assertEqual((root/'logs/.lc_markers.json').read_text(),'.lc_markers.json')

    def test_start_guard_checks_country_and_calibrated_day(self):
        from harness.credentials import verify_start
        args=SimpleNamespace(expect_country='ITA',expect_start_day='1936.01.01')
        verify_start(args,{'my_tag':'ITA','date':'1936.01.01.13'})
        for info in ({'my_tag':'SOV','date':'1936.01.01.13'}, {'my_tag':'ITA','date':'1941.06.01.00'}):
            with self.assertRaises(ValueError):verify_start(args,info)

class StorageRecoveryTests(unittest.TestCase):
    def test_explicit_save_can_repair_malformed_file_without_reusing_a_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'llm.json';path.write_text('{');path.chmod(0o600)
            credentials.save({'provider':'mistral','model':'mistral-large-4','api_key':'replacement'},path)
            self.assertEqual(credentials.load(path)['api_key'],'replacement')

    def test_endpoint_change_does_not_retain_key_and_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'llm.json'
            credentials.save({'provider':'custom','base_url':'https://one.test/v1','model':'x','api_key':'one'},path)
            credentials.save({'provider':'custom','base_url':'https://two.test/v1','model':'x','api_key':''},path)
            self.assertEqual(credentials.load(path)['api_key'],'')
            link=Path(folder)/'link.json';link.symlink_to(path)
            with self.assertRaises(ValueError):credentials.save({'provider':'mistral'},link)

class EndpointKeyIsolationTests(unittest.TestCase):
    def test_blank_custom_key_never_inherits_an_unrelated_local_key(self):
        from harness.llm import LLM
        cfg={'provider':'custom','base_url':'https://new.test/v1','model':'x','api_key':''}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'LOCAL_LLM_KEY':'old-local-secret','LOCAL_LLM_URL':'http://localhost:1234/v1'},clear=True), patch('harness.llm.load_env'), patch('harness.credentials.load',return_value=cfg):
            client=LLM('custom',log_path=str(Path(folder)/'calls.jsonl'))
            self.assertEqual(client.key,'')

class OwnershipTests(unittest.TestCase):
    def test_second_process_cannot_acquire_writer_lock(self):
        import subprocess,sys
        from harness import ownership
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'LECHONK_LOCK_FILE':str(Path(folder)/'writer.lock')},clear=True):
            fd=ownership.acquire()
            self.addCleanup(os.close,fd)
            env=dict(os.environ);env.pop('LECHONK_LOCK_FD',None)
            script='from harness.ownership import acquire; acquire()'
            result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('already owns',result.stderr)
            self.assertEqual(ownership.acquire(),fd)

    def test_usage_window_uses_completion_timestamp_without_double_counting_latency(self):
        from tools.run_report import aggregate
        row={'t':150,'latency_s':50,'provider':'mistral','model':'mistral-large-4','prompt_tokens':12,'completion_tokens':3}
        result=aggregate([row],100)
        self.assertEqual(result['observed_api_window_s'],50)

class ProviderErrorTests(unittest.TestCase):
    def test_provider_validation_error_is_useful_without_echoing_secret(self):
        from harness.llm import LLM
        client=object.__new__(LLM);client.key='private-fake-key'
        response=SimpleNamespace(json=lambda:{'message':'invalid parameter: private-fake-key'})
        self.assertEqual(client._error_message(response),'invalid parameter: [REDACTED]')

class StudioReasoningTests(unittest.TestCase):
    def test_contest_none_maps_to_lowest_supported_studio_effort(self):
        from harness.llm import LLM
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'MISTRAL_API_KEY':'test-key'},clear=True), patch('harness.llm.load_env'), patch('harness.credentials.load',return_value={}):
            client=LLM('mistral',log_path=str(Path(folder)/'calls.jsonl'))
            response=Mock(status_code=200);client.session.post=Mock(return_value=response)
            client._read_stream=Mock(return_value=({'role':'assistant','content':'ok'},{},'stop'))
            client.chat('mistral-large-4',[],reasoning='none')
            body=json.loads(client.session.post.call_args.kwargs['data'])
            self.assertEqual(body['reasoning_effort'],'low')
            self.assertEqual(client.session.post.call_args.args[0],'https://api.mistral.ai/v1/chat/completions')
            self.assertFalse(client.session.post.call_args.kwargs['allow_redirects'])

class SearchFallbackTests(unittest.TestCase):
    def test_external_search_parser_retains_real_urls_and_snippets(self):
        from harness.websearch import parse_results
        page={'results':[{'title':'Italy guide','url':'https://hoi4.paradoxwikis.com/Italy','content':'A wiki description.'}]}
        rows=parse_results(page)
        self.assertEqual(rows,[{'title':'Italy guide','url':'https://hoi4.paradoxwikis.com/Italy','snippet':'A wiki description.'}])

    def test_large4_search_uses_same_model_with_results_and_no_connector(self):
        from harness.planner import Planner
        from unittest.mock import Mock
        client=SimpleNamespace(provider='mistral',chat=Mock(return_value=({'content':'answer'}, {}, .1)))
        planner=object.__new__(Planner);planner.llm=client;planner.model='mistral-large-4';planner.name='Planner'
        rows=[{'title':'Italy','url':'https://hoi4.paradoxwikis.com/Italy','snippet':'facts'}]
        with patch('harness.websearch.search',return_value=rows):result=planner.web_search('Italy')
        self.assertEqual(result['sources'],[rows[0]['url']])
        self.assertEqual(client.chat.call_args.args[0],'mistral-large-4')
        self.assertNotIn('plugins',client.chat.call_args.kwargs)

class SearchTransportTests(unittest.TestCase):
    def test_missing_search_endpoint_is_explicit_and_does_not_call_network(self):
        from harness import websearch
        from harness.ops import OpError
        with patch.dict(os.environ,{},clear=True),patch('harness.websearch.requests.get') as get:
            with self.assertRaisesRegex(OpError,'not configured'):websearch.search('Italy')
            get.assert_not_called()

    def test_search_request_has_no_model_key_and_no_redirects(self):
        from harness import websearch
        from unittest.mock import Mock
        response=Mock(status_code=200)
        response.iter_content.return_value=[b'{"results":[{"title":"Italy","url":"https://hoi4.paradoxwikis.com/Italy","content":"Facts"}]}']
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch.dict(os.environ,{'LECHONK_SEARCH_URL':'http://127.0.0.1:8080/search','MISTRAL_API_KEY':'never-forwarded'},clear=True),patch('harness.websearch.requests.get',return_value=response) as get:
            self.assertEqual(len(websearch.search('Italy')),1)
            self.assertNotIn('headers',get.call_args.kwargs)
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
