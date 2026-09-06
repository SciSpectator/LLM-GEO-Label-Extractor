Table S10. Corpus composition after Phase 2 (804,427 samples, instance-weighted): per field, how many samples are MeSH-mapped, Cellosaurus, OOV, or Not Specified. Reproduced from the frozen per-sample outputs.

Result file(s) in this folder: composition.csv
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
