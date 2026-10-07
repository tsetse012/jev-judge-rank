"""Shared code for jev_rank.py, jev_dedupe.py and jev_recall.py. Standard library only.

- Jev: a small client for the TypeSafe System One API (model family "Jev"). One ask() = one POST.
  It retries 408/429/5xx and network errors, appends every successful request and response to
  <out>/raw_jev.jsonl, keeps a cost ledger in <out>/costs.json, and stops the whole run when the
  file <out>/STOP exists or the API answers HTTP 402 (no credit left).
- pmap: a thread-pool map that prints failures instead of hiding them.
- spearman: rank correlation, used by the --check option of jev_rank.py.
- tokens / name_tokens: the tokenizer behind the TF-IDF candidate search in jev_dedupe.py and jev_recall.py.

The API key is read from the TYPESAFE_API_KEY environment variable and from nowhere else.
"""
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
KEYS_URL = 'https://console.typesafe.ai/settings/keys'
BILLING_URL = 'https://console.typesafe.ai/settings/billing'
PRICE_IN = 0.042 / 1e6          # USD per input token ($0.042 per million); output tokens are free (TypeSafe price list, 2026-09)
RETRY_STATUS = (408, 429, 500, 502, 503, 504)
LOCK = threading.Lock()         # guards raw_jev.jsonl, costs.json and the caches the scripts update from worker threads


