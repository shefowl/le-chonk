#!/usr/bin/env python3
"""One tiny PAID Studio tool-call integration check; never accesses the game."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harness.llm import LLM
from harness.credentials import LECHONK


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Explicitly make one paid model request')
    args=parser.parse_args()
    if not args.run:
        print('No request made. --run makes one paid mistral-large-4 tool call via Studio.');return
    client=LLM('mistral')
    tool={'type':'function','function':{'name':'ping','description':'Return the word ok.',
          'parameters':{'type':'object','properties':{'value':{'type':'string','enum':['ok']}},'required':['value']}}}
    message,usage,elapsed=client.chat(LECHONK,[{'role':'user','content':'Call ping with value ok.'}],
          tools=[tool],tool_choice='any',max_tokens=64,reasoning='none',budget_s=45,role='integration/studio-ping')
    calls=message.get('tool_calls') or []
    passed=len(calls)==1 and calls[0]['function']['name']=='ping' and json.loads(calls[0]['function']['arguments'])=={'value':'ok'}
    print(json.dumps({'provider':'mistral','model':LECHONK,'tool_call_verified':passed,
                      'latency_s':round(elapsed,3),'prompt_tokens':usage.get('prompt_tokens'),
                      'completion_tokens':usage.get('completion_tokens')},indent=2))
    if not passed:raise SystemExit('Tool call did not match the integration contract')


if __name__=='__main__':main()
