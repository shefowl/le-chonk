#!/usr/bin/env python3
"""Aggregate measured API usage for a recorded run; no prompts, responses or keys exported."""
import argparse
from collections import Counter
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]


def aggregate(rows, started_at):
    calls=[];retries=0;malformed=0;failed=0
    for row in rows:
        if not isinstance(row,dict):malformed+=1;continue
        try:
            if float(row.get('t',0))<started_at:continue
        except (ValueError,TypeError):malformed+=1;continue
        if 'retry' in row:retries+=1;continue
        if row.get('event') in ('timeout','invalid_output','http_error'):
            failed+=1;continue
        if row.get('provider') and row.get('model') and 'latency_s' in row:calls.append(row)
    def token_total(key):
        values=[r.get(key) for r in calls]
        return dict(known_total=sum(v for v in values if isinstance(v,(int,float))),
                    unknown_calls=sum(v is None for v in values))
    costs=[r.get('cost_usd') for r in calls]
    elapsed=max((r['t']-started_at for r in calls),default=0)
    return dict(successful_call_records=len(calls), failed_call_records=failed, retries=retries, malformed_records=malformed,
                providers=dict(Counter(r['provider'] for r in calls)),
                models=dict(Counter(r['model'] for r in calls)),
                roles=dict(Counter(r.get('role','') for r in calls)),
                prompt_tokens=token_total('prompt_tokens'), completion_tokens=token_total('completion_tokens'),
                known_cost_usd=sum(v for v in costs if isinstance(v,(int,float))),
                cost_unknown_calls=sum(v is None for v in costs),
                observed_api_window_s=round(elapsed,3),
                note='Usage records only; game outcomes and total recording duration require separate evidence. Missing cost is unknown, not zero.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--log',type=Path,default=ROOT/'logs/llm.jsonl')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    manifest=json.loads(args.manifest.read_text())
    rows=[]
    if args.log.exists():
        for line in args.log.read_text().splitlines():
            try:rows.append(json.loads(line))
            except ValueError:continue
    report=aggregate(rows,float(manifest['started_at']))
    report['profile']={k:manifest.get(k) for k in ('provider','model','country','expected_start_day','source_commit','bridge_build')}
    report['generated_at']=datetime.now(timezone.utc).isoformat()
    data=json.dumps(report,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(data)
    else:print(data,end='')


if __name__=='__main__':main()
