#!/usr/bin/env python3
"""Preflight with no game writes and no completions. --api is a GET /models access check."""
import argparse
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harness import credentials
from harness.llm import LLM,load_env


def pids(pattern, exact=False):
    result=subprocess.run(['pgrep','-x' if exact else '-f',pattern],capture_output=True,text=True)
    return [int(p) for p in result.stdout.split() if p.isdigit()]


def checks(*,api=False,live=False,expect=None):
    rows=[]
    def add(name,ok,detail):rows.append(dict(check=name,ok=bool(ok),detail=detail))
    add('platform',sys.platform.startswith('linux') and platform.machine().lower() in ('x86_64','amd64'),'Harness supports native Linux x86-64 HOI4; not Proton/Windows')
    add('systemd',bool(shutil.which('systemd-run')),'Required for the 6 GiB process-tree limit')
    add('build_guard',not os.environ.get('CLAUSEWITZ_ANY_BUILD'),'Build override must be unset')
    try:
        from harness.bridge import module as B
        cfg=B('config');offsets=B('offsets')
        disk=cfg.disk_build_id()
        add('build',disk==offsets.BUILD_ID,'installed=%s; expected=%s (%s)'%(disk,offsets.BUILD_ID,offsets.VERSION))
    except Exception as exc:
        add('build',False,'Cannot inspect bridge/install: '+type(exc).__name__)
    try:
        client=LLM('mistral')
        add('studio_key',bool(client.key),'Studio key available; exact model '+credentials.LECHONK)
        if api:
            import requests
            try:
                with client.session.get(client.base+'/models',headers={'Authorization':'Bearer '+client.key},timeout=20,allow_redirects=False) as response:
                    if response.status_code!=200:add('model_access',False,'HTTP %d'%response.status_code)
                    else:
                        ids={m.get('id') for m in response.json().get('data',[]) if isinstance(m,dict)}
                        add('model_access',credentials.LECHONK in ids,'Exact contest model is '+('listed' if credentials.LECHONK in ids else 'not listed'))
            except (requests.RequestException,ValueError):add('model_access',False,'Network or response error; credentials omitted')
    except Exception as exc:
        # Never print provider response bodies or configuration values.
        add('studio_key',False,'Studio setup unavailable: '+type(exc).__name__+'; open tools/configure_model.py')
    harness=pids(r'^(/[^ ]*/)?python3 -u -m harness[.]run')
    add('single_writer',not harness,'Harness '+('already running: '+','.join(map(str,harness)) if harness else 'not running'))
    game=pids('hoi4',exact=True)
    add('game',len(game)==1,'Exactly one native hoi4 process is needed' if len(game)!=1 else 'pid='+str(game[0]))
    if live and not harness and len(game)==1 and not os.environ.get('CLAUSEWITZ_ANY_BUILD'):
        try:
            # Constructor validates the RUNNING ELF as well. read_only rejects every call.
            g=B('game').Game(game[0]);g.read_only=True
            tag=g.player_tag();paused=B('session').is_paused(g)
            add('country',expect is None or tag==expect,'country='+str(tag))
            add('paused',paused,'Game must be paused before starting')
            enemies=B('combat').enemy_tags(g)
            add('ethiopia',expect!='ITA' or 'ETH' in enemies,'enemies='+','.join(sorted(enemies)))
            # Date is informational until Gateway performs bounded calibration on start.
            add('clock',True,'Date will be calibrated by Gateway before any agent turn')
        except Exception as exc:
            add('live_read',False,'Read-only guard/check failed: '+type(exc).__name__)
    elif live:
        add('live_read',False,'Live checks skipped until process/build prerequisites are met')
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api',action='store_true');parser.add_argument('--live',action='store_true')
    parser.add_argument('--expect',default=None);parser.add_argument('--json',action='store_true')
    args=parser.parse_args();rows=checks(api=args.api,live=args.live,expect=args.expect)
    if args.json:print(json.dumps(rows,indent=2))
    else:
        for row in rows:print(('[OK] ' if row['ok'] else '[BLOCK] ')+row['check']+': '+row['detail'])
    return 0 if all(row['ok'] for row in rows) else 1


if __name__=='__main__':sys.exit(main())
