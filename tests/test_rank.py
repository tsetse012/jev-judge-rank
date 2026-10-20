"""Tests for scripts/jev_rank.py. No network: urllib.request.urlopen is replaced with a fake Jev API."""
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
import jev_rank  # noqa: E402
import jevlib  # noqa: E402

# name -> (novelty, usefulness, feasibility, pitch_mechanism); scores are expected level indexes 0..4
TABLE = {
    'Alpha': (4, 4, 4, 0.9),
    'Beta': (4, 4, 4, 0.9),       # same answers as Alpha but has_demo 0 -> field penalty
    'Gamma': (2, 2, 2, 0.2),      # pitch_mechanism below 0.4 -> Noul penalty
    'Delta': (2, 2, 2, 0.9),      # ties with Epsilon; fewer votes
    'Epsilon': (2, 2, 2, 0.9),
    'Zeta': (0, 0, 4, 0.9),       # only feasibility (weight 0.7)
}
ITEMS = [
    {'id': 'A', 'name': 'Alpha', 'pitch': 'pa', 'has_demo': 1, 'votes': 5},
    {'id': 'B', 'name': 'Beta', 'pitch': 'pb', 'has_demo': 0, 'votes': 3},
    {'id': 'G', 'name': 'Gamma', 'pitch': 'pg', 'has_demo': 1, 'votes': 9},
    {'id': 'D', 'name': 'Delta', 'pitch': 'pd', 'has_demo': 1, 'votes': 1},
    {'id': 'E', 'name': 'Epsilon', 'pitch': 'pe', 'has_demo': 1, 'votes': 7},
    {'id': 'Z', 'name': 'Zeta', 'pitch': 'pz', 'has_demo': 1, 'votes': 2},
]
LEVELS = ['l0', 'l1', 'l2', 'l3', 'l4']


def config():
    return {
        'state_fields': ['name', 'pitch'],
        'tie_break_field': 'votes',
        'top': 3,
        'report_fields': ['pitch'],
        'requests': [
            {'name': 'main', 'questions': {
                'novelty': {'type': 'score', 'weight': 1.0, 'instructions': 'novelty?', 'levels': LEVELS},
                'usefulness': {'type': 'score', 'weight': 1.0, 'instructions': 'useful?', 'levels': LEVELS},
                'feasibility': {'type': 'score', 'weight': 0.7, 'instructions': 'feasible?', 'levels': LEVELS}}},
            {'name': 'check', 'state_fields': ['name', 'pitch'], 'questions': {
                'pitch_mechanism': {'type': 'noul', 'role': 'penalty', 'below': 0.4, 'penalty': 0.1,
                                    'instructions': '`pitch` states a mechanism'}}}],
        'field_penalties': [{'field': 'has_demo', 'below': 1, 'penalty': 0.05}],
    }


class FakeJev:
    """Answers each question from TABLE by the item's name. fail_names -> HTTP 400, status -> that HTTP error always."""

    def __init__(self, fail_names=(), status=None, on_call=None):
        self.bodies = []
        self.fail_names = set(fail_names)
        self.status = status
        self.on_call = on_call
        self.lock = threading.Lock()

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        with self.lock:
            self.bodies.append(body)
            n = len(self.bodies)
        if self.on_call:
            self.on_call(n)
        if self.status:
            raise urllib.error.HTTPError(req.full_url, self.status, 'error', {}, io.BytesIO(b'{}'))
        item = body['state'].get('item', body['state'])
        if item['name'] in self.fail_names:
            raise urllib.error.HTTPError(req.full_url, 400, 'error', {}, io.BytesIO(b'{"error": "bad"}'))
        nov, use, fea, mech = TABLE[item['name']]
        values = {'novelty': nov, 'usefulness': use, 'feasibility': fea}
        answers = {}
        for k, q in body['questions'].items():
            if q['type'] == 'score':
                answers[k] = {'type': 'score', 'score': values.get(k, 2), 'confidence': 0.8}
            elif q['type'] == 'noul':
                answers[k] = {'type': 'noul', 'noul': mech}
            else:
                answers[k] = {'type': 'choice', 'choice': sorted(q['criteria'])[0], 'confidence': 0.9}
        return io.BytesIO(json.dumps({'model': 'jev-1.13.0', 'answers': answers,
                                      'usage': {'input_tokens': 50, 'output_tokens': 5}}).encode())


class RankTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / 'out'
        (self.dir / 'items.json').write_text(json.dumps(ITEMS))
        self.write_config(config())
        env = mock.patch.dict(os.environ, {'TYPESAFE_API_KEY': 'test-key'})
        env.start()
        self.addCleanup(env.stop)

    def write_config(self, cfg):
        (self.dir / 'axes.json').write_text(json.dumps(cfg))

    def run_rank(self, fake, *extra):
        argv = ['--items', str(self.dir / 'items.json'), '--config', str(self.dir / 'axes.json'),
                '--out', str(self.out)] + list(extra)
        with mock.patch('urllib.request.urlopen', fake), redirect_stdout(io.StringIO()):
            return jev_rank.main(argv)

    def rank_rows(self):
        return json.loads((self.out / 'rank.json').read_text())

    def test_combined_score_penalties_and_tie_break(self):
        self.assertEqual(self.run_rank(FakeJev()), 0)
        rows = self.rank_rows()
        self.assertEqual([r['id'] for r in rows], ['A', 'B', 'E', 'D', 'G', 'Z'])
        combined = {r['id']: r['combined'] for r in rows}
        self.assertEqual(combined['A'], 1.0)
        self.assertEqual(combined['B'], 0.95)                 # field penalty: has_demo 0 < 1
        self.assertEqual(combined['E'], 0.5)
        self.assertEqual(combined['D'], 0.5)                  # tie with E; E has more votes
        self.assertEqual(combined['G'], 0.4)                  # Noul penalty: 0.2 < 0.4
        self.assertEqual(combined['Z'], round(0.7 / 2.7, 3))  # weight 0.7 on feasibility only
        self.assertEqual([r['rank'] for r in rows], [1, 2, 3, 4, 5, 6])
        self.assertIn('novelty', rows[0]['strengths'])

    def test_one_item_per_request_and_only_state_fields_sent(self):
        fake = FakeJev()
        self.run_rank(fake)
        self.assertEqual(len(fake.bodies), len(ITEMS) * 2)
        for body in fake.bodies:
            self.assertEqual(set(body['state']), {'name', 'pitch'})
            self.assertEqual(body['model'], jevlib.DEFAULT_MODEL)
        questions = sorted(tuple(sorted(b['questions'])) for b in fake.bodies)
        self.assertEqual(questions.count(('feasibility', 'novelty', 'usefulness')), len(ITEMS))
        self.assertEqual(questions.count(('pitch_mechanism',)), len(ITEMS))

    def test_context_goes_only_to_its_own_request(self):
        rows = [{'id': 'X1', 'title': 'first', 'secret': 's1'}, {'id': 'X2', 'title': 'second', 'secret': 's2'}]
        (self.dir / 'ctx.json').write_text(json.dumps(rows))
        cfg = config()
        cfg['requests'].append({'name': 'fit', 'context_file': 'ctx.json', 'context_ids': ['X2'],
                                'context_fields': ['id', 'title'], 'context_key': 'targets', 'item_key': 'item',
                                'questions': {'fit': {'type': 'score', 'role': 'info', 'instructions': 'fit?',
                                                      'levels': LEVELS}}})
        self.write_config(cfg)
        fake = FakeJev()
        self.assertEqual(self.run_rank(fake), 0)
        with_ctx = [b for b in fake.bodies if 'targets' in b['state']]
        self.assertEqual(len(with_ctx), len(ITEMS))
        for b in with_ctx:
            self.assertEqual(b['state']['targets'], [{'id': 'X2', 'title': 'second'}])
            self.assertEqual(set(b['state']['item']), {'name', 'pitch'})
            self.assertEqual(list(b['questions']), ['fit'])
        self.assertEqual(sum(1 for b in fake.bodies if 'targets' not in b['state']), len(ITEMS) * 2)
        self.assertEqual(self.rank_rows()[0]['combined'], 1.0)   # role "info" is recorded, not combined
        self.assertIn('fit', self.rank_rows()[0])

    def test_resume_skips_cached_items(self):
        self.run_rank(FakeJev())
        first = self.rank_rows()
        again = FakeJev()
        self.assertEqual(self.run_rank(again), 0)
        self.assertEqual(again.bodies, [])
        self.assertEqual(self.rank_rows(), first)

    def test_changed_config_stops_until_reset(self):
        self.run_rank(FakeJev())
        cfg = config()
        cfg['requests'][0]['questions']['novelty']['instructions'] = 'a different question'
        self.write_config(cfg)
        fake = FakeJev()
        with self.assertRaises(SystemExit) as cm:
            self.run_rank(fake)
        self.assertIn('config changed', str(cm.exception))
        self.assertEqual(fake.bodies, [])
        self.assertEqual(self.run_rank(fake, '--reset'), 0)
        self.assertEqual(len(fake.bodies), len(ITEMS) * 2)

    def test_failed_item_exits_2_and_is_listed(self):
        self.assertEqual(self.run_rank(FakeJev(fail_names={'Gamma'})), 2)
        self.assertNotIn('G', [r['id'] for r in self.rank_rows()])
        self.assertIn("Missing 1 item(s)", (self.out / 'report.txt').read_text())
        self.assertIn("['G']", (self.out / 'report.txt').read_text())

    def test_stop_file_keeps_finished_items_for_the_rerun(self):
        def stop_after(n):
            if n == 3:
                (self.out / 'STOP').write_text('')
        with self.assertRaises(SystemExit):
            self.run_rank(FakeJev(on_call=stop_after), '--workers', '1')
        cached = json.loads((self.out / 'judged.json').read_text())
        self.assertEqual([x['id'] for x in cached], ['A'])
        self.assertFalse((self.out / 'rank.json').exists())
        (self.out / 'STOP').unlink()
        rerun = FakeJev()
        self.assertEqual(self.run_rank(rerun), 0)
        self.assertEqual(len(rerun.bodies), (len(ITEMS) - 1) * 2)
        self.assertNotIn('Alpha', [b['state']['name'] for b in rerun.bodies])

    def test_http_402_stops_with_billing_message(self):
        fake = FakeJev(status=402)
        with self.assertRaises(SystemExit) as cm:
            self.run_rank(fake, '--workers', '1')
        self.assertIn('console.typesafe.ai/settings/billing', str(cm.exception))
        self.assertEqual(len(fake.bodies), 1)
        self.assertFalse((self.out / 'rank.json').exists())

    def test_check_and_report(self):
        self.assertEqual(self.run_rank(FakeJev(), '--check', 'novelty:votes', '--title', 'Test rank'), 0)
        report = (self.out / 'report.txt').read_text()
        self.assertTrue(report.startswith('Test rank ('))
        self.assertIn('novelty vs votes rank correlation', report)
        self.assertIn('Part 1. Top 3 overall', report)
        self.assertIn('#1 [A] Alpha', report)
        self.assertIn('  pitch: pa', report)
        self.assertNotIn('#4 [D] Delta', report)                # top = 3
        self.assertIn('minus 0.05 if field has_demo < 1', report)

    def test_bad_arguments_fail_before_any_call(self):
        fake = FakeJev()
        with self.assertRaises(SystemExit):
            self.run_rank(fake, '--check', 'novelty')
        with self.assertRaises(SystemExit):
            self.run_rank(fake, '--check', 'unknown:votes')
        cfg = config()
        cfg['requests'][1]['questions']['pitch_mechanism']['type'] = 'choice'
        cfg['requests'][1]['questions']['pitch_mechanism']['criteria'] = {'yes': 'y', 'no': 'n'}
        self.write_config(cfg)
        with self.assertRaises(SystemExit):
            self.run_rank(fake)
        self.assertEqual(fake.bodies, [])

    def test_missing_key_stops_before_any_call(self):
        fake = FakeJev()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TYPESAFE_API_KEY', None)
            with self.assertRaises(SystemExit) as cm:
                self.run_rank(fake)
        self.assertIn('TYPESAFE_API_KEY', str(cm.exception))
        self.assertEqual(fake.bodies, [])


if __name__ == '__main__':
    unittest.main()
