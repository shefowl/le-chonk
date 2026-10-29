"""Construction cancellation uses the game's counted-handle command, offline only."""
import struct
import types
import unittest
from unittest.mock import patch

from harness import economy
from harness.bridge import module

G = module("game")
C = module("commands")


class Memory:
    base = 0x10000000

    def __init__(self):
        self.data = {}
        self.next = 0x100000
        self.freed_objects = set()

    def write(self, addr, blob):
        self.data.update((addr + i, b) for i, b in enumerate(blob))

    def read(self, addr, size):
        if any(ptr <= addr < ptr + 0x100 for ptr in self.freed_objects):
            raise AssertionError("dereferenced a removed construction line")
        return bytes(self.data.get(addr + i, 0) for i in range(size))

    def put(self, addr, fmt, *values):
        self.write(addr, struct.pack(fmt, *values))

    def u64(self, addr):
        return struct.unpack('<Q', self.read(addr, 8))[0]

    def i32(self, addr):
        return struct.unpack('<i', self.read(addr, 4))[0]

    def i64(self, addr):
        return struct.unpack('<q', self.read(addr, 8))[0]


class Fixture:
    def __init__(self, levels=(4,), started=(), effect=True, allowed=True):
        self.p = Memory()
        self.lines = [0x5000 + i * 0x200 for i in range(len(levels))]
        self.posts, self.calls, self.freed = [], [], []
        self.effect, self.allowed = effect, allowed
        self.p.put(0x1000 + G.CO_PRODUCTION, '<Q', 0x3000)
        for i, (ptr, count) in enumerate(zip(self.lines, levels)):
            self.p.put(ptr + 8, '<II', 59, 100 + i)
            self.p.put(ptr + G.BL_LEVELS, '<i', count)
            self.p.put(ptr + G.BL_DONE, '<q', 1 if i in started else 0)
        self.sync()
        self.p.put(self.p.base + 0x4414B78 + 80, '<Q', self.p.base + 0x1CDFEF0)
        self.g = types.SimpleNamespace(p=self.p, _inj=object(),
            _c=lambda tag: types.SimpleNamespace(ptr=0x1000),
            oi=types.SimpleNamespace(class_of=lambda ptr: 'CBuildingProductionLine'),
            handle_of=self.handle, call=self.call, free=self.freed.append,
            va=lambda off: self.p.base + off, _settle=lambda delay=.25: None,
            send_command=lambda *args: None)
        self.g.cmd = C.CommandLayer(self.g)
        self.g.cmd.post = self.post
        self.g.remove_construction = lambda index: G.Game.remove_construction(self.g, index)
        self.g.construction_queue = self.queue

    def sync(self):
        self.p.put(0x3000 + G.PS_CONSTRUCTION, '<QiiQ', 0x4000, len(self.lines), len(self.lines), 0)
        for i, ptr in enumerate(self.lines):
            self.p.put(0x4000 + i * 8, '<Q', ptr)

    def handle(self, ptr):
        ty, oid = struct.unpack('<II', self.p.read(ptr + 8, 8))
        return {'type': ty, 'id': oid}

    def call(self, fn, args):
        self.calls.append((fn, args))
        if fn == C.FN_NEW:
            ptr = self.p.next
            self.p.next += args[0] + 0x100
            return ptr
        if fn == 0x1CDFEF0:
            return int(self.allowed)
        raise AssertionError('unexpected native call %#x' % fn)

    def post(self, cmd):
        blob = self.p.read(cmd, 0x30)
        self.posts.append(blob)
        if not self.effect:
            return
        ty, oid, count = struct.unpack_from('<IIi', blob, 0x24)
        for ptr in list(self.lines):
            if self.handle(ptr) == {'type': ty, 'id': oid}:
                left = self.p.i32(ptr + G.BL_LEVELS) - count
                if left <= 0:
                    self.lines.remove(ptr)
                    self.p.freed_objects.add(ptr)
                else:
                    self.p.put(ptr + G.BL_LEVELS, '<i', left)
                self.sync()
                return

    def queue(self):
        return [{'index': i, 'building': 'industrial_complex',
                 'levels': self.p.i32(ptr + G.BL_LEVELS),
                 'done': self.p.i64(ptr + G.BL_DONE)} for i, ptr in enumerate(self.lines)]


