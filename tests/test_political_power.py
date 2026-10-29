"""A full cabinet must not hide available law choices from a PP review."""
import types
import unittest
from unittest.mock import patch
from harness import agents, politics

class PowerReviewTests(unittest.TestCase):
    def review(self, laws, pp=476, decisions=()):
        agent=agents.Politics.__new__(agents.Politics);agent.name='Politics'
        g=types.SimpleNamespace(politics=lambda:{'political_power':pp})
        ctx={'day':1,'gw':types.SimpleNamespace(run=lambda fn:fn(g))}
        slots={'advisors':{},'concern':[],'policies':[]}
        with patch.object(politics,'empty_slots',return_value=slots),patch.object(politics,'decision_options',return_value=list(decisions)),patch.object(agents,'B_module',return_value=types.SimpleNamespace(laws=lambda g:laws)):
            return agent._idle_power(ctx)

    def test_full_cabinet_still_prompts_review_of_available_laws(self):
        text=self.review({'economy':{'now':'civilian_economy','may_adopt':['partial_economic_mobilisation','war_economy']}})
        self.assertIsNotNone(text)
        self.assertIn('civilian_economy',text)
        self.assertIn('war_economy',text)
        self.assertIn('explain',text)

    def test_no_law_available_does_not_invent_a_purchase(self):
        self.assertIsNone(self.review({'economy':{'now':'war_economy','may_adopt':[]}}))

    def test_below_reserve_does_not_prompt_a_purchase(self):
        self.assertIsNone(self.review({'economy':{'now':'civilian_economy','may_adopt':['war_economy']}},pp=40))

    def test_available_decision_prompts_review_even_without_law_choices(self):
        text=self.review({},decisions=['baltic target=EST'])
        self.assertIn('baltic target=EST',text)
        self.assertIn('take_decision',text)
