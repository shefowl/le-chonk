"""Native read buffers must be freed promptly, independent of cyclic GC."""
import ctypes
import gc
import types
import unittest
import weakref
from unittest.mock import patch
from harness.bridge import module


class NativeBufferTests(unittest.TestCase):
    def test_read_does_not_retain_native_buffer_until_cyclic_gc(self):
        backend = module('process').backend
        proc = backend.Backend.__new__(backend.Backend); proc.pid = 1
        refs = []
        original = ctypes.create_string_buffer
        def allocate(*a):
            buf = original(*a); refs.append(weakref.ref(buf)); return buf
        was = gc.isenabled(); gc.disable()
        try:
            with patch.object(backend.ctypes, 'create_string_buffer', allocate), patch.object(backend.libc, 'process_vm_readv', return_value=1024):
                proc.read(123,1024)
            self.assertTrue(all(r() is None for r in refs), 'read buffer retained in a ctypes cycle')
        finally:
            if was: gc.enable()
            gc.collect()

    def test_write_does_not_retain_native_buffer_until_cyclic_gc(self):
        backend = module('process').backend
        proc = backend.Backend.__new__(backend.Backend); proc.pid = 1
        refs=[]; original=ctypes.create_string_buffer
        def allocate(*a):
            buf=original(*a); refs.append(weakref.ref(buf)); return buf
        was=gc.isenabled(); gc.disable()
        try:
            with patch.object(backend.ctypes,'create_string_buffer',allocate), patch.object(backend.libc,'process_vm_writev',return_value=4):
                proc.write(123,b'test')
            self.assertTrue(all(r() is None for r in refs), 'write buffer retained in a ctypes cycle')
        finally:
            if was:gc.enable()
            gc.collect()
