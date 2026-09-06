Table S5 (= main Table 2). Phase 2 database-resolution accuracy (Track A): for raw labels whose canonical form is an exact name/synonym of an in-branch MeSH concept (or a Cellosaurus cell line), did the pipeline assign the right id. Value- and instance-weighted Precision/Recall/F1/Accuracy.

Result file(s) in this folder: db_resolution.csv
Generating script in this folder: phase2_evaluation.py
Run:
  python phase2_evaluation.py <phase2_output_dir> <mesh.sqlite> <cellosaurus.sqlite> <out_dir>
  (produces db_resolution, acronym_expansion, vocabulary_precision, composition, collapse,
   branch_validity, consistency; the file(s) for THIS table are listed above)

Inputs for the corpus this paper reports:
  Phase 2 output dir   ../../06_final_labels_repaired/phase2_out
  pre-fold dictionary  ../../06_final_labels_repaired/phase2_out/dictionary_prefold.json.gz
  post-fold dictionary ../../06_final_labels_repaired/phase2_out/dictionary.json.gz
  labels               ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz
Pass these explicitly; the defaults in the script point at the first-pass output
kept in ../../04_phase2_output for provenance.
