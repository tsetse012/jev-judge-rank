"""Tests for scripts/jev_recall.py. No network: urllib.request.urlopen is replaced with a fake Jev API."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import jev_recall  # noqa: E402

CORPUS = [
    {'id': 'F1', 'kind': 'family', 'title': 'Stop check', 'text': 'check the done claim against evidence', 'pointer': 'repo-one'},
    {'id': 'F2', 'kind': 'family', 'title': 'Tool gate', 'text': 'judge a shell command before it runs', 'pointer': 'repo-two'},
    {'id': 'F3', 'kind': 'family', 'title': 'Router', 'text': 'pick a model per request'},
    {'id': 'F4', 'kind': 'family', 'title': 'Pruning', 'text': 'drop old tool output'},
    {'id': 'I1', 'kind': 'idea', 'title': 'Done claim judge', 'text': 'a stop hook that checks the done claim', 'url': 'u1'},
    {'id': 'I2', 'kind': 'idea', 'title': 'Evidence checker', 'text': 'checks evidence for a claim'},
    {'id': 'I3', 'kind': 'idea', 'title': 'Recipe helper', 'text': 'suggests a recipe for dinner'},
    {'id': 'I4', 'kind': 'note', 'title': 'Gardening', 'text': 'water the plants on monday'},
]
P = {'Stop check': 0.92, 'Tool gate': 0.61, 'Router': 0.55, 'Pruning': 0.52, 'Done claim judge': 0.88,
     'Evidence checker': 0.45, 'Recipe helper': 0.05, 'Gardening': 0.02}
QUERY = 'a stop hook that checks whether the done claim has evidence'


class FakeJev:
    def __init__(self, fail_titles=()):
        self.bodies = []
        self.fail_titles = set(fail_titles)
        self.lock = threading.Lock()

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        with self.lock:
            self.bodies.append(body)
        title = body['state']['item']['title']
        if title in self.fail_titles:
            raise urllib.error.HTTPError(req.full_url, 400, 'error', {}, io.BytesIO(b'{"error": "bad"}'))
        return io.BytesIO(json.dumps({'model': 'jev-1.13.0', 'answers': {'useful': {'type': 'noul', 'noul': P[title]}},
                                      'usage': {'input_tokens': 60, 'output_tokens': 5}}).encode())


class RecallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / 'out'
        (self.dir / 'corpus.json').write_text(json.dumps(CORPUS))
        env = mock.patch.dict(os.environ, {'TYPESAFE_API_KEY': 'test-key'})
        env.start()
        self.addCleanup(env.stop)

    def run_recall(self, fake, *extra):
        argv = ['--corpus', str(self.dir / 'corpus.json'), '--query', QUERY, '--out', str(self.out)] + list(extra)
        buf = io.StringIO()
        with mock.patch('urllib.request.urlopen', fake), redirect_stdout(buf):
            code = jev_recall.main(argv)
        self.stdout = buf.getvalue()
        return code

    def result(self):
        return json.loads((self.out / 'recall.json').read_text())

    def test_corpus_is_required(self):
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as cm:
            jev_recall.main(['--query', QUERY])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('--corpus', err.getvalue())

    def test_families_always_judged_others_shortlisted(self):
        fake = FakeJev()
        self.assertEqual(self.run_recall(fake, '--shortlist', '2'), 0)
        judged = sorted(b['state']['item']['title'] for b in fake.bodies)
        self.assertEqual(len(fake.bodies), 4 + 2)
        for t in ('Stop check', 'Tool gate', 'Router', 'Pruning'):
            self.assertIn(t, judged)
        self.assertIn('Done claim judge', judged)                 # closest non-family entries by TF-IDF
        self.assertNotIn('Gardening', judged)
        self.assertEqual(self.result()['candidates'], 6)

    def test_threshold_order_and_family_cap(self):
        self.run_recall(FakeJev(), '--top', '3')
        hits = self.result()['hits']
        self.assertEqual([h['id'] for h in hits], ['F1', 'F2', 'F3', 'I1'])   # families: at most max(3, 3 // 3) = 3
        self.assertEqual([h['p'] for h in hits], [0.92, 0.61, 0.55, 0.88])
        self.assertEqual(hits[0]['pointer'], 'repo-one')
        self.assertEqual(hits[3]['url'], 'u1')
        self.assertIn('== family entries', self.stdout)

    def test_one_candidate_per_request_and_what_is_sent(self):
        long_corpus = [dict(CORPUS[4], text='x' * 2000)]
        (self.dir / 'corpus.json').write_text(json.dumps(long_corpus))
        fake = FakeJev()
        self.run_recall(fake)
        self.assertEqual(len(fake.bodies), 1)
        state = fake.bodies[0]['state']
        self.assertEqual(state['situation'], QUERY)
        self.assertEqual(set(state['item']), {'kind', 'title', 'text'})      # pointer and url are not sent
        self.assertEqual(len(state['item']['text']), 900)
        self.assertEqual(list(fake.bodies[0]['questions']), ['useful'])

    def test_kinds_filter(self):
        fake = FakeJev()
        self.run_recall(fake, '--kinds', 'idea')
        self.assertEqual(sorted(b['state']['item']['title'] for b in fake.bodies),
                         ['Done claim judge', 'Evidence checker', 'Recipe helper'])
        with self.assertRaises(SystemExit) as cm:
            self.run_recall(FakeJev(), '--kinds', 'nothing')
        self.assertIn('kinds present', str(cm.exception))

    def test_bad_corpus_is_rejected(self):
        (self.dir / 'corpus.json').write_text(json.dumps([{'title': 'no id'}]))
        with self.assertRaises(SystemExit):
            self.run_recall(FakeJev())

    def test_failed_candidate_exits_2(self):
        self.assertEqual(self.run_recall(FakeJev(fail_titles={'Router'})), 2)
        self.assertEqual(self.result()['judged'], self.result()['candidates'] - 1)
        self.assertIn('1 failed', self.stdout)

    def test_json_output(self):
        self.assertEqual(self.run_recall(FakeJev(), '--json'), 0)
        rows = json.loads(self.stdout)
        self.assertEqual(rows[0]['id'], 'F1')


if __name__ == '__main__':
    unittest.main()
