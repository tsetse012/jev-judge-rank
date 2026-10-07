"""Tests for scripts/jev_dedupe.py. No network: urllib.request.urlopen is replaced with a fake Jev API."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import jev_dedupe  # noqa: E402


class FakeJev:
    """Answers the "same" Noul from a table keyed by (title of a, title of b), in the order they were asked."""

    def __init__(self, table, default=0.05, fail_titles=()):
        self.table = table
        self.default = default
        self.fail_titles = set(fail_titles)
        self.bodies = []
        self.lock = threading.Lock()

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        with self.lock:
            self.bodies.append(body)
        a, b = body['state']['a']['title'], body['state']['b']['title']
        if {a, b} & self.fail_titles:
            raise urllib.error.HTTPError(req.full_url, 400, 'error', {}, io.BytesIO(b'{"error": "bad"}'))
        p = self.table.get((a, b), self.default)
        return io.BytesIO(json.dumps({'model': 'jev-1.13.0', 'answers': {'same': {'type': 'noul', 'noul': p}},
                                      'usage': {'input_tokens': 40, 'output_tokens': 5}}).encode())


def item(i, name, text, **extra):
    return dict({'id': i, 'name': name, 'what': text, 'how': 'detail ' + text}, **extra)


class DedupeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / 'out'
        env = mock.patch.dict(os.environ, {'TYPESAFE_API_KEY': 'test-key'})
        env.start()
        self.addCleanup(env.stop)

    def run_dedupe(self, items, fake, *extra, against=None):
        (self.dir / 'items.json').write_text(json.dumps(items))
        argv = ['--items', str(self.dir / 'items.json'), '--text-fields', 'what,how', '--out', str(self.out),
                '--min-sim', '0'] + list(extra)
        if against is not None:
            (self.dir / 'old.json').write_text(json.dumps(against))
            argv += ['--against', str(self.dir / 'old.json')]
        buf = io.StringIO()
        with mock.patch('urllib.request.urlopen', fake), redirect_stdout(buf):
            code = jev_dedupe.main(argv)
        self.stdout = buf.getvalue()
        return code

    def groups(self):
        return json.loads((self.out / 'groups.json').read_text())

    def test_each_pair_is_asked_twice_with_order_swapped_and_averaged(self):
        items = [item('1', 'Alpha', 'plant watering reminder'), item('2', 'Beta', 'plant watering alert')]
        fake = FakeJev({('Alpha', 'Beta'): 0.9, ('Beta', 'Alpha'): 0.7})
        self.assertEqual(self.run_dedupe(items, fake), 0)
        self.assertEqual(len(fake.bodies), 2)
        orders = sorted((b['state']['a']['title'], b['state']['b']['title']) for b in fake.bodies)
        self.assertEqual(orders, [('Alpha', 'Beta'), ('Beta', 'Alpha')])
        self.assertEqual(set(fake.bodies[0]['state']['a']), {'title', 'canDo', 'core'})
        self.assertEqual(fake.bodies[0]['state']['a']['canDo'], 'plant watering reminder')
        self.assertEqual(json.loads((self.out / 'pairs.json').read_text()), {'m:n:1~n:2': 0.8})
        self.assertEqual(self.groups()['reps'], [{'id': '1', 'title': 'Alpha',
                                                  'members': [{'id': '2', 'title': 'Beta', 'same': 0.8}]}])

    def test_threshold_is_inclusive(self):
        items = [item('1', 'Alpha', 'plant watering'), item('2', 'Beta', 'plant watering')]
        self.run_dedupe(items, FakeJev({('Alpha', 'Beta'): 0.6, ('Beta', 'Alpha'): 0.7}))
        self.assertEqual(len(self.groups()['reps']), 1)                      # average 0.65 >= 0.65
        self.run_dedupe(items, FakeJev({}), '--threshold', '0.66')           # cached answers, stricter threshold
        self.assertEqual(len(self.groups()['reps']), 2)

    def test_hub_attachment_does_not_chain(self):
        items = [item('1', 'A', 'plant care', votes=3), item('2', 'B', 'plant care', votes=2),
                 item('3', 'C', 'plant care', votes=1)]
        table = {('A', 'B'): 0.9, ('B', 'A'): 0.9, ('B', 'C'): 0.9, ('C', 'B'): 0.9, ('A', 'C'): 0.1, ('C', 'A'): 0.1}
        self.run_dedupe(items, FakeJev(table), '--order-field', 'votes')
        reps = self.groups()['reps']
        self.assertEqual([(r['id'], [m['id'] for m in r['members']]) for r in reps], [('1', ['2']), ('3', [])])

    def test_order_field_picks_the_representative(self):
        items = [item('1', 'Small', 'plant care', votes=1), item('2', 'Big', 'plant care', votes=50)]
        self.run_dedupe(items, FakeJev({}, default=0.9), '--order-field', 'votes')
        self.assertEqual(self.groups()['reps'][0]['id'], '2')

    def test_component_pairs_use_the_component_question(self):
        items = [item('1', 'GoSDK', 'client library', kind='sdk'), item('2', 'McpServer', 'client library', kind='app'),
                 item('3', 'Planter', 'client library', kind='app')]
        fake = FakeJev({}, default=0.9)
        self.run_dedupe(items, fake, '--component-field', 'kind', '--component-values', 'sdk',
                        '--q-component', 'COMPONENT QUESTION', '--q-same', 'SAME QUESTION')
        asked = {}
        for b in fake.bodies:
            pair = frozenset((b['state']['a']['title'], b['state']['b']['title']))
            asked[pair] = b['questions']['same']['instructions']
        self.assertEqual(asked[frozenset(('GoSDK', 'McpServer'))], 'COMPONENT QUESTION')
        self.assertEqual(asked[frozenset(('McpServer', 'Planter'))], 'SAME QUESTION')
        self.assertIn('w:n:1~n:2', json.loads((self.out / 'pairs.json').read_text()))

    def test_component_field_needs_values(self):
        with self.assertRaises(SystemExit):
            self.run_dedupe([item('1', 'A', 'x')], FakeJev({}), '--component-field', 'kind')

    def test_against_list_and_same_name_pairs(self):
        items = [item('N1', 'Alpha', 'completely different words'), item('N2', 'Gamma', 'unrelated')]
        old = [{'id': 'O1', 'name': 'Alpha (old)', 'what': 'nothing in common', 'how': ''}]
        fake = FakeJev({('Alpha', 'Alpha (old)'): 0.8, ('Alpha (old)', 'Alpha'): 0.8})
        self.run_dedupe(items, fake, '--k-new', '0', '--k-old', '0', against=old)
        g = self.groups()
        self.assertEqual(g['dup_against'], {'N1': {'against_id': 'O1', 'against_title': 'Alpha (old)', 'same': 0.8}})
        self.assertEqual([r['id'] for r in g['reps']], ['N2'])

    def test_resume_asks_nothing_twice(self):
        items = [item('1', 'Alpha', 'plant care'), item('2', 'Beta', 'plant care'), item('3', 'Gamma', 'plant care')]
        self.run_dedupe(items, FakeJev({}, default=0.2))
        first = self.groups()
        again = FakeJev({}, default=0.2)
        self.assertEqual(self.run_dedupe(items, again), 0)
        self.assertEqual(again.bodies, [])
        self.assertEqual(self.groups(), first)

    def test_failed_pair_exits_2_and_is_counted(self):
        items = [item('1', 'Alpha', 'plant care'), item('2', 'Beta', 'plant care'), item('3', 'Gamma', 'plant care')]
        self.assertEqual(self.run_dedupe(items, FakeJev({}, fail_titles={'Gamma'})), 2)
        self.assertEqual(self.groups()['pairs_missing'], 2)
        self.assertEqual(self.groups()['pairs_asked'], 1)

    def test_dry_run_needs_no_key_and_writes_nothing(self):
        items = [item('1', 'Alpha', 'plant care'), item('2', 'Beta', 'plant care')]
        fake = FakeJev({})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TYPESAFE_API_KEY', None)
            self.assertEqual(self.run_dedupe(items, fake, '--dry'), 0)
        self.assertIn('candidate pairs 1', self.stdout)
        self.assertEqual(fake.bodies, [])
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
