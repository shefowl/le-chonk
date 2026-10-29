"""Targeted country decisions must not use the untargeted command/exile path."""
import types
import unittest
from unittest.mock import patch
from harness import agents, politics


class DecisionTests(unittest.TestCase):
    def test_law_post_success_without_effect_is_not_reported_as_adoption(self):
        g=types.SimpleNamespace(_settle=lambda delay:None,politics=lambda:{'active_ideas':['civilian_economy']})
        cab=types.SimpleNamespace(adopt_law=lambda g,law:{'ok':True})
        with patch.object(politics.B,'module',return_value=cab):
            with self.assertRaisesRegex(Exception,'not active on readback'):
                politics.adopt_law(g,'war_economy')

    def test_country_targets_are_read_from_the_decision_not_hardcoded(self):
        body='''{\n\t\ttargets = { EST LAT LIT }\n\t\ttarget_trigger = { exists = yes }\n}'''
        with patch.object(politics, '_blocks', return_value={'baltic':body}):
            self.assertEqual(politics.decision_targets('baltic'), ['EST','LAT','LIT'])
            self.assertEqual(politics.decision_targets('ordinary'), [])

    def test_country_target_routes_to_politics_and_not_exile(self):
        agent=agents.Politics.__new__(agents.Politics)
        agent.this_turn=set()
        gw=types.SimpleNamespace(run=lambda fn,*args:fn(None,*args))
        with patch.object(politics,'decide',return_value={'decision':'baltic','target':'EST'}) as decide:
            result=agent.handle({'gw':gw},'take_decision',{'key':'baltic','target':'EST'})
        decide.assert_called_once_with(None,'baltic','EST')
        self.assertEqual(result['target'],'EST')

    def test_missing_target_is_refused_before_any_command(self):
        with patch.object(politics,'decision_targets',return_value=['EST']),patch.object(politics,'decision_is_targeted',return_value=True),patch.object(politics.B,'module') as bridge:
            with self.assertRaisesRegex(Exception,'target'):
                politics.decide(None,'baltic')
        bridge.assert_not_called()

    def test_target_options_check_each_actual_country_and_never_post(self):
        commands=[]
        g=types.SimpleNamespace(decisions=lambda *a:[{'key':'baltic'}],tags=lambda:['EST','LAT','LIT'])
        def build(g,key,tag):
            commands.append((key,tag));return tag
        cab=types.SimpleNamespace(_attached=lambda g,fn:fn())
        industry=types.SimpleNamespace(can_execute=lambda g,cmd:cmd=='LAT')
        from harness import exile
        with patch.object(politics,'decision_targets',return_value=['EST','LAT','LIT']),patch.object(exile,'_target_cmd',side_effect=build),patch.object(politics.B,'module',side_effect=lambda name:{'cabinet':cab,'industry':industry}[name]):
            self.assertEqual(politics.targeted_decisions(g),[{'key':'baltic','target':'LAT'}])
        self.assertEqual(commands,[('baltic','EST'),('baltic','LAT'),('baltic','LIT')])

    def test_file_only_country_tag_does_not_abort_politics_briefing(self):
        from harness import exile
        g=types.SimpleNamespace(decisions=lambda *a:[{'key':'aid_spain'}],tags=lambda:['SPR','ITA'])
        def build(g,key,tag):
            if tag=='SPA':raise KeyError('unknown tag SPA')
            return tag
        cab=types.SimpleNamespace(_attached=lambda g,fn:fn())
        industry=types.SimpleNamespace(can_execute=lambda g,cmd:True)
        with patch.object(politics,'decision_targets',return_value=['SPA','SPR']),patch.object(exile,'_target_cmd',side_effect=build),patch.object(politics.B,'module',side_effect=lambda name:{'cabinet':cab,'industry':industry}[name]):
            self.assertEqual(politics.targeted_decisions(g),[{'key':'aid_spain','target':'SPR'}])

    def test_inactive_purge_system_is_reported_with_actual_value(self):
        c=types.SimpleNamespace(stability=lambda:.72,war_support=lambda:.5)
        def console(cmd):
            return types.SimpleNamespace(ok=True,message='Result = no' if cmd.startswith('eval_trigger') else 'SOV_paranoia = 0')
        g=types.SimpleNamespace(player=lambda:c,player_tag=lambda:'SOV',console=console)
        text='\n'.join(politics.country_mechanics(g))
        self.assertIn('72.0%',text)
        self.assertIn('paranoia 0.0; system active no',text)

