#!/usr/bin/env python3
"""Merge near-duplicates in a list with Jev, optionally also against an older list.

  python3 jev_dedupe.py --items new.json --text-fields pitch,mechanism --out ./out/dedupe
      [--against old.json --against-text-fields summary] [--component-field kind --component-values sdk,plugin]
      [--order-field votes] [--k-new 5 --k-old 3 --threshold 0.65 --workers 12] [--dry]

How it works:
1. Candidate pairs, locally: TF-IDF over English words, Korean two-character pieces and the split name
   (counted twice) gives each item its --k-new nearest neighbours in the list and --k-old in the --against
   list. Pairs whose names match (ignoring a trailing "(...)") are always added. Comparing every pair is
   n^2 calls, and almost all of those pairs are unrelated.
2. Each pair gets one Noul question ("are a and b the same?") twice, with a and b swapped, and the two
   answers are averaged: swapping the order alone changes about 13% of answers. Average >= --threshold
   (default 0.65) = duplicate. True duplicates mostly score 0.65-0.77; scores of 0.85 or more are rare.
3. If either item is a component (see --component-field: SDK, MCP server, plugin, list...), a separate
   question is used that is true only for the same kind of component on the same language and platform.
   The default question's "true if only the language differs" clause otherwise merges a Go SDK with an
   MCP server.
4. Grouping attaches items to a representative (a hub), not union-find. Items are visited in --order-field
   order, largest first; each joins the existing representative it matches best at or above the threshold,
   or becomes a new representative. Union-find would merge A and C because A~B and B~C, which builds
   wrong clusters.
Only the title and the --text-fields reach Jev; --sim-fields are used for the neighbour search only.
Outputs in --out: groups.json {threshold, pairs_asked, pairs_missing,
  reps: [{id, title, members: [{id, title, same}]}], dup_against: {id: {against_id, against_title, same}}},
  pairs.json (cache of averaged answers), raw_jev.jsonl, costs.json. Resume: run the same command again.
  Kill switch: create the file <out>/STOP.
Exit status: 0 = every pair answered; 2 = some pairs failed (run the same command again to retry them).
"""
import argparse
import math
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jevlib import LOCK, Jev, jload, jsave, noul, now, pmap, tokens  # noqa: E402

Q_SAME = ("`a` and `b` are essentially the same mechanism: they do the same kind of thing, for the same purpose, "
          "in the same way. True if only the programming language, framework, UI or name differs. False if what "
          "they work on or what they are for differs.")