class ConstructionCancellationTests(unittest.TestCase):
    def test_counted_handle_removes_all_four_levels_and_checks_before_post(self):
        f = Fixture()
        result = f.g.remove_construction(0)
        self.assertTrue(result['ok'])
        self.assertEqual(result.get('cancelled_levels'), 4)
        self.assertEqual(struct.unpack_from('<IIi', f.posts[0], 0x24), (59, 100, 4))
        self.assertEqual(f.calls[-1][0], 0x1CDFEF0)
        self.assertNotIn('_removed', vars(f.g))

    def test_unchanged_queue_is_not_reported_cancelled_or_cached_as_removed(self):
        f = Fixture(effect=False)
        result = f.g.remove_construction(0)
        self.assertFalse(result['ok'])
        self.assertTrue(result.get('posted'))
        self.assertEqual(result.get('cancelled_levels'), 0)
        self.assertIn('unconfirmed', result.get('error', ''))
        self.assertEqual(len(f.posts), 1)
        self.assertNotIn('_removed', vars(f.g))

    def test_started_project_is_never_posted(self):
        f = Fixture(started=(0,))
        result = f.g.remove_construction(0)
        self.assertFalse(result['ok'])
        self.assertEqual(f.posts, [])
        self.assertEqual(f.calls, [])

    def test_zero_levels_is_never_posted(self):
        f = Fixture(levels=(0,))
        result = f.g.remove_construction(0)
        self.assertFalse(result['ok'])
        self.assertEqual(f.posts, [])
        self.assertEqual(f.calls, [])

    def test_false_canexecute_frees_nonposted_allocation(self):
        f = Fixture(allowed=False)
        result = f.g.remove_construction(0)
        self.assertFalse(result['ok'])
        self.assertEqual(f.posts, [])
        self.assertEqual(f.freed, [0x100000])

    def test_unknown_canexecute_address_is_never_called(self):
        f = Fixture()
        f.p.put(f.p.base + 0x4414B78 + 80, '<Q', f.p.base + 0x1CDF981)
        result = f.g.remove_construction(0)
        self.assertFalse(result['ok'])
        self.assertEqual(f.posts, [])
        self.assertTrue(all(fn == C.FN_NEW for fn, _ in f.calls))
        self.assertEqual(f.freed, [0x100000])

    def test_corrupted_command_is_freed_without_canexecute_or_post(self):
        f = Fixture()
        write = f.p.write
        def corrupt(addr, blob):
            if len(blob) == 0x30:
                blob = blob[:0x2C] + b'\0' * 4
            write(addr, blob)
        f.p.write = corrupt
        result = f.g.remove_construction(0)
        self.assertFalse(result['ok'])
        self.assertEqual(f.posts, [])
        self.assertTrue(all(fn == C.FN_NEW for fn, _ in f.calls))
        self.assertEqual(f.freed, [0x100000])

    def test_uncertain_post_is_not_freed_or_retried(self):
        f = Fixture()
        def uncertain(cmd):
            f.post(cmd)
            raise OSError('injected post outcome uncertain')
        f.g.cmd.post = uncertain
        result = f.g.remove_construction(0)
        self.assertFalse(result['ok'])
        self.assertTrue(result.get('pending'))
        self.assertEqual(result.get('cancelled_levels'), 0)
        self.assertEqual(f.freed, [])
        self.assertEqual(len(f.posts), 1)

    def test_dry_run_checks_counted_command_and_frees_without_post(self):
        f = Fixture()
        result = G.Game.remove_construction(f.g, 0, dry_run=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['requested_levels'], 4)
        self.assertFalse(result['posted'])
        self.assertEqual(result['cancelled_levels'], 0)
        self.assertEqual(f.posts, [])
        self.assertEqual(f.freed, [0x100000])

    def test_partial_readback_reports_only_two_confirmed_levels(self):
        f = Fixture()
        def partial(cmd):
            f.posts.append(f.p.read(cmd, 0x30))
            f.p.put(f.lines[0] + G.BL_LEVELS, '<i', 2)
        f.g.cmd.post = partial
        result = economy.cancel_construction(f.g, 'industrial_complex', 20)
        self.assertEqual(result['cancelled'], 2)
        self.assertTrue(result['pending'])

    def test_canexecute_must_not_discard_work_that_started_after_initial_read(self):
        f = Fixture()
        call = f.g.call
        def start(fn, args):
            result = call(fn, args)
            if fn == 0x1CDFEF0:
                f.p.put(f.lines[0] + G.BL_DONE, '<q', 10)
            return result
        f.g.call = start
        result = f.g.remove_construction(0)
        self.assertFalse(result['ok'])
        self.assertEqual(f.posts, [])
        self.assertEqual(f.freed, [0x100000])

    def test_row_removed_during_attach_is_never_dereferenced_or_posted(self):
        f = Fixture()
        f.g._inj = None
        def attach():
            f.g._inj = object()
            ptr = f.lines.pop()
            f.p.freed_objects.add(ptr)
            f.sync()
        f.g.attach, f.g.detach = attach, lambda: None
        result = f.g.remove_construction(0)
        self.assertFalse(result['ok'])
        self.assertEqual(f.posts, [])
        self.assertEqual(f.calls, [])

    def test_tail_four_plus_four_plus_one_confirms_nine_and_keeps_started_row(self):
        f = Fixture(levels=(16, 4, 4, 1), started=(0,))
        result = economy.cancel_construction(f.g, 'industrial_complex', 20)
        self.assertEqual(result['cancelled'], 9)
        self.assertEqual(len(f.posts), 3)
        self.assertEqual(f.queue(), [{'index': 0, 'building': 'industrial_complex',
                                     'levels': 16, 'done': 1}])

    def test_economy_does_not_count_a_post_without_readback_effect(self):
        f = Fixture(levels=(4, 4, 1), effect=False)
        result = economy.cancel_construction(f.g, 'industrial_complex', 20)
        self.assertEqual(result['cancelled'], 0)
        self.assertTrue(result.get('pending'))
        self.assertIn('unconfirmed', result.get('error', ''))


if __name__ == '__main__':
    unittest.main()