class LawBriefingTests(unittest.TestCase):
    def test_comparison_keeps_both_benefits_and_energy_penalty(self):
        cats={'civilian':('economy',150,'consumer_goods_expected_value = 0.35 factory_energy_consumption = -0.3',False),
              'military':('economy',150,'consumer_goods_expected_value = 0.2 factory_energy_consumption = 0.25',True)}
        with patch.object(politics,'idea_categories',return_value=cats):
            text='\n'.join(politics.law_lines({'economy':{'now':'civilian','may_adopt':['military']}}))
        for part in ('civilian','military','150 PP','0.35','0.2','-0.3','0.25'):
            self.assertIn(part,text)

class MissingDecisionTests(unittest.TestCase):
    def test_spirit_remedy_is_checked_even_when_missing_from_visible_vector(self):
        from harness import spirits
        key='relax_academies'
        g=types.SimpleNamespace(date_raw=lambda:24,politics=lambda:{'active_ideas':['banned_thinking']},
                                decisions=lambda *a:[{'key':key,'ptr':123}] if a[-1]=='all' else [])
        cab=types.SimpleNamespace(takeable_decisions=lambda g:[])
        with patch.object(politics.B,'module',return_value=cab),patch.object(spirits,'decisions',return_value={'banned_thinking':[key]}),patch.object(politics,'decision_available',return_value=True) as available:
            self.assertEqual(politics.takeable_decisions(g),[key])
        available.assert_called_once_with(g,key)

    def test_undisplayed_remedy_is_not_exposed_when_game_refuses(self):
        from harness import spirits
        key='relax_academies'
        g=types.SimpleNamespace(date_raw=lambda:24,politics=lambda:{'active_ideas':['banned_thinking']},
                                decisions=lambda *a:[{'key':key,'ptr':123}])
        cab=types.SimpleNamespace(takeable_decisions=lambda g:[])
        with patch.object(politics.B,'module',return_value=cab),patch.object(spirits,'decisions',return_value={'banned_thinking':[key]}),patch.object(politics,'decision_available',return_value=False):
            self.assertEqual(politics.takeable_decisions(g),[])

class DecisionTimerTests(unittest.TestCase):
    def test_running_30_day_decision_is_read_from_remove_queue_not_mission_queue(self):
        from tests.test_air_reserves import Memory
        import struct
        p=Memory();p.write(0x1000+0xF78,struct.pack('<Q',0x2000))
        p.write(0x4000+8,struct.pack('<Q',0x5000));p.write(0x4000+0x10,struct.pack('<i',25))
        G=types.SimpleNamespace(CO_DECISIONS=0xF78,_arr=lambda p,obj,off:[0x4000] if off==0x70 else [])
        g=types.SimpleNamespace(p=p,player=lambda:types.SimpleNamespace(ptr=0x1000),_decision_name=lambda ptr:'academies' if ptr==0x5000 else None)
        from harness import events
        body='{\n\t\tremove_effect = { remove_ideas = banned_thinking }\n}'
        with patch.object(politics.B,'module',return_value=G),patch.object(politics,'_blocks',return_value={'academies':body}),patch.object(events,'loc',side_effect=lambda key:key):
            rows=politics.running_missions(g)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['days_left'],25)
        self.assertFalse(rows[0]['mission'])
        self.assertIn('banned_thinking',rows[0]['on_timeout'])
