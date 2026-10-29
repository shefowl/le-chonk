"""A model must see failed batch results before saving its optimistic summary."""
import json
import types
import unittest

from harness.agents import Agent
from harness.ops import OpError


def call(name, args):
    return {'id': name, 'function': {'name': name, 'arguments': json.dumps(args)}}


class TurnOutcomeTests(unittest.TestCase):
    def run_turn(self, nested=False, success=False):
        requests, records = [], []
        replies = [
            {'role': 'assistant', 'tool_calls': [call('build', {}), call('end_turn', {
                'summary': 'Twelve military factories were successfully queued in the interior.'})]},
            {'role': 'assistant', 'tool_calls': [call('end_turn', {
                'summary': 'No factories were added: construction was refused, existing queue retained.'})]}]

        def chat(*a, **kw):
            requests.append(a)
            return replies[len(requests)-1], {}, 0

        class Staff(Agent):
            def tools(self): return []
            def handle(self, ctx, name, args):
                if success: return {'queued': 12}
                if nested: return {'ok': False, 'error': 'not confirmed after post'}
                raise OpError('the game queued no factories')

        a = Staff('test', 'test', types.SimpleNamespace(chat=chat),
                  types.SimpleNamespace(write=lambda **k: records.append(k)), 'test')
        summary = a.turn({'date': 'now'}, 'build factories')
        return a, summary, requests, records

    def test_failed_action_holds_same_batch_summary_until_results_are_read(self):
        a, summary, requests, records = self.run_turn()
        self.assertEqual(len(requests), 2)
        self.assertIn('No factories were added', summary)
        self.assertNotIn('successfully', ' '.join(a.memory))
        self.assertFalse(next(r for r in records if r.get('action') == 'end_turn')['outcome']['ok'])

    def test_nested_unconfirmed_effect_also_holds_summary(self):
        _, summary, requests, _ = self.run_turn(nested=True)
        self.assertEqual(len(requests), 2)
        self.assertIn('No factories were added', summary)

    def test_successful_batch_does_not_add_a_model_call(self):
        _, summary, requests, _ = self.run_turn(success=True)
        self.assertEqual(len(requests), 1)
        self.assertIn('successfully queued', summary)


if __name__ == '__main__': unittest.main()
