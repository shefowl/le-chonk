"""Skip kernel zero pages without reading enormous virtual reservations."""
import ctypes
import errno
import mmap
import os
import unittest
from unittest.mock import patch
import types
from harness.bridge import module


class ScanRegionsTests(unittest.TestCase):
    def test_proc_uses_fresh_backend_ranges(self):
        mem=module('mem');p=mem.Proc.__new__(mem.Proc)
        p.maps=[]
        row=dict(start=0x1000,end=0x2000,perms='rw-p',path='')
        p.be=types.SimpleNamespace(scan_regions=lambda:[row])
        self.assertEqual(p.regions(),[row])

    def test_kernel_scan_preserves_real_anonymous_data_and_excludes_zero_pages(self):
        backend=module('process').backend
        scan=getattr(backend,'populated_ranges',None)
        self.assertTrue(callable(scan))
        size=512<<20
        mm=mmap.mmap(-1,size,flags=mmap.MAP_PRIVATE|mmap.MAP_ANONYMOUS)
        try:
            start=ctypes.addressof(ctypes.c_char.from_buffer(mm))
            mm[4096:4104]=b'live_obj'
            self.assertEqual(mm[128<<20],0) # install a shared kernel zero page
            with open('/proc/self/pagemap','rb',buffering=0) as f:
                try:ranges=scan(f.fileno(),start,start+size)
                except OSError as exc:
                    if exc.errno in (errno.ENOTTY,errno.EINVAL,errno.EACCES):
                        self.skipTest('kernel PAGEMAP_SCAN unavailable')
                    raise
            self.assertTrue(any(a<=start+4096<b for a,b in ranges))
            self.assertFalse(any(a<=start+(128<<20)<b for a,b in ranges))
            # A single write may allocate a 2 MiB transparent huge page.
            self.assertLess(sum(b-a for a,b in ranges),4<<20)
        finally:mm.close()

    def test_unsupported_kernel_preserves_full_scan_ranges(self):
        backend=module('process').backend
        p=backend.Backend.__new__(backend.Backend);p.pid=os.getpid()
        regions=[dict(start=0x100000000,end=0x140000000,perms='rw-p',path='')]
        self.assertTrue(callable(getattr(p,'scan_regions',None)))
        with patch.object(p,'_read_maps',return_value=regions),patch.object(backend,'populated_ranges',side_effect=OSError(errno.ENOTTY,'unsupported')):
            self.assertEqual(p.scan_regions(),regions)
