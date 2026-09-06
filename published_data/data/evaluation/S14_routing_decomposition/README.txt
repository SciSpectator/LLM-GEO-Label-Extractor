Table S14. Phase 2 resolution routing and per-database error decomposition (distinct values and instances): MeSH exact vs retrieval-picked, Cellosaurus verified vs fuzzy, OOV appropriate vs should-be-in-DB, uncovered. C_inappropriate_oov_examples.csv lists the actionable 'should be in a DB' OOV mints.

Result file(s) in this folder: C_db_match_grades.csv + C_inappropriate_oov_examples.csv
Generating script in this folder: phase2_db_match_eval.py
Run:
  python3 phase2_db_match_eval.py
  (defaults to the repaired dictionary and the shipped reference DBs; override with
   DICTIONARY MESH.sqlite CELLOSAURUS.sqlite OUT_DIR if needed)

Inputs for the corpus this paper reports:
  Phase 2 output dir   ../../06_final_labels_repaired/phase2_out
  pre-fold dictionary  ../../06_final_labels_repaired/phase2_out/dictionary_prefold.json.gz
  post-fold dictionary ../../06_final_labels_repaired/phase2_out/dictionary.json.gz
  labels               ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz
These are now the script defaults, so no editing is needed. The first-pass output kept
in ../../04_phase2_output is retained for provenance only and is not what this table reports.
