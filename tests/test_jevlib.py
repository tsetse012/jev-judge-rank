"""Tests for scripts/jevlib.py. No network: urllib.request.urlopen is replaced with canned responses."""
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
import jevlib  # noqa: E402

# Korean test strings, written as escapes so the source stays ASCII.
KO_AUTO_REPLY = '\uc790\ub3d9\ub2f5\uae00'          # "auto reply", four syllables
KO_WORD = '\ub9d0'                                 # one syllable


def response(answers, input_tokens=100, model='jev-1.13.0'):
    return io.BytesIO(json.dumps({'model': model, 'answers': answers,
                                  'usage': {'input_tokens': input_tokens, 'output_tokens': 5}}).encode())


def http_error(code, body=b'{"error": "test"}'):
    return urllib.error.HTTPError(jevlib.ENDPOINT, code, 'error', {}, io.BytesIO(body))


class TempOut(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.out = self.tmp / 'out'
        env = mock.patch.dict(os.environ, {'TYPESAFE_API_KEY': 'test-key'})
        env.start()
        self.addCleanup(env.stop)
        sleep = mock.patch('jevlib.time.sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)


class LoadKeyTest(unittest.TestCase):
    def test_reads_the_environment_variable(self):
        with mock.patch.dict(os.environ, {'TYPESAFE_API_KEY': 'abc'}):
            self.assertEqual(jevlib.load_key(), 'abc')

    def test_missing_key_exits_without_reading_any_file(self):
        no_files = AssertionError('load_key must not read files')
        with mock.patch.dict(os.environ, {}), mock.patch('builtins.open', side_effect=no_files), \
                mock.patch.object(Path, 'read_text', side_effect=no_files), \
                mock.patch.object(Path, 'exists', side_effect=no_files):
            os.environ.pop('TYPESAFE_API_KEY', None)
            with self.assertRaises(SystemExit) as cm:
                jevlib.load_key()
            self.assertNotIn('TYPESAFE_API_KEY', os.environ)
        self.assertIn(jevlib.KEYS_URL, str(cm.exception))


class AskTest(TempOut):
    def test_request_shape(self):
        seen = {}

        def fake(req, timeout=None):
            seen['req'], seen['timeout'] = req, timeout
            return response({'q': {'type': 'noul', 'noul': 0.7}})
        with mock.patch('urllib.request.urlopen', fake):
            jev = jevlib.Jev(self.out)
            answers = jev.ask({'text': 'hello'}, {'q': jevlib.noul('`text` is a greeting')})
        req = seen['req']
        self.assertEqual(answers, {'q': {'type': 'noul', 'noul': 0.7}})
        self.assertEqual(req.full_url, 'https://api.typesafe.ai/v1/systemone')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('Authorization'), 'Bearer test-key')
        self.assertEqual(req.get_header('Content-type'), 'application/json')
        self.assertEqual(json.loads(req.data.decode()), {
            'model': 'jev-latest', 'state': {'text': 'hello'},
            'questions': {'q': {'type': 'noul', 'instructions': '`text` is a greeting'}}})
        self.assertEqual(seen['timeout'], 90)

    def test_raw_log_and_cost_ledger(self):
        replies = iter([response({'q': {'noul': 0.1}}, input_tokens=100), response({'q': {'noul': 0.2}}, input_tokens=300)])
        with mock.patch('urllib.request.urlopen', lambda req, timeout=None: next(replies)):
            jev = jevlib.Jev(self.out)
            jev.ask({'n': 1}, {'q': jevlib.noul('x')}, note='first')
            jev.ask({'n': 2}, {'q': jevlib.noul('x')}, note='second')
        lines = [json.loads(x) for x in (self.out / 'raw_jev.jsonl').read_text().splitlines()]
        self.assertEqual([x['note'] for x in lines], ['first', 'second'])
        self.assertEqual(lines[1]['request']['state'], {'n': 2})
        self.assertEqual(lines[1]['response']['model'], 'jev-1.13.0')
        self.assertNotIn('test-key', (self.out / 'raw_jev.jsonl').read_text())
        costs = json.loads((self.out / 'costs.json').read_text())
        self.assertEqual(costs, {'calls': 2, 'input_tokens': 400, 'usd': round(400 * 0.042 / 1e6, 6)})

    def test_server_error_is_retried(self):
        fake = mock.Mock(side_effect=[http_error(503), response({'q': {'noul': 0.5}})])
        with mock.patch('urllib.request.urlopen', fake):
            answers = jevlib.Jev(self.out).ask({}, {'q': jevlib.noul('x')})
        self.assertEqual(answers['q']['noul'], 0.5)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_network_error_is_retried(self):
        fake = mock.Mock(side_effect=[urllib.error.URLError('down'), response({'q': {'noul': 0.5}})])
        with mock.patch('urllib.request.urlopen', fake):
            jevlib.Jev(self.out).ask({}, {'q': jevlib.noul('x')})
        self.assertEqual(fake.call_count, 2)

    def test_gives_up_after_five_attempts(self):
        fake = mock.Mock(side_effect=[http_error(503) for _ in range(5)])
        with mock.patch('urllib.request.urlopen', fake), self.assertRaises(RuntimeError) as cm:
            jevlib.Jev(self.out).ask({}, {'q': jevlib.noul('x')})
        self.assertEqual(fake.call_count, 5)
        self.assertIn('HTTP 503', str(cm.exception))

    def test_client_error_is_not_retried(self):
        fake = mock.Mock(side_effect=[http_error(400)])
        with mock.patch('urllib.request.urlopen', fake), self.assertRaises(RuntimeError):
            jevlib.Jev(self.out).ask({}, {'q': jevlib.noul('x')})
        self.assertEqual(fake.call_count, 1)

    def test_402_halts_the_run_without_retrying(self):
        fake = mock.Mock(side_effect=[http_error(402, b'{"error": {"type": "billing_error"}}')])
        with mock.patch('urllib.request.urlopen', fake):
            jev = jevlib.Jev(self.out)
            with self.assertRaises(jevlib.JevHalt) as cm:
                jev.ask({}, {'q': jevlib.noul('x')})
            self.assertIsInstance(cm.exception, SystemExit)
            self.assertIn(jevlib.BILLING_URL, str(cm.exception))
            with self.assertRaises(jevlib.JevHalt):         # later calls stop without a request
                jev.ask({}, {'q': jevlib.noul('x')})
        self.assertEqual(fake.call_count, 1)
        self.assertFalse((self.out / 'raw_jev.jsonl').exists())

    def test_stop_file_halts_before_sending(self):
        fake = mock.Mock()
        with mock.patch('urllib.request.urlopen', fake):
            jev = jevlib.Jev(self.out)
            (self.out / 'STOP').write_text('')
            with self.assertRaises(jevlib.JevHalt) as cm:
                jev.ask({}, {'q': jevlib.noul('x')})
        self.assertIn('STOP', str(cm.exception))
        fake.assert_not_called()


class PmapTest(unittest.TestCase):
    def test_keeps_order_and_prints_failures(self):
        def fn(x):
            if x == 3:
                raise ValueError('bad item')
            return x * 2
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = jevlib.pmap(fn, list(range(5)), workers=3)
        self.assertEqual(out, [0, 2, 4, None, 8])
        self.assertIn('ValueError: bad item', buf.getvalue())
        self.assertIn('1 failed in total', buf.getvalue())

    def test_on_result_sees_every_result(self):
        seen = []
        jevlib.pmap(lambda x: x + 1, [1, 2, 3], workers=2, on_result=seen.append)
        self.assertEqual(sorted(seen), [2, 3, 4])

    def test_halt_cancels_queued_items(self):
        calls = []
        lock = threading.Lock()

        def fn(x):
            with lock:
                calls.append(x)
            if x == 0:
                raise jevlib.JevHalt('stop now')
            return x
        with self.assertRaises(SystemExit):
            jevlib.pmap(fn, list(range(50)), workers=1)
        self.assertLess(len(calls), 10)


class SpearmanTest(unittest.TestCase):
    def test_perfect_and_reversed(self):
        self.assertEqual(jevlib.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertEqual(jevlib.spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)

    def test_ties_use_average_ranks(self):
        self.assertEqual(jevlib.spearman([1, 1, 2, 3], [1, 2, 3, 4]), 0.949)

    def test_degenerate_inputs(self):
        self.assertIsNone(jevlib.spearman([1, 2], [1, 2]))
        self.assertIsNone(jevlib.spearman([1, 1, 1], [1, 2, 3]))


class TokensTest(unittest.TestCase):
    def test_korean_text_becomes_two_character_pieces(self):
        self.assertEqual(jevlib.tokens(KO_AUTO_REPLY), [KO_AUTO_REPLY[0:2], KO_AUTO_REPLY[1:3], KO_AUTO_REPLY[2:4]])
        self.assertEqual(jevlib.tokens(KO_WORD), [KO_WORD])

    def test_english_words_without_stopwords(self):
        self.assertEqual(jevlib.tokens('The plant and the API, v2'), ['plant', 'api', 'v2'])

    def test_name_split_adds_joined_neighbours(self):
        parts = jevlib.name_tokens('SystemOneDotNet')
        for w in ('system', 'one', 'dot', 'net', 'dotnet', 'systemone'):
            self.assertIn(w, parts)
        self.assertEqual(jevlib.name_tokens('jev-go (fork)'), ['jev', 'go', 'jevgo'])

    def test_name_counts_twice(self):
        self.assertEqual(jevlib.tokens('', 'PlantWatch').count('plant'), 2)


class SmallHelpersTest(unittest.TestCase):
    def test_question_builders(self):
        self.assertEqual(jevlib.noul('n'), {'type': 'noul', 'instructions': 'n'})
        self.assertEqual(jevlib.score('s', ('low', 'high')), {'type': 'score', 'instructions': 's', 'criteria': ['low', 'high']})
        self.assertEqual(jevlib.choice('c', {'a': 'A'}), {'type': 'choice', 'instructions': 'c', 'criteria': {'a': 'A'}})

    def test_jsave_and_jload(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'x.json'
            jevlib.jsave(p, {'k': KO_WORD})
            self.assertEqual(jevlib.jload(p), {'k': KO_WORD})
            self.assertEqual(sorted(x.name for x in Path(d).iterdir()), ['x.json'])
            self.assertEqual(jevlib.jload(Path(d) / 'missing.json', []), [])


if __name__ == '__main__':
    unittest.main()
