"""War warnings include active focus threats without invoking game functions."""
import struct
import types
import unittest
from unittest.mock import patch

from harness import threats


class Memory:
    base = 0x10000000

    def __init__(self):
        self.data = {}

    def put(self, address, fmt, *values):
        for i, byte in enumerate(struct.pack(fmt, *values)):
            self.data[address + i] = byte

    def read(self, address, size):
        try:
            return bytes(self.data[address + i] for i in range(size))
        except KeyError as exc:
            raise OSError("unreadable test memory") from exc

    def i32(self, address):
        return struct.unpack('<i', self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]


class ThreatTests(unittest.TestCase):
    def setUp(self):
        self.p = p = Memory()
        self.countries = [types.SimpleNamespace(tag='SOV', tag_id=1, ptr=0x1000),
                          types.SimpleNamespace(tag='GER', tag_id=2, ptr=0x5000),
                          types.SimpleNamespace(tag='ITA', tag_id=3, ptr=0x9000)]
        self.manual = {'FRA': ['ITA']}
        self.g = types.SimpleNamespace(p=p, player_tag=lambda: 'SOV',
                                       countries=lambda: iter(self.countries),
                                       current_focus=lambda tag: {'key': 'GER_war', 'name': 'War'})
        for i, c in enumerate(self.countries):
            p.put(c.ptr + 0x484, '<i', 1)
            p.put(c.ptr + 0xf60, '<Q', 0x20000 + i * 0x1000)
            p.put(0x20000 + i * 0x1000 + 0xd4, '<i', 0)
            p.put(c.ptr + 0x1348, '<Q', 0)
        p.put(p.base + 0x458a088, '<Q', 0x30000)
        p.put(0x30000 + 0x30, '<Q', 0x31000)
        p.put(0x30000 + 0x3c, '<i', 4)
        p.put(0x31000, '<4i', 0, 1, 2, 3)

    def focus(self, descriptors):
        self.p.put(0x5000 + 0x1348, '<Q', 0x40000)
        self.p.put(0x40000 + 0x10, '<Q', 0x41000)
        self.p.put(0x41000 + 0x5c0, '<Q', 0x42000)
        self.p.put(0x41000 + 0x5cc, '<i', len(descriptors))
        for i, (tag, script) in enumerate(descriptors):
            self.p.put(0x42000 + 16 * i, '<i4xQ', tag, script)

    def snapshot(self):
        dip = types.SimpleNamespace(justifications=lambda g: self.manual)
        with patch.object(threats.B, 'module', return_value=dip):
            return threats.snapshot(self.g)

    def test_focus_warning_exists_with_empty_manual_justifications(self):
        self.manual = {}
        self.focus([(1, 0)])
        result = self.snapshot()
        self.assertEqual(result['pairs'], [('GER', 'SOV')])
        self.assertEqual(result['threats'][0]['source'], 'focus')
        self.assertEqual(result['threats'][0]['focus_key'], 'GER_war')
        self.assertIsNone(result['threats'][0]['days_left'])
        self.assertIn('GER', '\n'.join(threats.lines(result, 'SOV')))
        self.assertEqual(result['coverage_errors'], [])

    def test_remaps_definition_tag_and_keeps_unrelated_manual_pairs(self):
        self.p.put(0x31000 + 3 * 4, '<i', 1)
        self.focus([(3, 0)])
        result = self.snapshot()
        self.assertEqual(result['pairs'], [('GER', 'SOV'), ('ITA', 'FRA')])
        self.assertIn('ITA', '\n'.join(threats.lines(result, 'SOV', enemy='ITA')))

    def test_other_focus_targets_are_not_exposed(self):
        self.focus([(3, 0)])
        result = self.snapshot()
        self.assertEqual(result['pairs'], [('ITA', 'FRA')])
        self.assertEqual(threats.lines(result, 'SOV'), [])

    def test_scripted_target_does_not_claim_incoming_war(self):
        self.focus([(1, 0xdeadbeef)])
        result = self.snapshot()
        self.assertEqual(result['pairs'], [('ITA', 'FRA')])
        self.assertEqual(result['unresolved'][0]['kind'], 'scripted')
        self.assertTrue(result['coverage_errors'])
        text = '\n'.join(threats.lines(result, 'SOV'))
        self.assertIn('COVERAGE', text)
        self.assertNotIn('GER is', text)

    def test_bad_memory_reports_partial_coverage_and_keeps_known_threats(self):
        self.manual = {'SOV': ['ITA']}
        self.p.put(0x5000 + 0x1348, '<Q', 0xbad000)
        result = self.snapshot()
        self.assertEqual(result['pairs'], [('ITA', 'SOV')])
        self.assertTrue(result['coverage_errors'])
        self.assertIn('ITA', '\n'.join(threats.lines(result, 'SOV')))

    def test_swallowed_manual_read_error_does_not_become_no_threats(self):
        self.manual = {}
        self.p.put(0x21000 + 0xd4, '<i', 1)
        self.p.put(0x21000 + 0xc8, '<Q', 0xbad000)
        result = self.snapshot()
        self.assertTrue(result['coverage_errors'])
        self.assertIn('COVERAGE', '\n'.join(threats.lines(result, 'SOV')))

    def test_persistent_lines_repeat_until_threat_disappears(self):
        self.manual = {'SOV': ['GER']}
        self.assertEqual(threats.lines(self.snapshot(), 'SOV'),
                         threats.lines(self.snapshot(), 'SOV'))
        self.assertIn('GER', '\n'.join(threats.lines(self.snapshot(), 'SOV')))
        self.manual = {}
        self.assertEqual(threats.lines(self.snapshot(), 'SOV'), [])

    def test_invalid_target_count_and_descriptor_are_not_silently_empty(self):
        self.focus([(400, 0)])
        result = self.snapshot()
        self.assertTrue(result['coverage_errors'])
        self.p.put(0x41000 + 0x5cc, '<i', -1)
        self.assertTrue(self.snapshot()['coverage_errors'])

    def test_bad_second_descriptor_preserves_confirmed_first_target(self):
        self.focus([(1, 0), (400, 0)])
        result = self.snapshot()
        self.assertIn(('GER', 'SOV'), result['pairs'])
        self.assertTrue(result['coverage_errors'])

    def test_focus_name_read_failure_preserves_confirmed_target(self):
        self.focus([(1, 0)])
        def unreadable(tag):
            raise OSError('focus label unreadable')
        self.g.current_focus = unreadable
        result = self.snapshot()
        self.assertIn(('GER', 'SOV'), result['pairs'])
        self.assertTrue(result['coverage_errors'])
