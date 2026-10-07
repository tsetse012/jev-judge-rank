#!/usr/bin/env python3
"""Rank a list with Jev: judge every item on several Score axes, combine them into one score, write a report.

  python3 jev_rank.py --items items.json --config axes.json --out ./out/rank [--limit 10] [--workers 16]
                      [--check AXIS:FIELD] [--title TEXT] [--reset]

items.json  JSON array of objects. Each needs a unique id (config id_field, default "id") and a title
            (config title_field, default "name").
axes.json   A list of requests, each with its own questions. One request = the state of ONE item plus that
            request's questions. Items are never bundled into one request; questions are. A request with a
            context_file sends {item_key: item, context_key: context}, so a large context goes only to the
            axis that needs it. Full reference: README, section "axes.json reference".
Combined score = weighted mean over axes of (expected level / top level index), minus Noul penalties
            (role "penalty") and minus field_penalties computed from item fields by code.
Outputs in --out: rank.json, report.txt, judged.json (cache), meta.json (config fingerprint), raw_jev.jsonl
            (every request and response), costs.json (calls, input tokens, USD; cumulative for the folder).
Resume:     run the same command with the same --out; items already in judged.json are skipped.
            If the questions, context or fields sent changed, the run stops (old and new answers must not mix);
            pass --reset to clear judged.json, or use a new --out. Kill switch: create the file <out>/STOP.
Exit status: 0 = every item judged; 2 = some items failed (listed at the end of report.txt; run again).
"""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jevlib import LOCK, Jev, choice, jload, jsave, noul, now, pmap, score, spearman  # noqa: E402


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--items', required=True, help='JSON array of items')
    ap.add_argument('--config', required=True, help='axes config (JSON)')
    ap.add_argument('--out', required=True, help='output folder; run again with the same folder to resume')
    ap.add_argument('--limit', type=int, default=0, help='smoke test: judge only the first N items')
    ap.add_argument('--workers', type=int, default=16, help='parallel requests (default 16)')
    ap.add_argument('--check', action='append', default=[], metavar='AXIS:FIELD',
                    help='write the Spearman rank correlation between a question and a numeric item field to the '
                         'report, e.g. novelty:votes (repeatable)')
    ap.add_argument('--title', default='Jev rank', help='first line of report.txt')
    ap.add_argument('--reset', action='store_true',
                    help='accept a changed config: clear judged.json and judge again (raw_jev.jsonl is kept)')
    return ap.parse_args(argv)


def build_questions(req_name, qs):
    built = {}
    for k, q in qs.items():
        t = q.get('type')
        if t == 'score':
            if len(q.get('levels') or []) < 2:
                raise SystemExit(f'{req_name}.{k}: a score question needs 2 or more levels (5 is usual)')
            built[k] = score(q['instructions'], q['levels'])
        elif t == 'noul':
            built[k] = noul(q['instructions'])
        elif t == 'choice':
            built[k] = choice(q['instructions'], q['criteria'])
        else:
            raise SystemExit(f'{req_name}.{k}: unknown type {t!r} (use score, noul or choice)')
        if q.get('role') == 'penalty' and t == 'choice':
            raise SystemExit(f'{req_name}.{k}: role "penalty" needs a noul or score question')
    return built


def load_context(req, config_path):
    if not req.get('context_file'):
        return None
    cp = Path(req['context_file'])
    cp = cp if cp.is_absolute() else Path(config_path).resolve().parent / cp
    ctx = jload(cp)
    if ctx is None:
        raise SystemExit(f'context file not found: {cp}')
    if req.get('context_ids') and isinstance(ctx, list):
        # Keep only the rows the axis is about. Giving every row lets items that fit some other row score high too.
        cidf = req.get('context_id_field', 'id')
        want = set(req['context_ids'])
        ctx = [c for c in ctx if c.get(cidf) in want]
        if not ctx:
            raise SystemExit(f'no row in {cp} matches context_ids {sorted(want)} (context_id_field={cidf})')
    if req.get('context_fields') and isinstance(ctx, list):
        # Keep only the columns the question needs: a large unrelated state lowers accuracy.
        ctx = [{f: c.get(f) for f in req['context_fields']} for c in ctx]
    return ctx


