"""Offline scheduling and provider regressions; no live game or paid requests."""
import asyncio
import collections
import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from harness import claude_sub, llm, run


class ClaudeLimitsTests(unittest.TestCase):
    def test_timeout_cancels_request_and_returns_budget_error(self):
        cancelled = []
        result = types.SimpleNamespace(is_error=False, structured_output={'thought':'late','calls':[]}, usage={}, total_cost_usd=0)
        async def slow(*args, **kwargs):
            try:
                await asyncio.sleep(.15)
                return result, {}
            finally:
                cancelled.append(True)
        with patch.object(claude_sub, '_ask', slow):
            with self.assertRaises(llm.LLMOverBudget):
                claude_sub.chat('test', [], [], 'auto', 'test', 'none', .01, max_tokens=1000)
        self.assertEqual(cancelled, [True])

    def test_no_budget_keeps_deep_session_unlimited(self):
        result = types.SimpleNamespace(is_error=False, structured_output={'thought':'ok','calls':[]}, usage={}, total_cost_usd=0)
        async def answer(*args, **kwargs):
            await asyncio.sleep(.01)
            return result, {}
        with patch.object(claude_sub, '_ask', answer):
            msg, *_ = claude_sub.chat('test', [], [], 'auto', 'test', 'high', None, max_tokens=128000)
        self.assertEqual(msg['content'], 'ok')

    def test_sdk_receives_output_cap_and_adjusted_thinking(self):
        import claude_agent_sdk
        seen=[]
        async def query(*, prompt, options):
            seen.append(options)
            if False: yield None
        with patch.object(claude_agent_sdk, 'query', query), tempfile.TemporaryDirectory() as folder, patch.object(claude_sub, 'CWD', folder):
            asyncio.run(claude_sub._ask('test', 'sys', 'hi', [], {'type':'enabled','budget_tokens':3072}, 'low', max_tokens=2048))
        self.assertEqual(seen[0].env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], '2048')
        self.assertEqual(seen[0].env['MAX_STRUCTURED_OUTPUT_RETRIES'], '1')
        self.assertLess(seen[0].thinking['budget_tokens'], 2048)

    def test_structured_output_failure_is_retryable_but_not_other_sdk_errors(self):
        from claude_agent_sdk import ResultError
        async def bad(*args,**kw):
            raise ResultError('Failed to provide valid structured output after 1 attempts')
        with patch.object(claude_sub,'_ask',bad):
            with self.assertRaises(llm.LLMOutputError):
                claude_sub.chat('test',[],[],'auto','test','none',1)
        async def denied(*args,**kw):raise ResultError('Account authentication failed')
        with patch.object(claude_sub,'_ask',denied):
            with self.assertRaises(ResultError):claude_sub.chat('test',[],[],'auto','test','none',1)

    def test_agent_retries_invalid_output_once_without_thinking(self):
        from harness.agents import Agent
        calls=[]
        def answer(*args,**kw):
            calls.append(kw)
            if len(calls)==1:raise llm.LLMOutputError('invalid structured output')
            return {'role':'assistant','tool_calls':[{'id':'1','function':{'name':'end_turn','arguments':'{"summary":"Recovered structured reply with a complete turn summary."}'}}]}, {}, .01
        class Staff(Agent):
            reasoning=3072
            def tools(self):return []
        with tempfile.TemporaryDirectory() as folder:
            a=Staff('test','test',types.SimpleNamespace(chat=answer),run.Journal(str(Path(folder)/'j.jsonl')),'test')
            summary=a.turn({'date':'now'},'briefing')
        self.assertIn('Recovered',summary)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[-1]['reasoning'],'none')

    def test_llm_passes_output_limit_and_logs_wait(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(llm, 'load_env'):
            client=llm.LLM('claude', str(Path(folder)/'calls.jsonl'))
            def answer(*args, **kwargs):
                self.assertEqual(kwargs['max_tokens'], 8192)
                return {'role':'assistant','content':'ok'}, {}, .01, {'latency_s':.01}
            with patch.object(claude_sub, 'chat', answer):
                client.chat('test', [], max_tokens=8192)
            rec=json.loads(Path(client.log_path).read_text())
            self.assertGreaterEqual(rec['queue_s'], 0)

    def test_timeout_is_logged_with_unknown_token_usage(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(llm, 'load_env'):
            client=llm.LLM('claude', str(Path(folder)/'calls.jsonl'))
            with patch.object(claude_sub, 'chat', side_effect=llm.LLMOverBudget('slow')):
                with self.assertRaises(llm.LLMOverBudget):client.chat('test', [], role='test', budget_s=.01)
            row=json.loads(Path(client.log_path).read_text())
            self.assertEqual(row['event'], 'timeout')
            self.assertIsNone(row['completion_tokens'])


class SchedulingTests(unittest.TestCase):
    def runner(self, pause='fires'):
        r=run.Runner.__new__(run.Runner)
        r.args=types.SimpleNamespace(pause=pause)
        r.peace=lambda:False
        r.land_war=lambda:True
        return r

    def test_startup_waits_for_staff_before_clock_runs(self):
        r = self.runner()
        self.assertTrue(callable(getattr(r, 'start_staff_round', None)),
                        'startup must wait for the staff round')
        r._hold_lock = threading.Lock(); r._holds = 0; r.stopping = False
        r.gw = types.SimpleNamespace(urgent=lambda *a: None)
        r.journal = types.SimpleNamespace(write=lambda **kw: None)
        r.threads = []
        entered = threading.Event(); finish = threading.Event(); clock = []
        r.run_clock = lambda: clock.append('running')
        def wake():
            def work():
                entered.set()
                finish.wait(2)
            t = threading.Thread(target=work)
            r.threads.append(t); t.start()
        r.wake_executors = wake
        t = threading.Thread(target=r.start_staff_round, args=(True,))
        t.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(clock, [])
        finish.set(); t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(clock, ['running'])

    def test_failed_startup_staff_keeps_clock_paused(self):
        r = self.runner()
        r._hold_lock = threading.Lock(); r._holds = 0; r.stopping = False
        r.gw = types.SimpleNamespace(urgent=lambda *a: None)
        r.journal = types.SimpleNamespace(write=lambda **kw: None)
        r.threads = []
        clock = []
        r.run_clock = lambda: clock.append('running')
        r.wake_executors = lambda: getattr(r, '_startup_failures', []).append('Air staff')
        with self.assertRaises(RuntimeError):
            r.start_staff_round(True)
        self.assertEqual(clock, [])

    def test_resume_retries_a_staff_role_that_never_finished_its_first_turn(self):
        r = self.runner()
        r.hold = lambda: None; r.release = lambda: None
        r.threads = []
        for name in ('industry', 'politics', 'army', 'air_staff', 'admiralty'):
            setattr(r, name, types.SimpleNamespace(name=name, memory=[] if name=='politics' else ['done']))
        started=[]
        r.spawn=lambda agent, briefing, why:started.append(agent)
        r.start_staff_round(False)
        self.assertIn(r.politics, started)
        self.assertNotIn(r.army, started)

    def test_routine_staff_runs_during_land_war_but_tactics_pause(self):
        r=self.runner()
        with patch.object(run.roster, 'get', return_value={}):
            for key in ('industry','politics','air','army','admiralty'):
                self.assertFalse(r.holds_for(key), key)
            for key in ('supreme','marshals','events'):
                self.assertTrue(r.holds_for(key), key)

    def test_fast_peace_waits_for_staff_unless_pause_is_disabled(self):
        with patch.object(run.roster, 'get', return_value={}):
            r = self.runner(); r.peace = lambda: True
            self.assertTrue(r.holds_for('industry'))
            r.args.pause = 'never'
            self.assertFalse(r.holds_for('industry'))

    def test_explicit_pause_override_and_always_are_respected(self):
        with patch.object(run.roster, 'get', return_value={'pause_industry':'always'}):
            self.assertTrue(self.runner().holds_for('industry'))
        with patch.object(run.roster, 'get', return_value={}):
            self.assertTrue(self.runner('always').holds_for('industry'))
            self.assertFalse(self.runner('never').holds_for('marshals'))

    def test_navy_commanders_overlap_after_admiralty_and_lock_waits(self):
        r=self.runner()
        r.navy_lock=threading.Lock(); r.threads=[]; r.admiralty=object()
        r.holds_for=lambda key:False
        r.commanders={'a':None,'b':None}
        r.sync_commanders=lambda:None
        entered=threading.Barrier(2); failures=[]; finished=[]; admiral_done=[]
        r._turn=lambda *args:admiral_done.append(True)
        def turn(name, why):
            if not admiral_done:failures.append('before admiralty')
            try:entered.wait(timeout=.3)
            except threading.BrokenBarrierError:failures.append('serial')
            time.sleep(.01)
            finished.append(name)
        r._commander_turn=turn
        with patch.object(run.roster, 'get', return_value={}):
            r.navy_round('review')
            r.threads[0].join(timeout=1)
        self.assertEqual(failures, [])
        self.assertCountEqual(finished, ['a','b'])
        self.assertFalse(r.navy_lock.locked())

    def test_pending_letters_merge_without_delaying_emergencies_or_player(self):
        q=collections.deque([('supreme','request from A'),('supreme','request from A'),('supreme','request from B'),('supreme','NEW FIRE'),('supreme','message from the player')])
        rows=run.coalesce_alarms(q)
        self.assertEqual(len(rows),3)
        self.assertIn(('supreme','NEW FIRE'), rows)
        self.assertIn(('supreme','message from the player'), rows)
        self.assertEqual(rows[0], ('supreme','request from A; request from B'))
        self.assertFalse(q)

    def test_clock_samples_measure_progress_even_when_no_agents_wake(self):
        r=self.runner();r._holds=0
        with tempfile.TemporaryDirectory() as folder:
            r.journal=run.Journal(str(Path(folder)/'j.jsonl'))
            with patch.object(run.time,'monotonic',side_effect=[0,29,30]):
                r.sample_clock('1940.01.01.00')
                r.sample_clock('1940.01.02.00')
                r.sample_clock('1940.01.03.00')
            rows=[json.loads(l) for l in Path(r.journal.path).read_text().splitlines()]
        self.assertEqual([r['date'] for r in rows],['1940.01.01.00','1940.01.03.00'])

    def test_nested_holds_measure_only_outer_pause(self):
        r=self.runner();r._hold_lock=threading.Lock();r._holds=0;r.stopping=False
        r.gw=types.SimpleNamespace(urgent=lambda *a:None)
        r.run_clock=lambda:None
        with tempfile.TemporaryDirectory() as folder:
            r.journal=run.Journal(str(Path(folder)/'journal.jsonl'))
            with patch.object(run.time,'monotonic',side_effect=[10,13]):
                r.hold();r.hold();r.release();r.release()
            rows=[json.loads(l) for l in Path(r.journal.path).read_text().splitlines()]
        self.assertEqual([x['event'] for x in rows], ['clock_hold','clock_release'])
        self.assertEqual(rows[-1]['paused_s'],3)

class PlanningAndFreshnessTests(unittest.TestCase):
    def test_local_planning_restores_normal_profile_after_review(self):
        from harness.briefing import marshal_budget
        m=types.SimpleNamespace(reasoning='none',max_tokens=8000,budget_s=90)
        marshal_budget(m, 'new war')
        self.assertTrue(m.planning)
        self.assertLessEqual(m.reasoning, 4096)
        self.assertLess(m.max_tokens, 24000)
        self.assertLessEqual(m.budget_s,60)
        marshal_budget(m, 'another review')
        marshal_budget(m, None)
        self.assertEqual((m.reasoning,m.max_tokens,m.budget_s), ('none',8000,90))
        self.assertFalse(m.planning)

    def test_stale_background_answer_is_replanned_before_any_order(self):
        from harness.agents import Agent
        replies=iter([
            {'role':'assistant','content':'old', 'tool_calls':[{'id':'1','function':{'name':'change','arguments':'{}'}}]},
            {'role':'assistant','content':'new', 'tool_calls':[{'id':'2','function':{'name':'change','arguments':'{}'}},{'id':'3','function':{'name':'end_turn','arguments':'{"summary":"A current decision based on the refreshed briefing."}'}}]},
        ])
        client=types.SimpleNamespace(chat=lambda *a,**kw:(next(replies),{},.01))
        class Staff(Agent):
            def tools(self):return []
            def handle(self,ctx,name,args):
                applied.append(ctx['date'])
                return True
        applied=[]; refreshes=iter(['Updated briefing at day 5.',None])
        ctx={'date':'old','before_orders':lambda:next(refreshes)}
        with tempfile.TemporaryDirectory() as folder:
            agent=Staff('test','test',client,run.Journal(str(Path(folder)/'j.jsonl')),'test')
            agent.max_steps=1
            result=agent.turn(ctx,'old briefing')
        self.assertEqual(applied,['old'])  # only the second reply is executed
        self.assertIn('refreshed briefing',result)

    def test_freshness_guard_pauses_only_when_old_and_releases_at_owner(self):
        r=run.Runner.__new__(run.Runner)
        calls=[];now=[1]
        r.gw=types.SimpleNamespace(run=lambda fn:({'day':now[0],'text':str(now[0])} if fn is run.ops.date else ['GER']))
        r.hold=lambda:calls.append('hold')
        r.ctx=lambda:{'day':now[0],'date':str(now[0]),'enemies':['GER']}
        r.planner=types.SimpleNamespace(plan_for=lambda name:'the plan')
        r._war_part=lambda *a:''
        r.journal=types.SimpleNamespace(write=lambda **kw:None)
        c={'day':1,'date':'1','enemies':['GER']}
        held=[]
        with patch.object(run,'day_of',side_effect=lambda d:d['day']):
            guard=r.freshness_guard(types.SimpleNamespace(name='staff'), c, lambda ctx:'fresh '+ctx['date'],held)
            self.assertIsNone(guard())
            now[0]=4
            self.assertIn('fresh 4',guard())
            self.assertIsNone(guard())
        self.assertEqual(c['day'],4)
        self.assertEqual(calls,['hold'])
        self.assertEqual(held,[True])

if __name__=='__main__':unittest.main()
