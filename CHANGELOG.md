# Changelog

## 0.1.0 — 2026-10-07

- First public release: `scripts/jev_rank.py` (several Score axes per item, Noul and field penalties, weighted combined score, rank correlation check, report), `scripts/jev_dedupe.py` (TF-IDF candidate pairs, each pair asked in both orders and averaged, attachment to a representative, stricter question for components, comparison with an older list) and `scripts/jev_recall.py` (TF-IDF shortlist, one Noul per candidate), sharing `scripts/jevlib.py` (retries, raw request/response log, cost ledger, STOP file, stop on HTTP 402, resume caches).
- The API key is read from `TYPESAFE_API_KEY` only. `jev_recall.py` needs `--corpus`.
- Examples with recorded outputs (40 synthetic idea cards, 35 pattern summaries) and 51 offline tests.