class JevHalt(SystemExit):
    """Stops the whole run, not just one item: the STOP file exists, or the API answered HTTP 402.

    It is a SystemExit, so when nothing catches it Python prints the message and exits with status 1."""


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def jload(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def jsave(path, obj):
    """Write JSON atomically: a temporary file first, then a rename over the target."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(path)


def load_key():
    key = os.environ.get('TYPESAFE_API_KEY')
    if key:
        return key
    raise SystemExit(f'TYPESAFE_API_KEY is not set. Create an API key at {KEYS_URL} '
                     'and export it as the TYPESAFE_API_KEY environment variable.')


# Question builders. Jev does not read meaning from the question key: everything it needs goes in `instructions`.
def noul(instructions):
    """Yes/no question; the answer is the probability that the statement is true (0..1)."""
    return {'type': 'noul', 'instructions': instructions}


def score(instructions, levels):
    """Graded question; the answer `score` is the expected level index, 0..len(levels)-1 (0..4 for five levels)."""
    return {'type': 'score', 'instructions': instructions, 'criteria': list(levels)}


def choice(instructions, criteria):
    """Pick one option; criteria maps option -> description. Keep the option list short (wide choices are weak)."""
    return {'type': 'choice', 'instructions': instructions, 'criteria': dict(criteria)}


class Jev:
    """ask(state, questions) returns the `answers` object of one API response.

    Every successful request/response pair is appended to <out>/raw_jev.jsonl. That file is the record to
    re-score from when you change how answers are combined; the summaries are derived from it."""

    def __init__(self, out_dir, model='jev-latest', stop_file=None):
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.raw = self.out / 'raw_jev.jsonl'
        self.costs = self.out / 'costs.json'
        self.model = model
        self.stop = Path(stop_file) if stop_file else self.out / 'STOP'
        self.key = load_key()
        self.halted = None      # set after HTTP 402, so later calls stop without sending anything

    def check_stop(self):
        if self.halted:
            raise JevHalt(self.halted)
        if self.stop.exists():
            raise JevHalt(f'STOP file found ({self.stop}); stopping. Delete it and run the same command '
                          'again to continue from the cache.')

    def ask(self, state, questions, note='', timeout=90, retries=5):
        self.check_stop()
        body = {'model': self.model, 'state': state, 'questions': questions}
        data = json.dumps(body, ensure_ascii=False).encode()
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(ENDPOINT, data=data, headers={
                'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'})
            try:
                t = time.perf_counter()
                r = json.load(urllib.request.urlopen(req, timeout=timeout))
                ms = round((time.perf_counter() - t) * 1000)
                usage = r.get('usage', {}) or {}
                with LOCK:
                    with self.raw.open('a', encoding='utf-8') as f:
                        f.write(json.dumps({'ts': now(), 'note': note, 'ms': ms, 'request': body, 'response': r},
                                           ensure_ascii=False) + '\n')
                    c = jload(self.costs, {}) or {}
                    c['calls'] = c.get('calls', 0) + 1
                    c['input_tokens'] = c.get('input_tokens', 0) + (usage.get('input_tokens') or 0)
                    c['usd'] = round(c['input_tokens'] * PRICE_IN, 6)
                    jsave(self.costs, c)
                return r['answers']
            except urllib.error.HTTPError as e:
                detail = e.read()[:300].decode(errors='replace')
                last = f'HTTP {e.code}: {detail}'
                if e.code == 402:
                    self.halted = (f'Jev {last}\nHTTP 402 means the TypeSafe organization has no credit left. '
                                   f'Top up or turn on auto-reload at {BILLING_URL}, then run the same command '
                                   'again; finished work is cached.')
                    raise JevHalt(self.halted)
                if e.code in RETRY_STATUS and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1) + random.random())
                    continue
                raise RuntimeError(f'Jev {last}')
            except (urllib.error.URLError, OSError) as e:   # OSError covers socket.timeout on 3.9
                last = str(e)
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'Jev failed after {retries} attempts: {last}')


def pmap(fn, items, workers=12, on_result=None):
    """Parallel map that keeps the input order.

    A failing item becomes None and its error is printed; failures are never hidden. on_result(r) runs for
    each non-None result as it arrives (the scripts use it to update their cache). SystemExit (including
    JevHalt) and Ctrl-C stop the run: queued items are cancelled and the exception propagates."""
    out = [None] * len(items)
    errs = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): k for k, it in enumerate(items)}
        try:
            for f in as_completed(futs):
                k = futs[f]
                try:
                    out[k] = f.result()
                    if on_result and out[k] is not None:
                        on_result(out[k])
                except SystemExit:
                    raise
                except Exception as e:
                    errs.append(f'#{k}: {type(e).__name__}: {str(e)[:200]}')
        except BaseException:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
    for e in errs[:10]:
        print(f'[{now()}] failed {e}', flush=True)
    if errs:
        print(f'[{now()}] {len(errs)} failed in total; run the same command again to retry only the missing ones',
              flush=True)
    return out


def spearman(a, b):
    """Spearman rank correlation with average ranks for ties. None when n < 3 or a side is constant."""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    if len(a) < 3:
        return None
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return round(num / den, 3) if den else None


# ---------- Text similarity: local, no API calls ----------
# jev_dedupe.py and jev_recall.py use TF-IDF over these tokens only to pick candidates cheaply; Jev judges the
# candidates. The Korean handling is deliberate: Korean words carry particles and endings and are often written
# without spaces, so whole-word matching misses most overlaps. Overlapping two-character pieces (bigrams) of
# each run of Hangul syllables (U+AC00..U+D7A3) match them. Other non-Latin scripts are not tokenized.
STOPWORDS = frozenset('the and for with a an of to in on is it that this from by as or be are using use '
                      'built powered your you via into its not'.split())
HANGUL_RUN = re.compile('[\uac00-\ud7a3]+')


def name_tokens(title):
    """Split a name at case changes, digits and symbols, and also add each pair of neighbouring pieces joined:
    SystemOneDotNet -> system, one, dot, net, systemone, onedot, dotnet. Text in parentheses is ignored."""
    parts = [w.lower() for w in re.findall(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+', re.sub(r'\(.*?\)', ' ', title or ''))]
    return [w for w in parts + [a + b for a, b in zip(parts, parts[1:])] if len(w) >= 2 and w not in STOPWORDS]


def tokens(text, title=''):
    """English words of two or more letters or digits, Korean two-character pieces, and the split name twice."""
    t = (text or '').lower()
    out = [w for w in re.findall(r'[a-z0-9]{2,}', t) if w not in STOPWORDS]
    for run in HANGUL_RUN.findall(t):
        out += [run[k:k + 2] for k in range(len(run) - 1)] if len(run) > 1 else [run]
    return out + name_tokens(title) * 2
