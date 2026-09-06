Table S12 - effect of the final case-fold step
==============================================

casefold_effect.py    compares the pre-fold value dictionary with the released one and
                      reports what the fold merged.
casefold_effect.csv   the result.

Run
---
    python3 casefold_effect.py \
        ../../06_final_labels_repaired/phase2_out/dictionary_prefold.json.gz \
        ../../06_final_labels_repaired/phase2_out/dictionary.json.gz \
        casefold_effect.csv

Expected
--------
field          merged   groups   repointed   grouped
Tissue             41       38       1,674     8,242
Condition          43       32         837     6,413
Treatment         214      198       1,666     8,261
Total             298      268       4,177    22,916

These are the numbers Table S12 and the Results report, and they come from the
repaired corpus named under Inputs below. Passing the first-pass dictionaries in
../../04_phase2_output instead gives 749/336/23,477/28,036, which describes the
earlier pass and appears nowhere in the paper.

Definitions
-----------
merged_concepts    out-of-vocabulary identifiers that disappear, their values moving to
                   another identifier.
fold_groups        released identifiers that absorbed more than one pre-fold identifier.
repointed_samples  sample instances whose identifier changed.
grouped_samples    sample instances belonging to a fold group.

Both dictionaries ship with the package, so these numbers recompute without re-running the
pipeline. The released per-sample output is already post-fold, so re-running
phase2_casefold.py over it reports zero merges by design; that is the check that the fold
is closed, and it is what the "collapsing ... to none in the released per-sample output"
clause in the Table S12 caption refers to.

Inputs for the corpus this paper reports:
  Phase 2 output dir   ../../06_final_labels_repaired/phase2_out
  pre-fold dictionary  ../../06_final_labels_repaired/phase2_out/dictionary_prefold.json.gz
  post-fold dictionary ../../06_final_labels_repaired/phase2_out/dictionary.json.gz
  labels               ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz
Pass these explicitly; the defaults in the script point at the first-pass output
kept in ../../04_phase2_output for provenance.
