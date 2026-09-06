# Supplementary evaluation package — Oxford Bioinformatics submission

One folder per supplementary table, numbered as in the submitted supplement
(Tables S1–S22). Each folder holds that table's result CSV, the script that
produces it, and a README giving the caption and the run command.

All deterministic metrics read the released corpus and the reference databases
shipped with the repository:

```
../published_data/data/06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz
../published_data/data/06_final_labels_repaired/phase2_out/
../published_data/data/01_input_metadata/geo_metadata.sqlite
../published_data/reference/mesh.sqlite
../published_data/reference/cellosaurus.sqlite
```

## Tables with their own folder

| table | folder | result |
|---|---|---|
| S1 | `S01_manual_benchmark/` | manual accuracy benchmark; verdicts assigned by hand |
| S2 | `S02_phase1b_recovery/` | `phase1b_recovery.csv` |
| S3 | `S03_database_resolution/` | `db_resolution.csv` |
| S4 | `S04_acronym_expansion/` | `acronym_expansion.csv` |
| S5 | `S05_vocabulary_precision/` | `vocabulary_precision.csv` |
| S6 | `S06_oov_consolidation/` | `oov_consolidation.csv` |
| S7 | `S07_gse_shortform_audit/` | `E_gse_shortform_audit.csv` |
| S8 | `S08_casefold/` | `casefold_effect.csv` |
| S9 | `S09_baseline_comparison/` | `baseline_comparison.csv` |
| S11 | `S11_resource_summary/` | `resource_summary.csv` |
| S12 | `S12_integrity_screens/` | `screen_verification.csv`, `still_failing.csv` |
| S13 | `S13_residual_error_census/` | `error_classes.csv` and companions |
| S14 | `S14_study_level_performance/` | study-weighted benchmark |
| S15 | `S15_cluster_bootstrap/` | cluster-aware confidence intervals |
| S16 | `S16_concept_level_performance/` | rare versus common concepts |
| S17 | `S17_benchmark_grounding/` | `grounding_audit.csv` |
| S18 | `S18_multilabel/` | multi-label judge scoring |
| S20 | `S20_branch_and_consistency/` | branch validity and consistency |

Main-text tables are kept alongside: `Table3_normalization_quality/` and
`Table4_integrity_screens/`. The independent judge used for the OOV audit is
documented in `Methods_LLM_judge/`.

## Tables derived from other folders

Four tables introduce no new measurement and therefore have no folder of their own.

| table | where its numbers come from |
|---|---|
| S10 | recall provenance and arithmetic audit — re-tabulates S1 and S3 |
| S19 | scope of the closest approaches — values are author-reported by those methods, nothing is measured here |
| S21 | Phase 2 cascade and vocabulary enforcement — read off the shipped pipeline code |
| S22 | phase-wise coverage — `S02_phase1b_recovery/`, `supporting/corpus_composition/` and `S09_baseline_comparison/` |

Phase 1 and Phase 1b counts in S22 follow from `phase1b_recovery.csv` as
`804,427 − not_specified_at_phase1`, and Phase 2 counts as
`804,427 − not_specified` from `supporting/corpus_composition/composition.csv`.

## supporting/

Four measurements that the tables above are computed from, kept because a table or
a script reads them directly. Nothing here is a separate result.

```
supporting/corpus_composition/    per-field MeSH / Cellosaurus / OOV counts; S22 derives from it
supporting/age_analysis/          Age value classification used by the Table 4 screens
supporting/field_scope/           cross-field scope corrections cited by S13
supporting/metadata_grounding/    Sex and Age grounding counts read by S13's script
```

## Two counting conventions

`supporting/corpus_composition/composition.csv` and `S11_resource_summary/resource_summary.csv`
report the same fields with different totals, and both are correct. `composition`
assigns each sample to exactly one class in the order MeSH → Cellosaurus → OOV;
`resource_summary` counts a sample once in every class it carries. A sample whose
Tissue holds both a MeSH term and a cell line appears once in the first and twice in
the second. S11 uses the second convention, S22 the first.

## Reproduction

Each folder's README gives its run command. Two worked examples:

```bash
cd S06_oov_consolidation && python3 phase2_oov_consolidation.py
```

```bash
cd S11_resource_summary && python3 resource_summary.py \
    ../../published_data/data/06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz \
    resource_summary.csv
```

`Table4_integrity_screens/README.md` states which of its cells reproduce exactly and
which two denominators do not, with the values the script returns. No screen
definition was adjusted to match a published number.
