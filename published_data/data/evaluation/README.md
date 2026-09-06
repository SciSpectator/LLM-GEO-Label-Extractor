# Evaluation — one folder per table

Each supplementary/main evaluation table that has a shipped deterministic result has its own
folder here, containing that table's result CSV(s), the script that generates them, and a
`README.txt` with the caption and the run command. The two LLM-judge folders (`LLM_judge/`,
`multilabel/`) document themselves in `README.md` / `RESULTS.md` instead, alongside the judge
prompt and the scoring script. Most folders hold a deterministic whole-corpus metric that needs no LLM and no gold standard; these
read the final Phase 2 outputs (`../06_final_labels_repaired/phase2_out/`), the corpus the paper
reports (`../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz`), the raw sample metadata (`../01_input_metadata/geo_metadata.sqlite`, which is built once from the shipped
`samples_804k.json` with `../01_input_metadata/build_geo_metadata.py` rather than shipped twice, and
`gse_meta_scraped.json.gz` for S9) and the reference databases
(`../../reference/{mesh,cellosaurus}.sqlite`). Three folders differ by design:
`S1_manual_benchmark/` is the human accuracy tier and needs the ALE and CREEDS gold standards, both
of which ship inside it, while `LLM_judge/` and `multilabel/` hold the independent LLM-judge audits. phase2_evaluation.py takes four positional CLI args (results dir, mesh.sqlite, cellosaurus.sqlite, out dir); the single-table scripts (S8/S9/S14) read `"Directory to …"` placeholder constants set at the top of the file.

## Folders (table → contents → producing script)

| Folder                          | Table            | Result CSV(s)                                        | Script                        |
|---------------------------------|------------------|------------------------------------------------------|-------------------------------|
| S1_manual_benchmark/            | S1               | manual_gold_{Sex,Age,Tissue_Condition,Treatment}.csv | none -- verdicts assigned by hand |
| S3_phase1b_recovery/            | S3               | phase1b_recovery.csv                                 | phase1b_recovery.py           |
| S4_label_collapses/             | S4               | label_collapses.csv                              | s4_collapse.py                |
| S5_database_resolution/        | S5 (= Table 2)  | db_resolution.csv                                    | phase2_evaluation.py          |
| S6_acronym_expansion/          | S6              | acronym_expansion.csv                                | acronym_gold_eval.py          |
| S7_vocabulary_precision/       | S7              | vocabulary_precision.csv                             | phase2_evaluation.py          |
| S8_oov_consolidation/          | S8              | oov_consolidation.csv                                | phase2_oov_consolidation.py   |
| S9_gse_shortform_audit/        | S9              | E_gse_shortform_audit.csv                            | phase2_gse_shortform_eval.py  |
| S10_corpus_composition/         | S10              | composition.csv                                      | phase2_evaluation.py          |
| S11_age_analysis/               | S11 (+ Table 1 Age) | age_composition.csv                              | age_composition.py            |
| S12_casefold/                   | S12              | casefold_effect.csv                                  | casefold_effect.py            |
| S13_baseline_comparison/        | S13              | baseline_comparison.csv                              | see that folder's README      |
| S14_routing_decomposition/      | S14              | C_db_match_grades.csv, C_inappropriate_oov_examples.csv | phase2_db_match_eval.py    |
| S17_condition_control_split/    | S15, S17         | condition_control_split.csv                          | condition_control_split.py    |
| S19_metadata_grounding/         | S19              | grounding_audit.csv                                  | grounding_audit.py            |
| S20_age_provenance/             | S20              | age_provenance.csv                                   | age_provenance.py             |
| S21_resource_summary/           | S21              | resource_summary.csv                                 | resource_summary.py           |
| S22_field_scope/                | S22              | field_scope_audit.csv                                | field_scope_audit.py          |
| S23_integrity_screens/          | S23              | screen_verification.csv, still_failing.csv           | verify_screens.py             |
| S24_acronym_population/         | S24              | acronym_population.csv                               | acronym_population.py         |
| error_analysis/                 | main text 3.3    | error_classes.csv, not_errors.csv, field_scope_corrections.csv, oov_surface_shapes.csv, examples.csv | error_analysis.py |
| Table3_normalization_quality/   | Table 3 (main)   | collapse.csv, branch_validity.csv, consistency.csv   | phase2_evaluation.py          |
| Table4_integrity_screens/       | Table 4 (main)   | table4_screens.csv                                  | table4_screens.py             |
| LLM_judge/                      | (Methods)        | t5_shard*_judge.csv, judge_oov_pilot.csv, calibration_sheet.csv | phase2 OOV LLM-judge audit (merge_score.py; κ vs 150 human) |
| multilabel/                     | (Methods/Suppl.) | multilabel*_judge.csv                                | multi-label precision LLM-judge (score_all.py)  |

