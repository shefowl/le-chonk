#!/usr/bin/env python3
"""Install a launcher descriptor referencing the built mod; no game interaction."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-dir',type=Path,default=Path.home()/'.local/share/Paradox Interactive/Hearts of Iron IV')
    args=parser.parse_args()
    descriptor=ROOT/'mod/le_chonk/descriptor.mod'
    if not descriptor.exists():parser.error('Build the mod first: python3 mod/build.py')
    directory=args.user_dir/'mod';directory.mkdir(parents=True,exist_ok=True)
    target=directory/'le_chonk.mod'
    target.write_text(descriptor.read_text()+'\npath="'+str(descriptor.parent).replace('\\','/').replace('"','\\"')+'"\n')
    print('Installed '+str(target));print('Enable Le Chonk control in your launcher playset and restart HOI4.')


if __name__=='__main__':main()
