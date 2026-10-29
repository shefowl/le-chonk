"""A localisation address may be reallocated to a game object after loading a save."""
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from harness import modui

class MarkerMemoryTests(unittest.TestCase):
    def ui(self, data):
        self.memory=dict(data); self.writes=[]
        def write(a,b):self.writes.append((a,b));self.memory[a]=b
        g=types.SimpleNamespace(p=types.SimpleNamespace(pid=1,base=100,
            try_read=lambda a,n:self.memory.get(a,b'')[:n],write=write),
            _inj=None,attach=lambda:None,detach=lambda:None)
        ui=modui.ModUI(types.SimpleNamespace(run=lambda f:f(g)),log=lambda x:None)
        ui.present=True;ui.at={'ST_POLITICS':[(1000,100)]}
        ui.expected={1000:hashlib.sha256(b'old'.ljust(100,b' ')).hexdigest()}
        return ui

    def test_fresh_marker_is_discovered_and_safe_cache_survives_harness_restart(self):
        prefix=b'LCM_ST_POLITICS_'
        ui=self.ui({1000:prefix+b'.'*(100-len(prefix))})
        p=types.SimpleNamespace(pid=1,base=100,regions=lambda **kw:[{'start':1000,'end':1100}],
            try_read=lambda a,n:self.memory[a][:n],write=lambda a,b:self.memory.__setitem__(a,b))
        g=types.SimpleNamespace(p=p,_inj=None,attach=lambda:None,detach=lambda:None)
        ui.gw.run=lambda f:f(g)
        ui.at=None;ui.expected={}
        with tempfile.TemporaryDirectory() as d, patch.object(modui.roster,'ROOT',d):
            Path(d,'logs').mkdir()
            ui.write({'ST_POLITICS':'first'})
            p.regions=lambda **kw:[]
            restarted=modui.ModUI(ui.gw,log=lambda x:None);restarted.present=True
            restarted.write({'ST_POLITICS':'second'})
            self.assertTrue(self.memory[1000].startswith(b'second'))
            self.assertEqual(json.loads(Path(d,'logs/.lc_markers.json').read_text())['v'],3)

    def test_reused_marker_address_is_never_written(self):
        ui=self.ui({1000:b'\xf8\x43\x44\x04'+b'\0'*96})
        with tempfile.TemporaryDirectory() as d, patch.object(modui.roster,'ROOT',d):
            Path(d,'logs').mkdir()
            ui.write({'ST_POLITICS':'1936.01.01: focus running'})
        self.assertEqual(self.writes, [], 'GUI overwrote a reused object address')
        self.assertNotIn('ST_POLITICS',ui.shown)

    def test_valid_marker_can_be_updated_repeatedly(self):
        ui=self.ui({1000:b'old'.ljust(100,b' ')})
        with tempfile.TemporaryDirectory() as d, patch.object(modui.roster,'ROOT',d):
            Path(d,'logs').mkdir()
            ui.write({'ST_POLITICS':'first'})
            ui.write({'ST_POLITICS':'second'})
        self.assertEqual(len(self.writes),2)
        self.assertTrue(self.memory[1000].startswith(b'second'))

    def test_legacy_cache_cannot_authorize_a_write_after_reload(self):
        ui=self.ui({1000:b'bad'.ljust(100,b' ')})
        ui.gw.run=lambda f:f(types.SimpleNamespace(p=types.SimpleNamespace(
            pid=1,base=100,regions=lambda **kw:[],try_read=lambda a,n:self.memory[a][:n])))
        with tempfile.TemporaryDirectory() as d, patch.object(modui.roster,'ROOT',d):
            Path(d,'logs').mkdir()
            Path(d,'logs/.lc_markers.json').write_text(json.dumps({'v':2,'pid':1,'base':100,'at':{'ST_POLITICS':[[1000,100]]}}))
            found=ui._find()
        self.assertEqual(found,{})

    def test_v3_cache_is_revalidated_after_reload_in_same_process(self):
        ui=self.ui({1000:b'AI vptr'.ljust(100,b'\0')})
        ui.gw.run=lambda f:f(types.SimpleNamespace(p=types.SimpleNamespace(
            pid=1,base=100,regions=lambda **kw:[],try_read=lambda a,n:self.memory[a][:n])))
        with tempfile.TemporaryDirectory() as d, patch.object(modui.roster,'ROOT',d):
            Path(d,'logs').mkdir()
            Path(d,'logs/.lc_markers.json').write_text(json.dumps({'v':3,'pid':1,'base':100,
                'at':{'ST_POLITICS':[[1000,100]]},'expected':{'1000':ui.expected[1000]}}))
            self.assertEqual(ui._find(),{})

    def test_validation_and_write_freeze_engine_to_prevent_reallocation(self):
        ui=self.ui({1000:b'old'.ljust(100,b' ')})
        frozen=[]
        def write(a,b):
            self.assertTrue(frozen)
            self.memory[a]=b
        g=types.SimpleNamespace(p=types.SimpleNamespace(pid=1,base=100,
            try_read=lambda a,n:self.memory[a][:n],write=write), _inj=None,
            attach=lambda:frozen.append(True),detach=lambda:frozen.pop())
        ui.gw.run=lambda f:f(g)
        with tempfile.TemporaryDirectory() as d, patch.object(modui.roster,'ROOT',d):
            Path(d,'logs').mkdir()
            ui.write({'ST_POLITICS':'new'})
        self.assertEqual(frozen,[])
