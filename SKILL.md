---
name: jev-judge-rank
description: Rank a list on several written criteria with Jev (TypeSafe System One), merge near-duplicates, or recall the entries of a corpus that help one situation. Triggers - "rank this list with Jev", "score these items", "top N of these", "dedupe this list", "find similar Jev patterns".
---

# jev-judge-rank

Three scripts built on Jev, a judgment model that answers Choice, Score and Noul (yes/no probability) questions and cannot write text. The full documentation is `README.md` in this folder.

## When to use

- A list of tens to thousands of items (ideas, issues, articles, tools, comments) has to be ranked on criteria that can be written as sentences, cleared of near-duplicates, or searched for what helps one situation.
- Offer it without being asked when a list has more than 50 items and two or more criteria.
- Not for writing text (summaries, explanations), images or audio, or counting and date comparison; those belong to an LLM, a vision model or code. A rank is a reading order: open the top items before acting on them.

## Before running

- `TYPESAFE_API_KEY` must be set in the environment. If it is missing, ask the user to create a key at https://console.typesafe.ai/settings/keys and export it; never write a key into a file.
- The selected fields of every item are sent to TypeSafe's API (United States). Check with the user that those fields hold no secrets or personal data they may not share.

## Commands

```bash
S=~/.claude/skills/jev-judge-rank        # this folder
# rank: copy examples/ideas/axes.json and edit it; smoke test with --limit 10, then the full run into the same --out
python3 $S/scripts/jev_rank.py --items items.json --config axes.json --out out/rank --limit 10
python3 $S/scripts/jev_rank.py --items items.json --config axes.json --out out/rank
# dedupe: the first text field is one sentence on what the item does; --dry counts pairs without a key
python3 $S/scripts/jev_dedupe.py --items items.json --text-fields pitch,mechanism --order-field votes --out out/dedupe
# recall: the corpus is a JSON array of {id, text, title, kind, pointer}
python3 $S/scripts/jev_recall.py --corpus corpus.json --query "the decision to make, in one sentence" --out out/recall
```

## Rules to keep

- One item (or pair) per request; several questions per request are fine.
- Write all five Score levels as sentences and spell out the lowest one.
- Facts that are not in the state (has a demo, is based on a source) go in item fields and `field_penalties`, not in questions.
- Exit status 2 means some items or pairs failed: run the same command again. To stop a run, create `<out>/STOP`.
- Report the result as an order, with the method, the counts, the cost from `costs.json` and the `--check` line. Thresholds were measured on `jev-1.13.0`.
