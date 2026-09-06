Table S8. Out-of-vocabulary (OOV) concept consolidation, measured after BioLORD + LLM
similarity consolidation and before the final case-fold step (Table S12). Per field, over the
raw values Phase 2 left OOV: OOV values, the distinct concepts they consolidated into, variants
folded (values - distinct), and multi-variant clusters (concepts backed by >= 2 raw values).

Result file in this folder: oov_consolidation.csv
Input in this folder:       dictionary_prefold.json.gz  (the pre-case-fold dictionary snapshot;
                            the released final dictionary in ../../04_phase2_output/ is post-fold,
                            so this snapshot is shipped here specifically to reproduce S8)
Generating script:          phase2_oov_consolidation.py

Run:
  python3 phase2_oov_consolidation.py
  (DICT -> this folder's dictionary_prefold.json.gz; OUT -> an output dir), then:
  python phase2_oov_consolidation.py

Inputs for the corpus this paper reports:
  Phase 2 output dir   ../../06_final_labels_repaired/phase2_out
  pre-fold dictionary  ../../06_final_labels_repaired/phase2_out/dictionary_prefold.json.gz
  post-fold dictionary ../../06_final_labels_repaired/phase2_out/dictionary.json.gz
  labels               ../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz
Pass these explicitly; the defaults in the script point at the first-pass output
kept in ../../04_phase2_output for provenance.
