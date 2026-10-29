#!/usr/bin/env python3
"""Rebuild generated static command tables from a compatible installed game ELF."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harness.bridge import module as B


def main():
    config=B('config');offsets=B('offsets')
    if config.disk_build_id()!=offsets.BUILD_ID:
        raise SystemExit('Build mismatch. Port/verify the bridge or install the supported build before generating tables.')
    B('rtti').build()
    B('funcs').FuncIndex(B('elf').ELF(config.binary_path()))
    for module in ('cmdauto','cmdctor','stats'):
        result=B(module).build(force=True)
        print(module+': '+str(len(result)))


if __name__=='__main__':main()
