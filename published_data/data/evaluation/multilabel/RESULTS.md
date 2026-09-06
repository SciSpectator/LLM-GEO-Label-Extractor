# Multi-label extraction PRECISION — result

Independent judge = Claude (non-Gemma, §8; same validity basis as the OOV audit, human κ=1.0).
Sample = 400 multi-label cells (fields with >=2 ';'-separated labels), judged per shown label against
the sample's own raw metadata (title/source/characteristics). Recall (missing labels) is NOT measured:
no gold enumerating every true label per sample exists.

- per-label precision:           **0.907** [0.885–0.925] (758/836 decided; 89 labels indeterminate)
- per-sample all-labels-correct: **0.8444** (331/392 cells with a decidable verdict)

Reproduce with `python3 score_all.py` from this folder; it prints the line the numbers come from.

Error composition (the 78 incorrect): dominated by within-cell DUPLICATES (same concept twice) and
'Not Specified' MIXED with real labels — both deterministic data-hygiene artefacts; plus some
wrong-field extractions and metadata contradictions. Deduping identical ids and dropping NS when a
real label is present would raise precision further.

Prevalence (deterministic, whole corpus): multi-label cells = Condition 68,537 (8.52%),
Treatment 23,572 (2.93%), Tissue 16,280 (2.02%).

Files: multilabel_sample.csv, multilabel_shard*.csv, multilabel_judge.csv,
multilabel_shard*_judge.csv, score.py, score_all.py, JUDGE_PROMPT_MULTILABEL.md.
