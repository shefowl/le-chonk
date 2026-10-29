#!/usr/bin/env python3
"""Build an allowlisted public source release, including the exact bridge fork."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
ROOT=Path(__file__).resolve().parents[1]


def revision(path):
    if not (path/'.git').exists():return 'source-release'
    return subprocess.check_output(['git','rev-parse','HEAD'],cwd=path,text=True).strip()


def source_files(root=ROOT):
    root=Path(root)
    explicit=['README.md','LICENSE','.gitignore','.env.example','requirements.txt','requirements-dev.txt',
              'config/llm.example.json','config/italy-roster.json','docs/RUNBOOK.md','docs/DEVELOPMENT.md',
              'docs/2026-10-29-final-audit.md']
    files={root/p for p in explicit if (root/p).is_file()}
    for directory in ('harness','tools','tests'):
        files.update((root/directory).rglob('*.py'))
    files.update((root/'tools').glob('*.sh'))
    files.update((root/'mod').glob('*.py'))
    files.update((root/'docs/submission').glob('*.md'))
    files.update((root/'docs/ports').glob('*.md'))
    files.update((root/'knowledge/wiki').glob('*.txt'))
    # Curated shared facts retain source URLs, dates and versions.
    if (root/'knowledge/facts.json').exists():files.add(root/'knowledge/facts.json')
    bridge=root/'bridge'
    if (bridge/'.git').exists():
        listed=subprocess.check_output(['git','ls-files','-z'],cwd=bridge).decode().split('\0')
        files.update(bridge/p for p in listed if p and (bridge/p).is_file())
    else:
        files.update(p for p in bridge.rglob('*') if p.is_file() and
                     (p.suffix in ('.py','.md','.json','.txt','.svg') or p.name=='LICENSE'))
    return sorted(p for p in files if not p.is_symlink() and '__pycache__' not in p.parts
                  and p.suffix not in ('.pkl','.dds','.exe') and '.git' not in p.parts)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'cache-submission/le-chonk-source.zip')
    args=parser.parse_args()
    if not (ROOT/'bridge/calling/game.py').exists():raise SystemExit('The exact bridge fork must be present')
    # Compare against local real keys without printing them or placing them in the manifest.
    sys.path.insert(0,str(ROOT))
    from harness.llm import load_env
    from harness import credentials
    load_env()
    keys=[os.environ.get(name,'') for name in ('MISTRAL_API_KEY','OPENROUTER_API_KEY','LOCAL_LLM_KEY')]
    try:keys.append(credentials.load().get('api_key',''))
    except ValueError:raise SystemExit('Fix private config before packaging (values omitted)')
    secrets=[key.encode() for key in keys if len(key)>=12]
    manifest={'source_commit':revision(ROOT),'bridge_commit':revision(ROOT/'bridge'),
              'upstream':'https://github.com/smerss/Clausewitz-MCP','files':{},
              'note':'Source only. Generate bridge tables and GUI textures from your own compatible HOI4 installation.'}
    payload=[]
    for path in source_files():
        name=path.relative_to(ROOT).as_posix();data=path.read_bytes()
        if any(key in data for key in secrets):raise SystemExit('Refusing release: credential detected in '+name)
        if path.stat().st_mode&0o111 and path.suffix=='.sh':mode=0o755
        else:mode=0o644
        payload.append((name,data,mode));manifest['files'][name]=hashlib.sha256(data).hexdigest()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data,mode in payload:
            item=zipfile.ZipInfo('le-chonk/'+name,date_time=(2026,10,29,0,0,0))
            item.external_attr=(0o100000|mode)<<16;item.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(item,data)
        archive.writestr('le-chonk/SOURCE_MANIFEST.json',json.dumps(manifest,indent=2)+'\n')
    print('Packaged %d source files into %s (%d bytes)'%(len(payload),args.output,args.output.stat().st_size))


if __name__=='__main__':main()
