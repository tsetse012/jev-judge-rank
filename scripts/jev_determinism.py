#!/usr/bin/env python3
"""Does Jev answer the same question the same way when asked again?

A judgment model is used as if it were a function: same input, same verdict. Our pipelines assume that and
have never measured it. A rank correlation between two runs (rho) is not the same claim: rho counts ordering,
not agreement, so one flipped item in twelve still reads as 0.98.

This script asks a fixed set of items N times each and reports, per item:
  - Choice questions: how many of the N answers were identical, and where the flips sit (near the decision
    threshold or at high confidence).
  - Score / Noul questions: the spread of the numeric answer.

The repeats of one item are spread across the run instead of being sent back to back, so a change that happens
mid-run shows up as drift instead of hiding inside one item's block.

Every request and response goes to <out>/raw_jev.jsonl (kept by jevlib). determinism.json is derived from it.

    export TYPESAFE_API_KEY=...                       # from https://console.typesafe.ai/settings/keys
    python3 scripts/jev_determinism.py --out out/determinism
    python3 scripts/jev_determinism.py --replay mine.json --out out/mine   # your own questions

Without --replay the items are the example ideas in `examples/ideas`, which exercise Score and Noul. With it,
each comment in the replay is judged with the question set stored next to it, so you measure the questions
production actually sends.

Exit status: 0 when every Choice answer was identical, 3 when something flipped, 2 when a request failed.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jevlib import Jev, jload, jsave, now, pmap  # noqa: E402
from jev_rank import build_questions  # noqa: E402

DEFAULT_IDEAS = Path(__file__).resolve().parent.parent / 'examples/ideas/items.json'
DEFAULT_AXES = Path(__file__).resolve().parent.parent / 'examples/ideas/axes.json'

# A flip counts as "near the threshold" when every repeat sat in this band. Outside it, the model was sure and
# changed its mind anyway, which is the finding that matters.
THRESHOLD_BAND = (0.50, 0.85)


def build_comment_items(replay_path, n_praise):
    """Comment items taken from a recorded replay, so the question text is the one production actually sent."""
    d = jload(replay_path)
    if not d or 'rows' not in d:
        raise SystemExit(f'not a replay file: {replay_path}')
    rows = [r for r in d['rows'] if r.get('kind') and r.get('comment_text')]
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r['kind'], []).append(r)
    rng = random.Random(20261020)                       # fixed seed: the item set must be reproducible
    picked = []
    for kind in ('spam', 'question', 'negative'):       # rare classes first: they are where a flip costs most
        picked += by_kind.get(kind, [])
    praise = by_kind.get('praise', [])
    rng.shuffle(praise)
    picked += praise[:max(0, n_praise)]
    items = []
    for i, r in enumerate(picked):
        items.append({
            'id': f'ohou:{r["comment_key"]}',
            'source': 'ohou-comment',
            'kind_hint': r['kind'],
            'state': {'comment': str(r['comment_text'])[:600]},
            'questions': d['questions'],
        })
    return items


def build_idea_items(ideas_path, axes_path, n):
    """Idea items on the example axes: exercises Score and Noul, which Choice-only data never touches."""
    cfg = jload(axes_path)
    req = cfg['requests'][0]
    questions = build_questions(req['name'], req['questions'])
    fields = cfg.get('state_fields') or []
    items = []
    for it in (jload(ideas_path) or [])[:n]:
        state = {k: it.get(k) for k in fields} if fields else {k: v for k, v in it.items() if k != 'id'}
        items.append({'id': f'idea:{it["id"]}', 'source': 'idea-axis',
                      'kind_hint': req['name'], 'state': state, 'questions': questions})
    return items


def summarise(item, runs):
    """Per-item spread. `runs` is the list of answer dicts, one per repeat."""
    out = {'id': item['id'], 'source': item['source'], 'kind_hint': item['kind_hint'], 'reps': len(runs), 'questions': {}}
    for key in runs[0]:
        a = runs[0][key]
        if not isinstance(a, dict):
            continue
        if 'choice' in a:
            choices = [r.get(key, {}).get('choice') for r in runs]
            confs = [r.get(key, {}).get('confidence') for r in runs]
            confs = [c for c in confs if isinstance(c, (int, float))]
            distinct = sorted({str(c) for c in choices})
            rec = {
                'type': 'choice',
                'choices': choices,
                'distinct': distinct,
                'match': len(distinct) == 1,
                'conf_mean': round(statistics.fmean(confs), 4) if confs else None,
                'conf_min': round(min(confs), 4) if confs else None,
                'conf_max': round(max(confs), 4) if confs else None,
            }
            if len(distinct) > 1:
                lo, hi = THRESHOLD_BAND
                rec['flip_zone'] = 'threshold' if confs and all(lo <= c <= hi for c in confs) else 'confident'
            out['questions'][key] = rec
        elif 'score' in a or 'noul' in a:
            fld = 'score' if 'score' in a else 'noul'
            vals = [r.get(key, {}).get(fld) for r in runs]
            vals = [v for v in vals if isinstance(v, (int, float))]
            out['questions'][key] = {
                'type': fld,
                'values': vals,
                'sd': round(statistics.pstdev(vals), 5) if len(vals) > 1 else 0.0,
                'range': round(max(vals) - min(vals), 5) if vals else None,
                'exact': len({round(v, 6) for v in vals}) == 1,
            }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='Measure whether Jev repeats itself.')
    ap.add_argument('--replay', default=None,
                    help='optional JSON file with {"questions": ..., "rows": [{"kind", "comment_text", "comment_key"}]} '
                         'to test your own production questions. Without it only the example Score/Noul axes are used.')
    ap.add_argument('--ideas', default=str(DEFAULT_IDEAS))
    ap.add_argument('--axes', default=str(DEFAULT_AXES))
    ap.add_argument('--praise', type=int, default=8, help='how many praise comments to add (the rare classes are always all included)')
    ap.add_argument('--ideas-n', type=int, default=20)
    ap.add_argument('--reps', type=int, default=5)
    ap.add_argument('--model', default=None, help='default: JEV_MODEL env, else the pinned version in jevlib')
    ap.add_argument('--workers', type=int, default=12)
    ap.add_argument('--out', default='out/determinism')
    args = ap.parse_args(argv)

    items = []
    if args.replay:
        items += build_comment_items(Path(args.replay), args.praise)
    items += build_idea_items(Path(args.ideas), Path(args.axes), args.ideas_n)
    if not items:
        raise SystemExit('no items built')
    jev = Jev(args.out, model=args.model)
    args.model = jev.model
    print(f'[{now()}] items {len(items)} · reps {args.reps} · requests {len(items) * args.reps} · model {args.model}', flush=True)

    # Interleave: every item's repeats are spread across the run, so drift cannot hide inside one block.
    tasks = [(it, rep) for rep in range(args.reps) for it in items]
    random.Random(7).shuffle(tasks)

    results, errors, models = {}, [], {}
    done = [0]

    def run(task):
        it, rep = task
        try:
            return it['id'], jev.ask(it['state'], it['questions'], note=f'det:{it["id"]}:rep{rep}'), None
        except SystemExit:
            raise                                  # JevHalt / STOP: stop the whole run
        except Exception as e:                     # keep going; a failed repeat is data too
            return it['id'], None, f'rep{rep}: {type(e).__name__}: {str(e)[:200]}'

    def keep(pair):
        iid, answers, err = pair
        if err:
            errors.append(f'{iid} {err}')
        else:
            results.setdefault(iid, []).append(answers)
        done[0] += 1
        if done[0] % 25 == 0:
            print(f'[{now()}] {done[0]}/{len(tasks)}', flush=True)

    pmap(run, tasks, workers=args.workers, on_result=keep)

    # Pull the model id the API actually served out of the raw record.
    raw = Path(args.out) / 'raw_jev.jsonl'
    if raw.exists():
        for line in raw.read_text(encoding='utf-8').splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = (rec.get('response') or {}).get('model')
            if m:
                models[m] = models.get(m, 0) + 1

    complete = {i: r for i, r in results.items() if len(r) == args.reps}
    summary = [summarise(next(it for it in items if it['id'] == i), r) for i, r in complete.items()]
    summary.sort(key=lambda s: (s['source'], s['id']))

    choice_qs = [(s, k, q) for s in summary for k, q in s['questions'].items() if q['type'] == 'choice']
    numeric_qs = [(s, k, q) for s in summary for k, q in s['questions'].items() if q['type'] in ('score', 'noul')]
    flips = [(s, k, q) for s, k, q in choice_qs if not q['match']]
    confident_flips = [(s, k, q) for s, k, q in flips if q.get('flip_zone') == 'confident']
    numeric_drift = [(s, k, q) for s, k, q in numeric_qs if not q['exact']]

    report = {
        'ts': now(), 'model_requested': args.model, 'model_served': models,
        'items': len(items), 'reps': args.reps, 'requests': len(tasks), 'errors': errors,
        'choice_questions': len(choice_qs),
        'choice_exact': len(choice_qs) - len(flips),
        'choice_exact_rate': round((len(choice_qs) - len(flips)) / len(choice_qs), 4) if choice_qs else None,
        'flips': len(flips), 'confident_flips': len(confident_flips),
        'numeric_questions': len(numeric_qs), 'numeric_exact': len(numeric_qs) - len(numeric_drift),
        'max_numeric_sd': max((q['sd'] for _, _, q in numeric_qs), default=None),
        'items': summary,
    }
    jsave(Path(args.out) / 'determinism.json', report)

    print()
    print(f'model requested {args.model} · served {models or "(no raw record)"}')
    if choice_qs:
        print(f'Choice: {report["choice_exact"]}/{len(choice_qs)} identical across all {args.reps} repeats = {report["choice_exact_rate"]}')
        print(f'  flips {len(flips)} (near threshold {len(flips) - len(confident_flips)} · confident {len(confident_flips)})')
        for s, k, q in flips:
            print(f'    [{q.get("flip_zone")}] {s["id"]} · {k}: {q["choices"]} conf {q["conf_min"]}~{q["conf_max"]}')
    else:
        print('Choice: none in this item set (Score/Noul only)')
    print(f'Score/Noul: {report["numeric_exact"]}/{len(numeric_qs)} identical · max sd {report["max_numeric_sd"]}')
    for s, k, q in numeric_drift[:10]:
        print(f'    {s["id"]} · {k}: {q["values"]} sd {q["sd"]}')
    if errors:
        print(f'errors {len(errors)}: {errors[:3]}')
    print(f'raw record: {Path(args.out) / "raw_jev.jsonl"}')

    if errors:
        return 2
    return 0 if not flips else 3

if __name__ == '__main__':
    sys.exit(main())
