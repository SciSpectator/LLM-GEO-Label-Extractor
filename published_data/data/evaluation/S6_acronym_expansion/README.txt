Table S6. Bare disease-acronym -> MeSH expansion (Track B). Over samples whose bare label is a known disease acronym with GSE context available: resolved vs not_resolved, and the MeSH target when resolved.

Result file(s) in this folder: acronym_expansion.csv
Generating script in this folder: acronym_gold_eval.py
Run:
  python3 acronym_gold_eval.py \
      ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz \
      ../../../reference/mesh.sqlite phase2_evaluation.py acronym_expansion.csv

Expected: recall 36,549/37,736 = 0.9685, precision 36,549/36,658 = 0.9970;
21 acronyms resolved in every instance, 18 in some.

DO NOT regenerate this table with phase2_evaluation.py. That script reads the
outcome from the dictionary top-level entry, which for an abbreviation is the
corpus-wide default the collision guard withheld on, and it reports recall 0.298
where the corpus shows 0.968. It also writes a four-column file over this
seven-column one. phase2_evaluation.py is kept here only because
acronym_gold_eval.py imports the ACRONYM_GOLD list from it rather than restating
it; it is not this table's generator. The per-sample outcome exists only in the
released corpus, which is why that corpus is the input above.
