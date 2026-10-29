"""Incoming focus warnings persist, wake command, and expose the deployment tool."""
import types
import unittest
from unittest.mock import patch
from harness import agents, air, briefing, threats
from harness.run import Runner


class WarThreatIntegrationTests(unittest.TestCase):
    report={'pairs':[('GER','SOV')], 'threats':[{'from':'GER','target':'SOV','source':'focus','focus_key':'war_with_ussr'}], 'coverage_errors':[]}

    def test_focus_warning_wakes_supreme_and_all_marshals_once(self):
        r=Runner.__new__(Runner)
        r.own='SOV';r.last_tension_day=None;r.tension=set();r.alarms=[]
        r.supreme=types.SimpleNamespace(inbox=[])
        r.marshals={1:types.SimpleNamespace(theatre={'enemy':'GER'},inbox=[]),2:types.SimpleNamespace(theatre={'enemy':'JAP'},inbox=[])}
        r.gw=types.SimpleNamespace(run=lambda *args:self.report)
        events=[];r.journal=types.SimpleNamespace(write=lambda **kw:events.append(kw))
        r.watch_tension(1);r.watch_tension(1);r.watch_tension(2)
        self.assertEqual(len(r.alarms),3)
        self.assertIn('war_with_ussr',r.supreme.inbox[0])
        self.assertTrue(all(len(m.inbox)==1 for m in r.marshals.values()))
        self.assertEqual(len(events),1)

    def test_warning_remains_in_later_briefings(self):
        ctx={'own':'SOV','war_threats':self.report}
        self.assertEqual(briefing.war_threat_lines(ctx),briefing.war_threat_lines(ctx))
        self.assertIn('war_with_ussr',' '.join(briefing.war_threat_lines(ctx)))

    def test_snapshot_is_shared_only_within_current_game_day(self):
        day=[24];g=types.SimpleNamespace(date_raw=lambda:day[0],player_tag=lambda:'SOV')
        with patch.object(threats,'snapshot',return_value=self.report) as snap:
            threats.cached_snapshot(g);threats.cached_snapshot(g)
            self.assertEqual(snap.call_count,1)
            day[0]=48;threats.cached_snapshot(g)
            self.assertEqual(snap.call_count,2)

    def test_physical_gap_is_reviewed_before_incoming_war(self):
        m=agents.Marshal.__new__(agents.Marshal)
        m.alerts=lambda ctx:[('HOLE',89,'2 border provinces with no division of ours')]
        ctx={'own':'SOV','war_threats':self.report}
        with patch.object(agents,'at_war_in',return_value=False),patch.object(agents,'facing',return_value={'GER','HUN'}):
            self.assertIn('2 border provinces',m.before_end(ctx))


class WingToolIntegrationTests(unittest.TestCase):
    def test_model_tool_routes_selection_and_records_new_wing_owner(self):
        agent=agents.AirStaff.__new__(agents.AirStaff);agent.this_turn=set()
        ctx={'holders':{1:('SOV','SOV')},'own':'SOV','gw':types.SimpleNamespace(run=lambda fn,*args:fn(None,*args))}
        with patch.object(air,'create_wing',return_value={'ok':True,'created_wings':[900],'planes':100}) as create:
            agent.handle(ctx,'create_air_wing',{'state_id':1,'design_id':1477,'planes':100})
        create.assert_called_once_with(None,1,1477,100,ctx['holders'],'SOV')
        self.assertEqual(ctx['air_owner'][900],'air staff')

    def test_refused_creation_does_not_claim_new_owner(self):
        agent=agents.AirStaff.__new__(agents.AirStaff);agent.this_turn=set()
        ctx={'holders':{},'own':'SOV','gw':types.SimpleNamespace(run=lambda fn,*args:fn(None,*args))}
        with patch.object(air,'create_wing',return_value={'ok':False,'error':'no reserve'}):
            with self.assertRaisesRegex(Exception,'no reserve'):
                agent.handle(ctx,'create_air_wing',{'state_id':1,'design_id':1477,'planes':100})
        self.assertNotIn('air_owner',ctx)

    def test_new_wing_can_receive_its_first_mission_in_the_same_turn(self):
        agent=agents.AirStaff.__new__(agents.AirStaff);agent.this_turn=set()
        ctx={'holders':{1:('SOV','SOV')},'own':'SOV','enemies':set(),'mv':object(),
             'gw':types.SimpleNamespace(run=lambda fn,*args:fn(None,*args))}
        def assign(g,region,mission,ids,kind,count,exclude):
            self.assertNotIn(900,exclude)
            self.assertEqual(ids,[900])
            return {'wings_flying_it':[900]}
        with patch.object(air,'create_wing',return_value={'ok':True,'created_wings':[900],'planes':100}),patch.object(agents,'briefing_air_regions',return_value=types.SimpleNamespace(name={1:'test region'})),patch.object(air,'assign',side_effect=assign):
            agent.handle(ctx,'create_air_wing',{'state_id':1,'design_id':1477,'planes':100})
            agent.handle(ctx,'air_mission',{'wings':[900],'region':1,'mission':'air_superiority'})
        # The usual guard still prevents reordering after that first real mission.
        self.assertIn(900,agent._not_mine(ctx))

    def test_end_turn_returns_created_ids_until_first_mission_is_reviewed(self):
        agent=agents.AirStaff.__new__(agents.AirStaff);agent.this_turn={('created_wing',900)}
        ctx={'gw':types.SimpleNamespace(run=lambda fn:fn(None))}
        rows=[{'id':900,'planes':100,'missions':[],'army':None}]
        with patch.object(agents.Supreme,'before_end',return_value=None),patch.object(air,'wings',return_value=rows):
            text=agent.before_end(ctx)
        self.assertIsNotNone(text)
        self.assertIn('900',text)
        self.assertIn('end_turn again',text)
