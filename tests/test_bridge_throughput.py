"""Pure-read bridge speed regressions on synthetic memory, never a live process."""
import struct
import types
import unittest
from unittest.mock import patch
from harness.bridge import module

class SubjectLookupTests(unittest.TestCase):
    def test_subjects_keep_sparse_tag_ids_without_loading_every_country(self):
        combat=module('combat');game=module('game')
        me=types.SimpleNamespace(ptr=0x1000,tag_id=2)
        looked=[]
        def country(tag):
            looked.append(tag)
            if tag=='ENG':return me
            raise KeyError(tag)
        tags=['GER',None,'ENG','RAJ','---','CAN']
        rels=[0x2000,0x2100,0x2200,0x2300]
        values={0x1000+combat.CO_DIPLOMACY_STATUS:0x1500}
        values.update({q:0x900 for q in rels})
        ids={0x200c:2,0x2010:3,0x210c:2,0x2110:5,0x220c:0,0x2210:2,0x230c:2,0x2310:1}
        g=types.SimpleNamespace(base=0x800,_tags=tags,country=country,player_tag=lambda:'ENG',
                                tags=lambda:[t for t in tags if t and t!='---'],
                                p=types.SimpleNamespace(u64=lambda p:values[p],u16=lambda p:ids[p]))
        with patch.object(combat,'_SUBJ_VT',0x100),patch.object(game,'_arr',return_value=rels):
            self.assertEqual(combat.subject_tags(g),['CAN','RAJ'])
        self.assertEqual(looked,['ENG'])

class ObjectResolutionTests(unittest.TestCase):
    def index(self):
        objindex=module('objindex')
        class RTTI(dict):
            scans=0
            def __iter__(self):
                self.scans+=1
                return super().__iter__()
        rtti=RTTI({'4CFoo':[0x10],'4CBar':[0x20]})
        proc=types.SimpleNamespace(va=lambda x:x,try_read=lambda a,n:struct.pack('<Q',0x10))
        idx=objindex.ObjectIndex(proc,rtti)
        idx._byvptr={0x10:[0x100,0x200]}
        return idx,rtti

    def test_repeated_lookup_does_not_rescan_rtti_names(self):
        idx,rtti=self.index()
        for _ in range(3):self.assertEqual(idx.instances('CFoo'),[0x100,0x200])
        self.assertLessEqual(rtti.scans,1)
        self.assertEqual(idx.instances('4CFoo'),[0x100,0x200])
        self.assertEqual(idx.instances('Missing'),[])

    def test_class_name_cache_does_not_cache_stale_object_addresses(self):
        idx,_=self.index()
        self.assertEqual(idx.instances('CFoo'),[0x100,0x200])
        idx._byvptr={0x10:[0x300]}
        self.assertEqual(idx.instances('CFoo'),[0x300])
        rebuild=[]
        idx.p.try_read=lambda a,n:struct.pack('<Q',0x20 if a==0x300 else 0x10)
        def build(force=False):
            if force:
                rebuild.append(True)
                idx._byvptr={0x10:[0x400]}
            return idx._byvptr
        idx.build=build
        self.assertEqual(idx.instances('CFoo'),[0x400])
        self.assertEqual(rebuild,[True])

if __name__=='__main__':unittest.main()