`phase2_evaluation.py` produces seven CSVs in one run (the ones for S5, S6, S7, S10 and
Table 3). A copy is placed in each of those folders; each folder's README says which of the
seven belongs to that table.


`Table4_integrity_screens/` recomputes the five main-text screens on the first-pass
labels in `data/05_final_labels/`, which is the corpus Table 4 reports. Six of its ten
cells reproduce exactly. The four that do not are named in that folder's README, with
the values the script gets, because no screen definition was adjusted in order to match
a published number.

## Tables with no standalone result CSV (derived, snapshot, or manual)

These supplement tables are not given their own folder because they do not have an independent
deterministic CSV; the master list records where their numbers come from:

- **S2** — controlled-vocabulary normalization by field: an aggregate of the counts in
  `Table3_normalization_quality/collapse.csv` (raw values, canonical concepts) and the MeSH /
  Cellosaurus / OOV concept counts from `S7_vocabulary_precision/vocabulary_precision.csv`.
- **S15** — consolidated extraction yield: re-tabulates the corpus coverage counts with the manual
  benchmark's precision/recall; the control/disease split it quotes is
  `S17_condition_control_split/condition_control_split.csv`.
- **S16, S18** — consolidated re-presentations (precision/recall, arithmetic audit) that
  re-tabulate the numbers already in Table 1 / S1 (manual accuracy) and S5 (Track A); they introduce
  no new measurement. S18's false-negative column is deliberately wider than S1's: it is S1's
  substitutions plus its false negatives, which is the quantity that makes TP/(TP+FN) equal the
  recall reported in both.
- **S25** — residual-error census: consolidates counts already measured elsewhere, chiefly
  `S19_metadata_grounding/grounding_audit.csv` (Sex 'Not Specified' with a token present 6,632;
  Sex ungrounded 88; Age ungrounded 25,649) and the contradiction screens in
  `S23_integrity_screens/`. No new measurement.
- **S34** — the Phase 2 cascade as executed, and where controlled-vocabulary membership is
  enforced: read off the shipped code, `code/2_normalization_phase2/run_phase2.py` (the driver
  `run_full.sh` invokes) together with `phase2_normalize.py` and `phase2_llm.py`. No new
  measurement. It replaces the reference to a Figure S2 that was never in the supplement.
- **S35** — the three phases side by side per label: Phase 1 and Phase 1b coverage from
  `S3_phase1b_recovery/`, Phase 2 coverage from `S10_corpus_composition/composition.csv`, and the
  controlled-vocabulary share from `S13_baseline_comparison/baseline_comparison.csv`. No new
  measurement.
- **S26** — study-scoped expansion of acronym-shaped Condition labels: the verdict counts are
  `error_analysis/acronym_audit_summary.csv` (947 / 116 / 2,467 / 10,512 occurrences), expressed
  there as percentages of the 65,696 acronym-shaped occurrences that `S24_acronym_population`
  defines.

All S10 columns (Labels present / Not Specified / MeSH / Cellosaurus / OOV) reproduce exactly from
`S10_corpus_composition/composition.csv` (Labels present = MeSH + Cellosaurus + OOV).


Tables added in revision 1, answering the review
------------------------------------------------
S27  study_level_performance   the Table S1 verdicts re-aggregated by GSE, every study
                               weighted equally. Folder S27_study_level_performance.
S28  cluster_bootstrap         confidence intervals from resampling whole studies,
                               replacing Wilson where samples are not independent.
                               Folder S28_cluster_bootstrap (same script as S27).
S29  concept_level_performance macro average over distinct gold concepts, with the rare
                               and common tails. Folder S29_concept_level_performance.
S33  branch_and_consistency  MeSH branch validity and identical-input consistency,
                               previously quoted in the Results with no table.
                               Folder S33_branch_and_consistency.
S30  benchmark_grounding       how often the answer is stated verbatim in the record,
                               and accuracy split by whether a key names the field.
                               Folder S30_benchmark_grounding.
S31  multi-label scoring       NO new folder. The numbers are those already in
                               multilabel/ (score_all.py); the table only promotes them
                               out of a supplementary note.
S32  approach comparison       NO folder. It compares published descriptions of GEOfetch,
                               Metappuccino, NLP-ML/txt2onto, SPIRES/OntoGPT and BioSyn
                               with this pipeline. Every figure quoted in it is the one
                               those authors report on their own benchmark and is not
                               comparable to Table S1; nothing in it is measured here.

Table S1 was corrected in the same revision. Its false-positive column is split into
substitutions (the gold holds a different value) and false alarms (the gold holds no
value), and the Sex and Age false-negative counts drop to 3,493 and 59 accordingly, so
each row now sums to n. No precision, recall or F1 changed. The corrected counts are
verified against the data by verify_sexage.py and age_unparse.py in
S27_study_level_performance.
