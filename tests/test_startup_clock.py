"""Startup clock discovery on synthetic memory, with no live commands."""
import struct
import types
import threading
import unittest
from unittest.mock import patch
from harness import bridge


class StartupClockTests(unittest.TestCase):
    def test_unanchored_moving_counter_is_refused(self):
        api = bridge.module('api')
        state = {'value': api.parts_to_date(1937, 11, 20)}
        g = api.HOI4.__new__(api.HOI4)
        g.p = types.SimpleNamespace(regions=lambda **kw: [{'start': 100, 'end':104}],
            try_read=lambda a, n: struct.pack('<i', state['value']) if a == 100 else bytes(n))
        g._save_date_cache = lambda: self.fail('unanchored clock cached')
        for ref in (None, api.parts_to_date(1941, 6, 12)):
            g._log_date = lambda: ref
            with self.assertRaisesRegex(RuntimeError, 'no moving clock anchored'):
                g.find_live_date_address(advance=lambda: state.update(value=state['value']+1))

    def test_many_unrelated_movers_do_not_outvote_the_moving_clock_signature(self):
        api = bridge.module('api')
        old, live = api.parts_to_date(1937, 11, 20), api.parts_to_date(1941, 6, 12)
        buf = bytearray(64)
        for off in (0, 4, 8, 12): struct.pack_into('<i', buf, off, old)
        struct.pack_into('<I', buf, 24, 0x3D2AAAAB)
        struct.pack_into('<i', buf, 32, live)
        g = api.HOI4.__new__(api.HOI4)
        g.p = types.SimpleNamespace(regions=lambda **kw: [{'start': 100, 'end':164}],
            try_read=lambda a, n: bytes(buf[a-100:a-100+n]) if a >= 100 else bytes(n))
        g._save_date_cache = lambda: None
        g._log_date = lambda: None
        def tick():
            for off in (0, 4, 8, 12, 32):
                struct.pack_into('<i', buf, off, struct.unpack_from('<i', buf, off)[0]+1)
        self.assertEqual(g.find_live_date_address(advance=tick), 132)
        # Live 1.19.4 can lack the old 1/24 signature. The current public log
        # still excludes the larger collection of unrelated 1937 counters.
        struct.pack_into('<I', buf, 24, 0)
        g._log_date = lambda: live
        self.assertEqual(g.find_live_date_address(advance=tick), 132)

    def test_resume_fallback_uses_the_same_bounded_calibration_as_startup(self):
        from harness.run import Runner
        state = {'raw': 100}
        g = types.SimpleNamespace(date_raw=lambda: state['raw'],
            find_date_address=lambda **kw: 10, _date_addr=10,
            p=types.SimpleNamespace(i32=lambda a: state['raw']))
        r = Runner.__new__(Runner)
        r.args = types.SimpleNamespace(rewound=False)
        r.gw = types.SimpleNamespace(run=lambda fn: fn(g))
        def bounded(game):
            state['raw'] = 200
        # The old fallback tries an unrestricted unpause. It must never be reached.
        sess = types.SimpleNamespace(is_paused=lambda g: True,
            pause=lambda *a: self.fail('unbounded resume fallback'))
        with patch.object(bridge.Gateway, '_calibrate_clock', side_effect=bounded,
                          create=True) as calibrate, \
                patch('harness.run.B_module', return_value=sess):
            r._sane_clock(200)
        calibrate.assert_called_once_with(g)

    def test_stuck_clock_recovery_scans_paused_and_resumes_only_after_validation(self):
        from harness.run import Runner
        state = {'raw': 190}
        g = types.SimpleNamespace(date_raw=lambda: state['raw'])
        r = Runner.__new__(Runner)
        r._hold_lock = threading.Lock()
        r._holds, r.stopping = 0, False
        r.gw = types.SimpleNamespace(run=lambda fn: fn(g))
        resumes = []
        r.run_clock = lambda: resumes.append('resumed')
        sess = types.SimpleNamespace(is_paused=lambda g: False)
        def bounded(game): state['raw'] = 200
        with patch.object(bridge.Gateway, '_calibrate_clock', side_effect=bounded,
                          create=True) as calibrate, \
                patch('harness.run.B_module', return_value=sess):
            r.check_clock([190, 0])
        calibrate.assert_called_once_with(g)
        self.assertEqual(resumes, ['resumed'])

    def test_discovery_scans_while_paused_then_uses_bounded_tick(self):
        api = bridge.module('api')
        value = api.parts_to_date(1936, 1, 1, 11)
        state = {'paused': True, 'value': value}
        scans = []
        def read(a, n):
            if n > 4:
                scans.append(state['paused'])
            return struct.pack('<ii', state['value'], 0)[:n]
        g = api.HOI4.__new__(api.HOI4)
        g.p = types.SimpleNamespace(regions=lambda **kw: [{'start': 100, 'end':108}],
                                   try_read=read)
        g._save_date_cache = lambda: None
        g._log_date = lambda: value
        def tick():
            self.assertEqual(scans, [True])
            state['value'] += 1
        self.assertIn('advance', __import__('inspect').signature(g.find_live_date_address).parameters)
        self.assertEqual(g.find_live_date_address(advance=tick), 100)

    def test_gateway_bounds_tick_and_leaves_game_paused(self):
        state = {'paused': True, 'timer': 0}
        events = []
        def pause(g, want):
            events.append(('pause', want)); state['paused'] = want
            if not want:
                self.assertEqual(state['timer'], 1)
                state['paused'] = True  # game's one-hour timer fires
            return True, ''
        sess = types.SimpleNamespace(is_paused=lambda g: state['paused'], pause=pause)
        def discover(**kw):
            self.assertTrue(state['paused'])
            self.assertIn('advance', kw)
            kw['advance']()
            self.assertTrue(state['paused'])
        def timer(hours):
            # The console command starts and auto-pauses itself. Its SResult.ok is
            # false even on the successful path in the 1.19.4 handler.
            state['timer'] = 0
            return types.SimpleNamespace(ok=False)
        g = types.SimpleNamespace(set_game_speed=lambda s:events.append(('speed', s)),
              pause_in_hours=timer, find_live_date_address=discover,
              oi=types.SimpleNamespace(build=lambda **kw:None), _load_tags=lambda:None)
        # The counter is read after posting the timer.
        type_g = type('Game', (), {'hours_until_pause':property(lambda self:state['timer'])})
        obj = type_g(); obj.__dict__.update(vars(g))
        with patch.object(bridge, 'module', side_effect=lambda n: sess if n=='session' else types.SimpleNamespace(Game=lambda pid:obj)):
            bridge.Gateway._open(1)
        self.assertTrue(state['paused'])
        self.assertNotIn(('speed', 1), events)
