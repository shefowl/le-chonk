#!/usr/bin/env python3
"""Prepare/check Italy; --start explicitly starts a fresh paid Studio campaign."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import time
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.preflight import checks,pids


def archive_state(root=ROOT):
    """Preserve the previous campaign/chat/roster without touching GUI marker caches."""
    root=Path(root)
    destination=root/'logs'/'archive'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    destination.mkdir(parents=True,mode=0o700)
    for relative in ('logs/campaign.json','logs/chat.json','roster.json'):
        source=root/relative
        if source.exists():shutil.copy2(source,destination/source.name)
    for relative in ('logs/campaign.json','logs/chat.json'):
        (root/relative).unlink(missing_ok=True)
    shutil.copyfile(root/'config/italy-roster.json',root/'roster.json')
    return destination


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',action='store_true',help='Start paid final run after all checks pass')
    parser.add_argument('--api',action='store_true',help='Also verify exact model access with GET /models')
    parser.add_argument('--days',type=int,default=30,help='Game days; use 1 for integration smoke')
    args=parser.parse_args()
    if not 1<=args.days<=3650:parser.error('--days must be 1..3650')
    if args.start:
        from harness.ownership import acquire
        writer_lock=acquire()
    rows=checks(api=args.api,live=True,expect='ITA')
    for row in rows:print(('[OK] ' if row['ok'] else '[BLOCK] ')+row['check']+': '+row['detail'],flush=True)
    if not all(row['ok'] for row in rows):return 1
    if not args.start:
        print('Checks passed. Load Italy 1 January 1936 on pause; --start begins paid calls.');return 0
    # Recheck admission before modifying persistent campaign files.
    if pids(r'^(/[^ ]*/)?python3 -u -m harness[.]run'):raise RuntimeError('Harness started during preflight; refusing second writer')
    destination=archive_state()
    print('Previous state preserved in '+str(destination),flush=True)
    manifest={'started_at':time.time(),'provider':'mistral','model':'mistral-large-4',
              'country':'ITA','expected_start_day':'1936.01.01','days':args.days,
              'state_archive':str(destination.relative_to(ROOT)),
              'source_commit':__import__('subprocess').check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip() if (ROOT/'.git').exists() else 'source-release',
              'bridge_build':next(row['detail'] for row in rows if row['check']=='build')}
    (ROOT/'logs/final-latest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (destination/'run-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print('Starting all roles: Mistral Studio / mistral-large-4. No --resume.',flush=True)
    os.chdir(ROOT)
    os.execv(str(ROOT/'tools/run_campaign.sh'),[str(ROOT/'tools/run_campaign.sh'),
             '--models','lechonk','--days',str(args.days),'--speed','4','--peace-speed','4',
             '--history','on','--pause','fires','--expect-country','ITA',
             '--expect-start-day','1936.01.01','--watch','ETH'])


if __name__=='__main__':sys.exit(main())
