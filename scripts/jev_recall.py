#!/usr/bin/env python3
"""Recall the corpus entries (design patterns, past ideas, notes...) that are useful for one situation sentence.

  python3 jev_recall.py --corpus corpus.json --query "..." [--top 12] [--threshold 0.5] [--shortlist 120]
                        [--kinds KIND,...] [--workers 16] [--out DIR] [--json]

Corpus file: a JSON array of objects, for example
  [{"id": "F02", "kind": "family", "title": "Completion-claim check at stop",
    "text": "When an agent is about to stop and says it is done, ...", "pointer": "stingray"}]
  id    required, unique.
  text  what gets matched and judged (a few hundred characters works well; the first 900 are sent).
  title, kind, pointer, url, family: optional. pointer / url say where to read more; they are not sent.
  Entries with kind "family" skip the shortlist and are always judged (use it for a small set of
  pattern summaries); every other kind goes through the shortlist.
Step 1, local: TF-IDF (English words, Korean two-character pieces, split names) keeps the --shortlist
  non-family entries closest to the query.
Step 2, Jev: one request per candidate with the Noul question "using this item for the situation would
  actually help". Candidates are never bundled into one numbered request.
Output: entries with probability >= --threshold, highest first. "family" entries are listed first (up to
  max(3, top/3)), then the others (up to --top). Written to <out>/recall.json, with every request and
  response in <out>/raw_jev.jsonl. --out defaults to ~/.cache/jev-judge-rank/recall/<timestamp>.
Exit status: 0 = every candidate judged; 2 = some candidates failed.
"""
import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jevlib import Jev, jload, jsave, noul, now, pmap, tokens  # noqa: E402

# Pointer-mode recall question, adapted from samdotmak/jev-recall (MIT); asked one candidate per request.
Q_USEFUL = ("Using the design or example in `item` for `situation` would actually help: the situation has the same "
            "kind of decision to make, or the structure carries over as it is. False if only words overlap and the "
            "structure or purpose differs.")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--query', required=True, help='the situation in one sentence: what you need to decide or sort')
    ap.add_argument('--corpus', required=True, help='JSON array of entries (shape above)')
    ap.add_argument('--top', type=int, default=12, help='maximum non-family hits listed (default 12)')
    ap.add_argument('--threshold', type=float, default=0.5, help='minimum probability to list an entry (default 0.5)')
    ap.add_argument('--shortlist', type=int, default=120, help='non-family entries kept by TF-IDF (default 120)')
    ap.add_argument('--kinds', default='', help='comma-separated kinds to keep (default: every kind)')
    ap.add_argument('--workers', type=int, default=16, help='parallel requests (default 16)')
    ap.add_argument('--out', help='output folder (default ~/.cache/jev-judge-rank/recall/<timestamp>)')
    ap.add_argument('--json', action='store_true', help='print the hits as JSON instead of text')
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    corpus = jload(args.corpus)
    if not corpus:
        raise SystemExit(f'corpus not found or empty: {args.corpus}')
    if not isinstance(corpus, list) or not all(isinstance(c, dict) and 'id' in c for c in corpus):
        raise SystemExit(f'{args.corpus}: expected a JSON array of objects that each have an "id"')
    kinds = {k for k in args.kinds.split(',') if k}
    items = [c for c in corpus if not kinds or c.get('kind') in kinds]
    if not items:
        raise SystemExit(f'no corpus entry has a kind in {sorted(kinds)}; kinds present: '
                         f'{sorted({str(c.get("kind")) for c in corpus})}')

    # TF-IDF over tokens() (English words, Korean two-character pieces, split names; see jevlib).
    docs = [Counter(tokens(c.get('text', ''), c.get('title', ''))) for c in items]
    df = Counter(w for d in docs for w in d)
    N = len(docs)

    def vec(c):
        v = {w: (1 + math.log(n)) * math.log((N + 1) / (df.get(w, 0) + 1)) for w, n in c.items()}
        nm = math.sqrt(sum(x * x for x in v.values())) or 1
        return {w: x / nm for w, x in v.items()}
    qv = vec(Counter(tokens(args.query)))
    dvs = [vec(d) for d in docs]
    sims = [(sum(x * dv.get(w, 0) for w, x in qv.items()), k) for k, dv in enumerate(dvs)]
    fam = [k for k, c in enumerate(items) if c.get('kind') == 'family']
    rest = sorted((s, k) for s, k in sims if items[k].get('kind') != 'family')[::-1][:args.shortlist]
    cand = fam + [k for s, k in rest]

    out = Path(args.out) if args.out else Path.home() / '.cache' / 'jev-judge-rank' / 'recall' / time.strftime('%Y%m%d-%H%M%S')
    jev = Jev(out)
    Q = {'useful': noul(Q_USEFUL)}

    def judge(k):
        c = items[k]
        a = jev.ask({'situation': args.query,
                     'item': {'kind': c.get('kind'), 'title': c.get('title'), 'text': (c.get('text') or '')[:900]}},
                    Q, note=f"recall:{c['id']}")
        return k, round(a['useful']['noul'], 3)
    res = [r for r in pmap(judge, cand, workers=args.workers) if r]
    failed = len(cand) - len(res)
    hits = sorted([(p, k) for k, p in res if p >= args.threshold], reverse=True)
    fam_hits = [(p, k) for p, k in hits if items[k].get('kind') == 'family'][:max(3, args.top // 3)]
    idea_hits = [(p, k) for p, k in hits if items[k].get('kind') != 'family'][:args.top]
    rows = [{'p': p, **{f: items[k].get(f) for f in ('id', 'kind', 'title', 'family', 'url', 'pointer')},
             'text': (items[k].get('text') or '')[:200]} for p, k in fam_hits + idea_hits]
    jsave(out / 'recall.json', {'query': args.query, 'corpus': args.corpus, 'candidates': len(cand),
                                'judged': len(res), 'threshold': args.threshold, 'hits': rows})
    costs = jload(out / 'costs.json', {}) or {}
    code = 0 if not failed else 2
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return code
    print(f"[{now()}] query: {args.query}\ncorpus {len(items)} -> candidates {len(cand)} (all {len(fam)} family + "
          f"top {len(rest)} by similarity) -> Jev {len(res)} calls · input {costs.get('input_tokens', 0):,} tokens "
          f"≈ ${costs.get('usd', 0):.4f} · {len(hits)} at or above threshold {args.threshold}"
          + (f" · {failed} failed" if failed else ''))
    if fam_hits:
        print('\n== family entries')
        for p, k in fam_hits:
            c = items[k]
            print(f"  {p:.2f} [{c['id']}] {c.get('title')} -> {c.get('pointer') or ''}")
    if idea_hits:
        print('\n== other entries')
        for p, k in idea_hits:
            c = items[k]
            print(f"  {p:.2f} [{c['id']}] {c.get('title')} ({c.get('kind')}{', ' + c['family'] if c.get('family') else ''})"
                  f" -> {(c.get('text') or '')[:110]}\n        -> {c.get('pointer') or c.get('url') or ''}")
    if not hits:
        print('\nNo entry reached the threshold. Rewrite the query around the decision to make (what gets sorted '
              'into what) and run it fresh.\n'
              '  (Lowering --threshold against the same result is post-hoc tuning — declare the threshold first and re-run.)')
    print(f"\nRaw: {out / 'recall.json'} · {out / 'raw_jev.jsonl'}")
    return code


if __name__ == '__main__':
    sys.exit(main())