def main(argv=None):
    args = parse_args(argv)
    cfg = jload(args.config)
    if cfg is None:
        raise SystemExit(f'config file not found: {args.config}')
    items = jload(args.items)
    if not isinstance(items, list):
        raise SystemExit(f'items file not found or not a JSON array: {args.items}')
    if args.limit:
        items = items[:args.limit]
    IDF, TF = cfg.get('id_field', 'id'), cfg.get('title_field', 'name')
    SF = cfg.get('state_fields')                 # item fields sent to Jev (default: every field except the id)
    TIE = cfg.get('tie_break_field')             # on equal combined scores the larger value of this field ranks higher
    TOP = int(cfg.get('top', 100))
    QT = float(cfg.get('strength_quantile', 0.85))
    DIG = int(cfg.get('round_digits', 3))
    ids = [it.get(IDF) for it in items]
    if None in ids or len(set(ids)) != len(ids):
        raise SystemExit(f'id_field ({IDF}) is missing or repeated; every item needs a unique id')

    if not cfg.get('requests'):
        raise SystemExit('config needs a non-empty "requests" list')
    REQS = []
    seen = set()
    for r in cfg['requests']:
        if not r.get('name') or not r.get('questions'):
            raise SystemExit('every request needs a "name" and a "questions" object')
        dup = seen & set(r['questions'])
        if dup:
            raise SystemExit(f'question keys must be unique across requests: {sorted(dup)}')
        seen |= set(r['questions'])
        REQS.append((r, build_questions(r['name'], r['questions']), load_context(r, args.config)))
    AXES = [(k, float(q.get('weight', 1))) for r, _, _ in REQS for k, q in r['questions'].items()
            if q['type'] == 'score' and q.get('role', 'axis') == 'axis']
    PENS = [(k, float(q.get('below', 0.4)), float(q.get('penalty', 0.10))) for r, _, _ in REQS
            for k, q in r['questions'].items() if q.get('role') == 'penalty']
    # Deterministic field penalties. Facts such as "has a demo" or "is grounded in a source" are not asked of Jev:
    # a question about something missing from the state gets "don't know" answers (0.3-0.7) instead of a signal.
    FPENS = [(f['field'], float(f['below']), float(f.get('penalty', 0.10))) for f in cfg.get('field_penalties', [])]
    if not AXES:
        raise SystemExit('no score question with role "axis"; at least one is needed')
    LEVELS = {k: len(q['levels']) - 1 for r, _, _ in REQS for k, q in r['questions'].items() if q['type'] == 'score'}
    NUMERIC_Q = {k for r, _, _ in REQS for k, q in r['questions'].items() if q['type'] in ('score', 'noul')}
    CHECKS = []
    for c in args.check:
        if ':' not in c:
            raise SystemExit(f'--check {c!r}: use AXIS:FIELD, e.g. novelty:votes')
        ax, fld = c.split(':', 1)
        if ax not in NUMERIC_Q:
            raise SystemExit(f'--check {c!r}: {ax!r} is not a score or noul question in the config')
        CHECKS.append((ax, fld))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache, meta_p = out / 'judged.json', out / 'meta.json'
    # Fingerprint of everything that changes the answers (questions, context, fields sent). If it changed, old
    # cached answers would silently mix with new ones, so stop loudly instead.
    sig = hashlib.sha256(json.dumps({'requests': cfg['requests'], 'state_fields': SF, 'ctx': [c for _, _, c in REQS]},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    if cache.exists():
        old = (jload(meta_p, {}) or {}).get('config_sig')
        if old != sig:
            if args.reset:
                cache.unlink()
                print(f"[{now()}] --reset: config fingerprint {old} -> {sig}; cleared judged.json "
                      "(raw_jev.jsonl kept)", flush=True)
            else:
                raise SystemExit(f"config changed (fingerprint {old} -> {sig}); old and new answers must not mix. "
                                 "Pass --reset to judge again in this folder, or use a new --out to keep both.")
    jev = Jev(out)
    jsave(meta_p, {'config_sig': sig, 'config': str(Path(args.config).resolve()),
                   'items': str(Path(args.items).resolve()), 'updated': now()})

    def state_of(it, fields=None):
        fields = fields or SF
        return {k: it.get(k) for k in fields} if fields else {k: v for k, v in it.items() if k != IDF}

    def judge(it):
        res = {'id': it[IDF]}
        for r, Q, ctx in REQS:
            st = state_of(it, r.get('state_fields'))
            if ctx is not None:
                st = {r.get('item_key', 'item'): st, r.get('context_key', 'context'): ctx}
            a = jev.ask(st, Q, note=f"rank:{r['name']}:{it[IDF]}")
            for k, q in r['questions'].items():
                if q['type'] == 'score':
                    res[k] = round(a[k]['score'], 3)
                    res[k + '_conf'] = round(a[k].get('confidence') or 0, 2)
                elif q['type'] == 'noul':
                    res[k] = round(a[k]['noul'], 3)
                else:
                    res[k] = a[k]['choice']
                    res[k + '_conf'] = round(a[k].get('confidence') or 0, 2)
        return res

    J = {x['id']: x for x in (jload(cache, []) or [])}
    pending = [it for it in items if it[IDF] not in J]
    print(f"[{now()}] items {len(items)} · cached {len(items) - len(pending)} · to judge {len(pending)} · "
          f"requests per item {len(REQS)} · axes {[k for k, _ in AXES]}", flush=True)
    done = [0]

    def keep(r):
        with LOCK:
            J[r['id']] = r
            done[0] += 1
            if done[0] % 50 == 0:
                jsave(cache, list(J.values()))
                print(f"[{now()}] judged {done[0]}/{len(pending)}", flush=True)

    try:
        if pending:
            pmap(judge, pending, workers=args.workers, on_result=keep)
    finally:
        with LOCK:      # also on STOP / HTTP 402 / Ctrl-C, so a rerun resumes from everything already paid for
            jsave(cache, list(J.values()))
    miss = [it[IDF] for it in items if it[IDF] not in J]

    W = sum(w for _, w in AXES)
    rows = []
    for it in items:
        r = J.get(it[IDF])
        if not r:
            continue
        comb = (sum(w * r[k] / LEVELS[k] for k, w in AXES) / W
                - sum(p for k, b, p in PENS if r.get(k, 1) < b)
                - sum(p for f, b, p in FPENS if isinstance(it.get(f), (int, float)) and it.get(f) < b))
        rows.append({**r, 'title': it.get(TF), 'combined': round(comb, DIG), 'tie': it.get(TIE) if TIE else None})
    rows.sort(key=lambda x: (-x['combined'], -(x['tie'] or 0) if isinstance(x['tie'], (int, float)) else 0))

    def cut(k):
        v = sorted(x[k] for x in rows)
        return v[int(QT * (len(v) - 1))] if v else 99
    CUT = {k: cut(k) for k, _ in AXES}
    for n, x in enumerate(rows, 1):
        x['rank'] = n
        x['strengths'] = [k for k, _ in AXES if x[k] >= CUT[k]]
    jsave(out / 'rank.json', rows)

    # ---------- checks ----------
    checks = []
    byid = {it[IDF]: it for it in items}
    for ax, fld in CHECKS:
        pairs = [(x[ax], byid[x['id']].get(fld)) for x in rows if isinstance(byid[x['id']].get(fld), (int, float))]
        rho = spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) > 2 else None
        checks.append(f"{ax} vs {fld} rank correlation {rho} (n={len(pairs)})")
    costs = jload(out / 'costs.json', {}) or {}

    # ---------- report ----------
    disp = cfg.get('report_fields', [])          # item fields printed under each top item
    grp = cfg.get('group_by_field')              # Part 3 groups the top N by this field (list values allowed)
    L = [f"{args.title} ({now()[:10]})", "",
         f"Method: {len(REQS)} Jev request(s) per item ({', '.join(r['name'] for r, _, _ in REQS)}); items are never "
         "bundled into one request. Axes = " + ' · '.join(f"{k} (weight {w:g})" for k, w in AXES)
         + ". Combined = weighted mean of (axis expected value / top level index)"
         + ''.join(f", minus {p:g} if {k} < {b:g}" for k, b, p in PENS)
         + ''.join(f", minus {p:g} if field {f} < {b:g}" for f, b, p in FPENS)
         + f". Strength = top {round((1 - QT) * 100)}% on that axis.",
         f"Scale: items {len(items)} · judged {len(rows)} · missing {len(miss)} · Jev calls {costs.get('calls', 0)} · "
         f"input {costs.get('input_tokens', 0):,} tokens ≈ ${costs.get('usd', 0):.3f}",
         "Checks: " + (' · '.join(checks) if checks else '(none; use --check axis:field to compare with another signal)'),
         "How to read: use the order, not the absolute values (scores sit in a narrow band). "
         "Open the top items yourself before acting on them.",
         "=" * 78, f"Part 1. Top {min(TOP, len(rows))} overall", "=" * 78]
    for x in rows[:TOP]:
        it = byid[x['id']]
        L.append("")
        L.append(f"#{x['rank']} [{x['id']}] {x['title']}"
                 + (f"   strengths [{'·'.join(x['strengths'])}]" if x['strengths'] else ''))
        L.append("  scores: " + ' · '.join(f"{k} {x[k]:.2f}/{LEVELS[k]}" for k, _ in AXES)
                 + f" · combined {x['combined']}" + ''.join(f" · {k} {x.get(k)}" for k, _, _ in PENS))
        for f in disp:
            v = it.get(f)
            if v not in (None, '', []):
                L.append(f"  {f}: {v}")
    L += ["", "=" * 78, "Part 2. Top 15 per axis (overall rank in parentheses)", "=" * 78]
    for k, _ in AXES:
        L += ["", f"## {k}"]
        for n, x in enumerate(sorted(rows, key=lambda x: -x[k])[:15], 1):
            L.append(f"{n:>2}. [{x['id']}] {x['title']} {k} {x[k]:.2f} (#{x['rank']})")
    if grp:
        L += ["", "=" * 78, f"Part 3. Top {TOP} grouped by {grp}", "=" * 78]
        g = defaultdict(list)
        for x in rows[:TOP]:
            v = byid[x['id']].get(grp)
            vs = v if isinstance(v, list) else ([v] if v not in (None, '') else [])
            for one in vs:
                g[one].append(x)
        for key in sorted(g, key=lambda k: -len(g[k])):
            L += ["", f"## {key}: {len(g[key])} item(s)"] + [f"- #{x['rank']} [{x['id']}] {x['title']}" for x in g[key]]
    if miss:
        L += ["", f"Missing {len(miss)} item(s) (judging failed; run the same command again to retry only these): "
                  f"{miss[:30]}"]
    (out / 'report.txt').write_text('\n'.join(L) + '\n', encoding='utf-8')
    print(f"[{now()}] done: judged {len(rows)}/{len(items)} · missing {len(miss)} · strength cut-offs {CUT}"
          + (' · ' + ' · '.join(checks) if checks else '')
          + f" · Jev calls {costs.get('calls', 0)} ${costs.get('usd', 0):.3f} -> {out / 'rank.json'} · {out / 'report.txt'}",
          flush=True)
    return 0 if not miss else 2


if __name__ == '__main__':
    sys.exit(main())
