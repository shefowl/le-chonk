"""Exercise admission must use actual hostile borders before theatres exist."""
import types
import unittest
from unittest.mock import patch
from harness import agents, ops


class ExerciseFrontTests(unittest.TestCase):
    def test_front_army_without_marshal_is_refused_before_any_training_command(self):
        agent=agents.Army.__new__(agents.Army)
        mv=types.SimpleNamespace(adj={10:{11},20:{21}},land={10,11,20,21})
        g=object()
        ctx={'gw':types.SimpleNamespace(run=lambda fn,*a:fn(g,*a)),
             'mv':mv,'enemies':{'ETH'},'marshals':{}}
        with patch.object(ops,'_ogs',return_value=[100,200]), \
             patch.object(ops,'theatre_of',return_value=120), \
             patch.object(ops,'division_bases',side_effect=lambda g,og:[og+1]), \
             patch.object(ops,'province_of',side_effect=lambda g,d:{101:10,201:20}[d]), \
             patch.object(ops,'_ctl',return_value=lambda p:{10:'ITA',11:'ETH',20:'ITA',21:'FRA'}[p]), \
             patch.object(ops,'exercise',side_effect=AssertionError('training command must not be posted')):
            with self.assertRaisesRegex(ops.OpError,'482.*ETH'):
                agent.handle(ctx,'exercise',{'armies':[482,483],'on':True})

    def test_peacetime_border_and_wartime_reserve_may_exercise(self):
        agent=agents.Army.__new__(agents.Army)
        mv=types.SimpleNamespace(adj={20:{21}},land={20,21})
        ctx={'gw':types.SimpleNamespace(run=lambda fn,*a:fn(None,*a)),
             'mv':mv,'enemies':{'ETH'},'marshals':{}}
        with patch.object(ops,'_ogs',return_value=[200]), \
             patch.object(ops,'theatre_of',return_value=120), \
             patch.object(ops,'division_bases',return_value=[201]), \
             patch.object(ops,'province_of',return_value=20), \
             patch.object(ops,'_ctl',return_value=lambda p:'FRA' if p==21 else 'ITA'), \
             patch.object(ops,'exercise',return_value={483:'exercising'}):
            self.assertEqual(agent.handle(ctx,'exercise',{'armies':[483],'on':True}),{483:'exercising'})