Q_COMPONENT = ("`a` and `b` are the same kind of component: they play the same role in the same language on the same "
               "platform (for example two Go SDKs, two MCP servers, two n8n nodes, two lists of the same kind). False "
               "if the language or platform differs (a Go SDK and an MCP server, a .NET SDK and an n8n node). False "
               "if either one is an app or tool that solves a specific problem.")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--items', required=True, help='JSON array of items to dedupe')
    ap.add_argument('--out', required=True, help='output folder; run again with the same folder to resume')
    ap.add_argument('--id-field', default='id')
    ap.add_argument('--title-field', default='name')
    ap.add_argument('--text-fields', required=True,
                    help='comma-separated; the first field = one sentence on what the item does, the others are '
                         'joined as the core description (first 300 characters). Sent to Jev with the title')
    ap.add_argument('--sim-fields', default='',
                    help='comma-separated fields used only to find neighbours, never sent to Jev (for example a '
                         'short English description when the text fields are in another language)')
    ap.add_argument('--against', help='an older list; new items that match one of its entries go to dup_against')
    ap.add_argument('--against-id-field', default='id')
    ap.add_argument('--against-title-field', default='name')
    ap.add_argument('--against-text-fields', help='default: the same as --text-fields')
    ap.add_argument('--against-sim-fields', default='', help='default: the same as --sim-fields')
    ap.add_argument('--component-field', help='item field that says what kind of thing an item is')
    ap.add_argument('--component-values', default='',
                    help='comma-separated values of --component-field that mark a component, e.g. '
                         'sdk,mcp_server,plugin,list; pairs that include one use --q-component')
    ap.add_argument('--order-field', help='numeric field; the larger value becomes the representative (e.g. votes)')
    ap.add_argument('--k-new', type=int, default=5, help='neighbours per item inside the list (default 5)')
    ap.add_argument('--k-old', type=int, default=3, help='neighbours per item in the --against list (default 3)')
    ap.add_argument('--min-sim', type=float, default=0.10, help='skip neighbours below this TF-IDF cosine (default 0.10)')
    ap.add_argument('--threshold', type=float, default=0.65,
                    help='average same-probability at or above which a pair is a duplicate (default 0.65)')
    ap.add_argument('--workers', type=int, default=12, help='parallel requests (default 12)')
    ap.add_argument('--q-same', default=Q_SAME, help='Noul question for ordinary pairs')
    ap.add_argument('--q-component', default=Q_COMPONENT, help='Noul question for pairs that include a component')
    ap.add_argument('--dry', action='store_true', help='count candidate pairs and calls, then stop (no API key needed)')
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.component_field and not args.component_values:
        raise SystemExit('--component-field needs --component-values (the values that mark a component)')
    new_items = jload(args.items)
    if not isinstance(new_items, list):
        raise SystemExit(f'items file not found or not a JSON array: {args.items}')
    old_items = []
    if args.against:
        old_items = jload(args.against)
        if not isinstance(old_items, list):
            raise SystemExit(f'--against file not found or not a JSON array: {args.against}')
    TFS = args.text_fields.split(',')
    ATFS = (args.against_text_fields or args.text_fields).split(',')
    SIMS = [f for f in args.sim_fields.split(',') if f]
    ASIMS = [f for f in (args.against_sim_fields or args.sim_fields).split(',') if f]
    COMP = {v for v in args.component_values.split(',') if v} if args.component_field else set()

    def mk(it, idf, tf, tfs, sims, prefix):
        txt = [str(it.get(f) or '') for f in tfs]
        extra = [str(it.get(f) or '') for f in sims] + ([str(it.get(args.component_field) or '')] if args.component_field else [])
        return {'k': f"{prefix}{it[idf]}", 'id': it[idf], 'title': it.get(tf) or str(it[idf]),
                'order': it.get(args.order_field) if args.order_field else None,
                'comp': bool(args.component_field and it.get(args.component_field) in COMP),
                'canDo': txt[0], 'core': ' | '.join(t for t in txt[1:] if t)[:300], 'text': ' '.join(txt + extra)}
    NEW = [mk(it, args.id_field, args.title_field, TFS, SIMS, 'n:') for it in new_items]
    OLD = [mk(it, args.against_id_field, args.against_title_field, ATFS, ASIMS, 'o:') for it in old_items]
    BY = {d['k']: d for d in NEW + OLD}

    # TF-IDF vectors over tokens() (English words, Korean two-character pieces, split names; see jevlib).
    docs = {d['k']: Counter(tokens(d['text'], d['title'])) for d in NEW + OLD}
    df = Counter(w for c in docs.values() for w in c)
    N = len(docs)
    vec = {}
    for k, c in docs.items():
        v = {w: (1 + math.log(n)) * math.log((N + 1) / (df[w] + 1)) for w, n in c.items()}
        nm = math.sqrt(sum(x * x for x in v.values())) or 1
        vec[k] = {w: x / nm for w, x in v.items()}

    def cos(a, b):
        va, vb = vec[a], vec[b]
        if len(va) > len(vb):
            va, vb = vb, va
        return sum(x * vb.get(w, 0) for w, x in va.items())

    def base(s):
        return re.sub(r'\s*\(.*?\)\s*$', '', s or '').lower().strip()

    pairs = set()
    for a in NEW:
        for pool, k in ((NEW, args.k_new), (OLD, args.k_old)):
            if not pool or not k:
                continue
            for s, bk in sorted(((cos(a['k'], b['k']), b['k']) for b in pool if b['k'] != a['k']), reverse=True)[:k]:
                if s >= args.min_sim:
                    pairs.add(tuple(sorted((a['k'], bk))))
        for b in NEW + OLD:
            if b['k'] != a['k'] and base(b['title']) == base(a['title']):
                pairs.add(tuple(sorted((a['k'], b['k']))))

    def kind(x, y):
        return 'w' if (BY[x]['comp'] or BY[y]['comp']) else 'm'      # w = component question, m = default question

    def pkey(x, y):
        s = tuple(sorted((x, y)))
        return f"{kind(*s)}:{s[0]}~{s[1]}"
    print(f"[{now()}] items {len(NEW)} · against {len(OLD)} · candidate pairs {len(pairs)} · "
          f"Jev calls up to {len(pairs) * 2} (each pair asked twice, order swapped)", flush=True)
    if args.dry:
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jev = Jev(out)
    DONE = jload(out / 'pairs.json', {}) or {}
    Q = {'same': noul(args.q_same)}
    QW = {'same': noul(args.q_component)}

    def st(d):
        return {'title': d['title'], 'canDo': d['canDo'], 'core': d['core']}

    def one(p):
        k = pkey(*p)
        if k in DONE:
            return None
        a, b = BY[p[0]], BY[p[1]]
        q = QW if k.startswith('w:') else Q
        p1 = jev.ask({'a': st(a), 'b': st(b)}, q, note=f'dedupe:{k}')['same']['noul']
        p2 = jev.ask({'a': st(b), 'b': st(a)}, q, note=f'dedupe:{k}:rev')['same']['noul']
        return k, round((p1 + p2) / 2, 3)
    done = [0]

    def keep(r):
        with LOCK:
            DONE[r[0]] = r[1]
            done[0] += 1
            if done[0] % 50 == 0:
                jsave(out / 'pairs.json', DONE)
    pending = [p for p in sorted(pairs) if pkey(*p) not in DONE]
    print(f"[{now()}] cached pairs {len(pairs) - len(pending)} · pairs to ask {len(pending)}", flush=True)
    try:
        pmap(one, pending, workers=args.workers, on_result=keep)
    finally:
        with LOCK:      # also on STOP / HTTP 402 / Ctrl-C, so a rerun resumes from everything already paid for
            jsave(out / 'pairs.json', DONE)
    miss = [p for p in pairs if pkey(*p) not in DONE]

    def same(x, y):
        return DONE.get(pkey(x, y), 0)

    dup = {}
    for a in NEW:
        best = max(((same(a['k'], b['k']), b) for b in OLD if pkey(a['k'], b['k']) in DONE),
                   key=lambda t: t[0], default=(0, None))
        if best[0] >= args.threshold:
            dup[a['id']] = {'against_id': best[1]['id'], 'against_title': best[1]['title'], 'same': best[0]}

    def num(v):
        return v if isinstance(v, (int, float)) else 0
    reps = []
    for c in sorted([a for a in NEW if a['id'] not in dup], key=lambda d: -num(d['order'])):
        best, bv = None, 0.0
        for r in reps:                  # compare with representatives only: no transitive merging
            v = same(r['k'], c['k'])
            if v >= args.threshold and v > bv:
                best, bv = r, v
        if best:
            best['members'].append({'id': c['id'], 'title': c['title'], 'same': bv})
        else:
            reps.append({'k': c['k'], 'id': c['id'], 'title': c['title'], 'members': []})
    jsave(out / 'groups.json', {'threshold': args.threshold, 'pairs_asked': len(DONE), 'pairs_missing': len(miss),
                                'reps': [{'id': r['id'], 'title': r['title'], 'members': r['members']} for r in reps],
                                'dup_against': dup})
    mem = sum(len(r['members']) for r in reps)
    print(f"[{now()}] representatives {len(reps)} · merged into a representative {mem} · same as an --against entry "
          f"{len(dup)} · total {len(reps) + mem + len(dup)}/{len(NEW)} · pairs missing {len(miss)} -> "
          f"{out / 'groups.json'}", flush=True)
    for r in sorted(reps, key=lambda r: -len(r['members']))[:8]:
        if r['members']:
            print(f"  {r['title']} <- {', '.join(m['title'] for m in r['members'][:6])}", flush=True)
    for i, d in list(dup.items())[:8]:
        print(f"  same as an --against entry: {BY['n:' + str(i)]['title']} -> {d['against_title']} ({d['same']})",
              flush=True)
    return 0 if not miss else 2


if __name__ == '__main__':
    sys.exit(main())
