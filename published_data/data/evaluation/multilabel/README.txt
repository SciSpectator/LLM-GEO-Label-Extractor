Supplementary Note: multi-label extraction. Per-label precision on cells that
carry more than one label in a field.

This is a judged evaluation, not a deterministic one. It is kept apart from the
numbered table folders because it measures precision only: a sample's cell can
be scored for whether each label shown is supported by that sample's own raw
metadata, but no resource enumerates every label that should have been there, so
recall is not estimable and is not reported.

Result files in this folder:
  RESULTS.md                  the two headline numbers and how the errors break down
  multilabel_judge.csv        the 150-cell calibration set (judge vs human, κ=1.0)
  multilabel_shard*_judge.csv the judged sample, one row per shown label:
                              item, field, label, verdict, reason
  multilabel_sample.csv       the drawn sample with the raw metadata each verdict
  multilabel_shard*.csv       was made against (title, source, characteristics)

Scripts in this folder: score_all.py (all shards), score.py (one shard)

Run:
  python3 score_all.py

It prints the line the supplement quotes:
  labels=925 decided=836 | PRECISION 758/836=0.907 [0.885,0.925]
                         | per-sample all-correct 331/392=0.844

Labels the judge could not decide are excluded from both the numerator and the
denominator rather than counted as correct; that is why 925 labels yield 836
decided, and 400 sampled cells yield 392 with a verdict for every label.

The judging protocol is JUDGE_PROMPT_MULTILABEL.md, also shipped as
../../../prompts/04_judge/multilabel_judge.md.
